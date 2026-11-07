"""Issue segments: which issue each request of a session worked on, from the agent's own marks.

Adapters yield **markers** (:attr:`UsageEvent.issue_mark`) where an agent
marked an issue with a Specivo tool call (see
:mod:`ai_usage_reporter.issue_marks`). This module gives every request of a
session the issue of its **segment**:

- **Main thread.** A request belongs to the issue of the latest mark at or
  before it. Requests before the session's first mark belong to the first
  marked issue: that is the research done before the issue existed or was
  picked up.
- **A thread** (a subagent's transcript, an OpenCode child session) with
  marks of its own follows the same rule with its own marks. A thread without
  marks takes the main thread's issue at the time the thread started (its
  first request or mark).
- A session with no marks at all has no segment issue.

Records are then cut by segment as well as by model and day (see
:func:`ai_usage_reporter.aggregate.aggregate`).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime

from ai_usage_reporter.model import UsageEvent

ThreadKey = tuple[str, str, str | None]
Marks = list[tuple[datetime, str]]


def split_markers(events: Iterable[UsageEvent]) -> tuple[list[UsageEvent], list[UsageEvent]]:
    """The usage events and the markers, apart."""
    usage: list[UsageEvent] = []
    markers: list[UsageEvent] = []
    for event in events:
        (markers if event.is_marker else usage).append(event)
    return usage, markers


def _issue_at(marks: Marks, when: datetime) -> str | None:
    if not marks:
        return None
    index = bisect_right(marks, when, key=lambda item: item[0])
    return marks[index - 1][1] if index else marks[0][1]


def segment_issues(usage: Sequence[UsageEvent], markers: Iterable[UsageEvent]) -> list[str | None]:
    """The segment issue of each event of *usage*, in the same order."""
    marks: dict[ThreadKey, set[tuple[datetime, str]]] = defaultdict(set)
    for marker in markers:
        if marker.issue_mark:
            marks[(marker.tool, marker.session_id, marker.thread_id)].add((marker.timestamp, marker.issue_mark))
    if not marks:
        return [None] * len(usage)
    ordered: dict[ThreadKey, Marks] = {key: sorted(found) for key, found in marks.items()}
    starts: dict[ThreadKey, datetime] = {}
    for event in usage:
        key = (event.tool, event.session_id, event.thread_id)
        if key[2] is not None and (key not in starts or event.timestamp < starts[key]):
            starts[key] = event.timestamp
    for key, found in ordered.items():
        if key[2] is not None and (key not in starts or found[0][0] < starts[key]):
            starts[key] = found[0][0]
    issues: list[str | None] = []
    for event in usage:
        key = (event.tool, event.session_id, event.thread_id)
        own = ordered.get(key)
        if own:
            issues.append(_issue_at(own, event.timestamp))
            continue
        main = ordered.get((event.tool, event.session_id, None))
        if not main:
            issues.append(None)
            continue
        issues.append(_issue_at(main, starts.get(key, event.timestamp)))
    return issues
