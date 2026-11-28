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
    #: Thread events (prompts, model output, tool calls) read for active time.
    thread_events: int = 0
    #: Things about the file the person should know (a schema newer than the
    #: adapter was tested with, counters that went back); shown by ``push`` and ``doctor``.
    notes: list[str] = field(default_factory=list)


class SourceBusyError(Exception):
    """The source is locked by its tool right now; it is skipped and read on the next run."""


class SourceSchemaError(Exception):
    """The tool's data has a layout the adapter cannot read (its schema changed).

    The file is skipped, the other sources go on, and ``push`` and ``doctor``
    show the message, which tells the person what to update.
    """


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
    #: The thread of the session the request ran in when it is not the main
    #: thread: a Claude Code subagent's transcript. ``None`` is the main thread
    #: (or a child session already named in ``sub_session_id``).
    thread: str | None = None
    #: Set on a **marker**, not on usage: the agent marked this issue (upper
    #: case, e.g. ``ACME-12``) at ``timestamp`` with one of its Specivo tool
    #: calls. A marker carries no tokens and no requests; see
    #: :mod:`ai_usage_reporter.segments`.
    issue_mark: str | None = None
    #: Where in the agent's work the request ran, when the tool says so (constant
    #: within a session): the session it continues or was started from, a run
    #: with nobody in the loop (cron, scheduled), and the agent profile.
    parent_session: str | None = None
    unattended: bool = False
    profile: str | None = None
    #: The backend the request went through as the tool names it (a gateway, a
    #: provider id), when it is not ``provider`` itself; sent as ``route``.
    route: str | None = None
    #: How the request is paid when the tool says so (``subscription``, ``local``);
    #: a matching ``[[cost_rules]]`` entry wins over it.
    cost_type: str | None = None
    #: When the requests this event stands for began, for an event that sums
    #: several (``timestamp`` is the last one). ``None``: at ``timestamp``.
    span_start: datetime | None = None

    @property
    def is_marker(self) -> bool:
        return self.issue_mark is not None

    @property
    def thread_id(self) -> str | None:
        """The thread within the session: a subagent transcript or a child session."""
        return self.thread or self.sub_session_id

    def to_cache(self) -> dict[str, Any]:
        """A JSON-able form for the local state cache (never sent anywhere)."""
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _DATETIME_FIELDS and value is not None:
                value = value.isoformat()
            elif f.name == "tokens":
                value = value.as_dict()
            data[f.name] = value
        return data

    @classmethod
    def from_cache(cls, data: Mapping[str, Any]) -> UsageEvent:
        values = dict(data)
        for name in _DATETIME_FIELDS:
            if values.get(name) is not None:
                values[name] = datetime.fromisoformat(values[name])
        values["tokens"] = TokenCounts.from_mapping(values["tokens"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in known})

    def with_session(self, session_id: str) -> UsageEvent:
        return replace(self, session_id=session_id)


_DATETIME_FIELDS = ("timestamp", "span_start")

#: The kinds of :class:`ThreadEvent` (see the active time definition in :mod:`ai_usage_reporter.activity`).
PROMPT = "prompt"
MODEL = "model"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
TURN_END = "turn_end"
THREAD_EVENT_KINDS = (PROMPT, MODEL, TOOL_START, TOOL_END, TURN_END)
#: A thread event that is no moment of work: the thread started a subagent (work activity only).
#: Active time never sees it (see :func:`ai_usage_reporter.activity.timed_events`).
SPAWN = "spawn"
#: Marks a :class:`ThreadEvent` in the parse cache, next to the usage events.
_THREAD_CACHE_TAG = "thread_event"


@dataclass(frozen=True, slots=True)
class ThreadEvent:
    """A moment in one thread of a session, read for measuring active time.

    A thread is the main conversation of a session (``thread_id`` ``None``) or
    one subagent; ``thread_id`` matches :attr:`UsageEvent.thread_id` of the
    requests made in it. ``kind`` is one of :data:`THREAD_EVENT_KINDS`;
    ``call_id`` pairs a ``tool_start`` with its ``tool_end``. The flags classify
    a tool call for the definition's exceptions and may be set on either event
    of the pair: a tool whose run is a person answering (``human_wait``), a call
    that blocks until a child thread finishes (``subagent_wait``), and a call a
    person approved or rejected by hand (``user_decided``). ``key`` names the
    event across all files of the adapter (a replayed line gives the same key),
    so it is counted once; ``None``: never deduplicated.

    Local only, like :class:`UsageEvent`: nothing here is ever serialised for
    the network, only the seconds and spans computed from it.
    """

    tool: str
    session_id: str
    thread_id: str | None
    timestamp: datetime
    kind: str
    call_id: str | None = None
    human_wait: bool = False
    subagent_wait: bool = False
    user_decided: bool = False
    key: str | None = None
    #: Work activity detail of a tool call (see :mod:`ai_usage_reporter.work_activity`), set on either
    #: event of the pair: the call's category (one of ``work_activity.CATEGORIES``), whether it failed,
    #: the lines its patch added and removed, local hashes naming the files it edited (never a path),
    #: and whether it started a subagent. Read from the tool's input and result in memory and reduced
    #: to these values at once: no command, path, patch or output is kept.
    category: str | None = None
    failed: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    files: tuple[str, ...] = ()
    spawns: bool = False

    def to_cache(self) -> dict[str, Any]:
        """A JSON-able form for the local parse cache (never sent anywhere)."""
        data: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        data["timestamp"] = self.timestamp.isoformat()
        data[_THREAD_CACHE_TAG] = True
        return data

    @classmethod
    def from_cache(cls, data: Mapping[str, Any]) -> ThreadEvent:
        values = {k: v for k, v in data.items() if k in {f.name for f in fields(cls)}}
        values["timestamp"] = datetime.fromisoformat(values["timestamp"])
        if "files" in values:
            values["files"] = tuple(str(v) for v in values["files"] or ())
        return cls(**values)


def event_from_cache(data: Mapping[str, Any]) -> UsageEvent | ThreadEvent:
    """A cached usage or thread event (a cache written before thread events holds usage only)."""
    if data.get(_THREAD_CACHE_TAG) is True:
        return ThreadEvent.from_cache(data)
    return UsageEvent.from_cache(data)


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
