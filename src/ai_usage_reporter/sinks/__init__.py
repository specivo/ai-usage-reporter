"""Sinks receive wire records (already privacy-filtered dicts) and report one outcome each.

Built-in sinks are ``specivo`` (the HTTP contract) and ``json`` (stdout or a
file). Third-party sinks come in through the entry-point group
``ai_usage_reporter.sinks``: the entry point names a callable that takes the
:class:`SinkContext` and returns a :class:`Sink`. Third-party sinks are
experimental.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol, TextIO

from ai_usage_reporter import CLIENT_NAME, CONTRACT_VERSION, __version__

ENTRY_POINT_GROUP = "ai_usage_reporter.sinks"

#: Outcome statuses besides the server's created/updated/unchanged/rejected.
PENDING = "pending"  # not delivered; stays in the retry queue
WRITTEN = "written"  # handed to a non-recording sink (json)


@dataclass(slots=True)
class Outcome:
    idempotency_key: str
    status: str
    error_code: str | None = None
    error_message: str | None = None
    error_field: str | None = None
    warnings: list[str] = field(default_factory=list)
    entry_id: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status in ("created", "updated", "unchanged")


class Sink(Protocol):
    name: str
    #: Whether outcomes update the bucket ledger (only a real server's do).
    records_state: bool

    def send(self, records: Sequence[dict[str, Any]]) -> list[Outcome]: ...


@dataclass(frozen=True, slots=True)
class SinkContext:
    url: str | None = None
    api_key: str | None = None
    output: Path | None = None
    stream: TextIO | None = None


def make_batch(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": CONTRACT_VERSION,
        "client": {"name": CLIENT_NAME, "version": __version__},
        "records": list(records),
    }


def plugin_sinks() -> dict[str, Callable[[SinkContext], Sink]]:
    found: dict[str, Callable[[SinkContext], Sink]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in ("specivo", "json"):
            continue
        try:
            found[ep.name] = ep.load()
        except Exception:
            continue
    return found
