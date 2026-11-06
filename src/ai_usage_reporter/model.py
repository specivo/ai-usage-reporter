"""Internal data types shared by adapters, the pipeline and the sinks.

Nothing in this module is ever serialised for the network. :class:`UsageEvent`
carries local context (the working directory, the source file) that the
enrichment step reads, and that :mod:`ai_usage_reporter.record` deliberately
leaves out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The token classes of the contract, in its order.
TOKEN_CLASSES: tuple[str, ...] = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "reasoning")


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Token counts in the contract's (Anthropic-style) semantics.

    ``input`` excludes cache reads and writes, and ``output`` excludes reasoning
    when the provider reports reasoning apart, so every token is in exactly one
    class.
    """

    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    reasoning: int = 0

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(*(getattr(self, c) + getattr(other, c) for c in TOKEN_CLASSES))

    @property
    def total(self) -> int:
        return sum(getattr(self, c) for c in TOKEN_CLASSES)

    @property
    def context(self) -> int:
        """The context one request carried: fresh input plus cache reads and writes."""
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def is_zero(self) -> bool:
        return self.total == 0

    def as_dict(self) -> dict[str, int]:
        return {c: int(getattr(self, c)) for c in TOKEN_CLASSES}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> TokenCounts:
        value = value or {}
        return cls(**{c: _count(value.get(c)) for c in TOKEN_CLASSES})


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def count(value: object) -> int:
    """A non-negative integer from a log value; anything else is 0."""
    return _count(value)


@dataclass(slots=True)
class ParseStats:
    """What a parse of one file found besides usage; shown by ``ai-usage doctor``."""

    lines: int = 0
    malformed_lines: int = 0
    events: int = 0
    skipped_events: int = 0


class SourceBusyError(Exception):
    """The source is locked by its tool right now; it is skipped and read on the next run."""


@dataclass(slots=True)
class SourceFile:
    """One log file an adapter found.

    ``path`` is local only. ``priority`` orders copies of the same session (lower
    wins) when an adapter keeps one copy per session; ``session_hint`` is the
    session id the adapter read from the path, if any. ``companions`` are files
    whose changes also change what :meth:`Adapter.parse` reads from ``path``
    (a SQLite write-ahead log), so the reporter parses again when they change.
    """

    adapter: str
    path: Path
    root: Path
    session_hint: str | None = None
    priority: int = 0
    stats: ParseStats = field(default_factory=ParseStats)
    companions: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """Usage of one model request, as an adapter read it.

    Local context (``cwd``, ``source_path``) is used for enrichment and
    exclusion only and never leaves the machine.
    """

    tool: str
    session_id: str
    model: str
    timestamp: datetime
    tokens: TokenCounts
    provider: str | None = None
    tool_version: str | None = None
    #: Native ids an adapter uses for its dedup keys.
    message_id: str | None = None
    request_id: str | None = None
    #: A copy that may repeat another event (a Claude Code sidechain). It loses
    #: against a non-replay copy of the same request.
    is_replay: bool = False
    #: Names the message for the replay check: a replay event whose key matches
    #: a non-replay event's is dropped (see :mod:`ai_usage_reporter.dedup`).
    replay_key: str | None = None
    git_branch: str | None = None
    #: The repository URL when the tool logs it (Codex does); normalised later.
    repo_url: str | None = None
    cwd: str | None = None
    source_path: str | None = None
    #: How many model requests this event stands for (normally 1).
    requests: int = 1
    #: The child session (a subagent) the request ran in, when ``session_id``
    #: is the parent it was folded into. ``push --session`` matches either.
    sub_session_id: str | None = None
    #: What the tool says the request cost, as a decimal string, and its ISO
    #: 4217 currency. Sent as ``reported_cost`` when every request of a bucket
    #: has one.
    cost: str | None = None
    cost_currency: str | None = None

    def to_cache(self) -> dict[str, Any]:
        """A JSON-able form for the local state cache (never sent anywhere)."""
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "timestamp":
                value = value.isoformat()
            elif f.name == "tokens":
                value = value.as_dict()
            data[f.name] = value
        return data

    @classmethod
    def from_cache(cls, data: Mapping[str, Any]) -> UsageEvent:
        values = dict(data)
        values["timestamp"] = datetime.fromisoformat(values["timestamp"])
        values["tokens"] = TokenCounts.from_mapping(values["tokens"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in known})

    def with_session(self, session_id: str) -> UsageEvent:
        return replace(self, session_id=session_id)


def parse_timestamp(value: object) -> datetime | None:
    """An aware UTC datetime from an ISO 8601 string or epoch seconds/milliseconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
        # Values this large are milliseconds. Anything outside 2000..2100 is noise.
        if seconds > 1e11:
            seconds /= 1000.0
        if not 946684800 <= seconds <= 4102444800:  # 2000-01-01 .. 2100-01-01
            return None
        return datetime.fromtimestamp(seconds, tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Logs without an offset are UTC in both supported tools.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
