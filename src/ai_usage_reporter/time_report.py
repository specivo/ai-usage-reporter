"""Measured agent time per issue, day, session or tool, from the records a push would send.

``ai-usage time`` collects as a push does and groups the records it would
send; nothing is sent and no server is asked. Per group:

- **agent time**: the sum of the records' ``active_seconds`` (all threads
  added, so two agents at once for ten minutes are twenty minutes);
- **wall clock**: the length of the union of the records' ``active_spans``
  (the same two agents are ten minutes). The total row's wall clock is the
  union over every record, not the sum of the rows;
- **sessions**: distinct (tool, session id) pairs;
- **not measured**: records without active time (a source that yields no
  thread events, or a record whose spans could not be sent). They count apart,
  never as zero.

The values are read from the wire form of each record (``record.to_wire()``),
so they are exactly the numbers a push sends: seconds rounded per record,
spans in whole seconds and clipped to the contract's window.

Only issue keys, project keys, UTC days, tool names and opaque session ids
appear in a report: never a path, a branch, a prompt or a working directory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ai_usage_reporter.activity import Span, union_seconds

GROUPINGS = ("issue", "day", "session", "tool")
UNASSIGNED = "(unassigned)"
NO_SESSION = "(no session)"


@dataclass(slots=True)
class TimeRow:
    """One group of records: its key, and for ``--by session`` the tool of that session."""

    key: str
    tool: str | None = None
    agent_seconds: int = 0
    spans: list[Span] = field(default_factory=list)
    sessions: set[tuple[str, str]] = field(default_factory=set)
    records: int = 0
    unmeasured: int = 0

    @property
    def measured(self) -> int:
        return self.records - self.unmeasured

    @property
    def wall_clock_seconds(self) -> int:
        return round(union_seconds(self.spans))

    def add(self, wire: Mapping[str, Any]) -> None:
        self.records += 1
        self.sessions.add((str(wire.get("tool", "")), str(wire.get("session_id") or NO_SESSION)))
        seconds = wire.get("active_seconds")
        spans = wire.get("active_spans")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not isinstance(spans, list):
            self.unmeasured += 1
            return
        self.agent_seconds += seconds
        for pair in spans:
            start, end = (datetime.fromisoformat(value).timestamp() for value in pair)
            self.spans.append((start, end))

    def as_dict(self) -> dict[str, Any]:
        measured = self.measured > 0
        out: dict[str, Any] = {"key": self.key}
        if self.tool is not None:
            out["tool"] = self.tool
        out.update(
            {
                "wall_clock_seconds": self.wall_clock_seconds if measured else None,
                "agent_seconds": self.agent_seconds if measured else None,
                "sessions": len(self.sessions),
                "records": self.records,
                "unmeasured_records": self.unmeasured,
            }
        )
        return out


@dataclass(slots=True)
class TimeReport:
    by: str
    rows: list[TimeRow]
    total: TimeRow


def group_key(wire: Mapping[str, Any], by: str) -> tuple[str, str | None]:
    """The group of one wire record: (key, tool for ``session``)."""
    if by == "day":
        return str(wire["usage_date"]), None
    if by == "tool":
        return str(wire["tool"]), None
    if by == "session":
        return str(wire.get("session_id") or NO_SESSION), str(wire["tool"])
    attribution = wire.get("attribution") or {}
    issue = attribution.get("issue_ref")
    if isinstance(issue, str) and issue:
        return issue, None
    project = attribution.get("project_key")
    if isinstance(project, str) and project:
        return project, None
    return UNASSIGNED, None


def summarise(
    wires: Iterable[Mapping[str, Any]], by: str, *, since: date | None = None, until: date | None = None
) -> TimeReport:
    """The records in *wires* between *since* and *until* (UTC days, both included), grouped *by*."""
    if by not in GROUPINGS:
        raise ValueError(f"unknown grouping {by!r}")
    rows: dict[tuple[str, str | None], TimeRow] = {}
    total = TimeRow(key="total")
    for wire in wires:
        day = date.fromisoformat(str(wire["usage_date"]))
        if (since is not None and day < since) or (until is not None and day > until):
            continue
        key, tool = group_key(wire, by)
        row = rows.get((key, tool))
        if row is None:
            row = rows[(key, tool)] = TimeRow(key=key, tool=tool)
        row.add(wire)
        total.add(wire)
    if by == "day":
        ordered = sorted(rows.values(), key=lambda r: r.key)
    else:
        ordered = sorted(rows.values(), key=lambda r: (-r.agent_seconds, -r.measured, r.key, r.tool or ""))
    return TimeReport(by=by, rows=ordered, total=total)


def format_duration(seconds: int) -> str:
    """``1 h 12 min``, ``12 min``, ``< 1 min`` or ``0 min``."""
    if seconds <= 0:
        return "0 min"
    minutes = (seconds + 30) // 60  # halves up
    if minutes == 0:
        return "< 1 min"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def render_table(report: TimeReport) -> str:
    """The report as a plain text table with a total row."""
    header = [report.by]
    if report.by == "session":
        header.append("tool")
    header += ["wall clock", "agent time", "sessions", "not measured"]

    def cells(row: TimeRow) -> list[str]:
        out = [row.key]
        if report.by == "session":
            out.append(row.tool or "")
        if row.measured:
            out += [format_duration(row.wall_clock_seconds), format_duration(row.agent_seconds)]
        else:
            out += ["-", "-"]
        out += [str(len(row.sessions)), str(row.unmeasured)]
        return out

    table = [header, *(cells(r) for r in report.rows)]
    total = cells(report.total)
    if report.by == "session":
        total[1] = ""
    table.append(total)
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    numeric = set(range(len(header) - 4, len(header)))

    def line(values: list[str]) -> str:
        parts = [
            v.rjust(w) if i in numeric else v.ljust(w) for i, (v, w) in enumerate(zip(values, widths, strict=True))
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * w for w in widths)
    body = [line(header), rule, *(line(values) for values in table[1:-1]), rule, line(table[-1])]
    return "\n".join(body) + "\n"


def to_json(report: TimeReport, *, since: date | None, until: date | None, would_send: bool) -> dict[str, Any]:
    """The report as a JSON-ready dict; durations in seconds, ``null`` where nothing was measured."""
    return {
        "by": report.by,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "would_send": would_send,
        "groups": [row.as_dict() for row in report.rows],
        "total": report.total.as_dict(),
    }
