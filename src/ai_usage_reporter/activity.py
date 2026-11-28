"""Active time: how long an agent was busy, from the events of its threads (definition version 1).

This module is the one implementation of the definition. Adapters only yield
:class:`~ai_usage_reporter.model.ThreadEvent` s; everything else is here, pure
and without I/O.

**Terms.** A *thread* is one line of work with its own transcript: the main
conversation of a session or one subagent. A *turn* runs from a ``prompt`` (a
person's message, a background notification, a subagent handing back) to the
end of the agent's answer. Events are ``prompt``, ``model``, ``tool_start``,
``tool_end`` and ``turn_end``.

**Definition, version 1.** The constants :data:`GAP_LIMIT` (600 s) and
:data:`TOOL_CAP` (3600 s) belong to the version and are not configurable. The
*active set* of a thread is the union of:

1. **Tool runs**: from a call's ``tool_start`` to its ``tool_end``, paired by
   the call id (other events may lie between them), at most the first
   :data:`TOOL_CAP` seconds, and cut at the end of its turn. Exceptions:

   - a *human-wait* tool (a person answering) contributes nothing, and its
     interval is removed from the active set even where rule 2 adds it;
   - a *subagent-wait* call (blocks until a child thread finishes) contributes
     nothing; the child counts its own time, and rule 2 still applies;
   - a call a person approved or rejected by hand is not a tool run; rule 2
     decides.

2. **Short gaps**: the interval between two consecutive events of the thread
   when it is at most :data:`GAP_LIMIT` long.
3. **Never across a turn boundary**: a gap that starts at a ``turn_end`` or
   ends at a ``prompt`` is never added.

A tool call without a result contributes nothing. Overlapping intervals
(parallel tool calls) count once. The *agent time* of a scope is the sum of
its threads' active seconds; its *wall clock* is the length of the union of
their active sets.

**Owner rule.** The active set is cut at every event (and every request) into
pieces. A piece belongs to one model request of the same thread: the first one
at or after the piece's end when it lies in the same turn, otherwise the last
one at or before the piece's start, otherwise none (the piece is *unplaced*:
counted for ``doctor``, never sent). A piece inherits everything from its
request: model, UTC day, issue segment and its fate in deduplication. So time
follows tokens exactly, and pieces are never cut at midnight.

**Spans on the wire** (:func:`wire_spans`): rounded to whole seconds, clipped
to the window the contract allows, merged where they touch, and at most
:data:`MAX_SPANS`: beyond that, spans less than :data:`SPAN_JOIN_GAP` seconds
apart are joined (smallest gaps first) while the contract's length rule holds,
then the shortest spans are dropped. ``active_seconds`` is never changed by
this; only the wall clock of such a record comes out slightly low.

Times inside this module are POSIX seconds (floats).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ai_usage_reporter.model import PROMPT, TOOL_END, TOOL_START, TURN_END, ThreadEvent, UsageEvent

#: The definition the values are computed with; sent as ``active_time_version``.
ACTIVE_TIME_VERSION = 1
#: Gaps between two events count up to this long (seconds).
GAP_LIMIT = 600
#: A tool run counts its first this many seconds.
TOOL_CAP = 3600
#: The contract's limit of spans per record.
MAX_SPANS = 1000
#: Over :data:`MAX_SPANS`, spans closer than this (seconds) are joined first.
SPAN_JOIN_GAP = 60

#: ``(start, end)`` in POSIX seconds, ``start < end``.
Span = tuple[float, float]
#: A thread: (tool, session id, thread id or ``None`` for the main thread).
ThreadKey = tuple[str, str, str | None]


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """The union of *spans*, sorted, with overlapping and touching spans joined; empty ones dropped."""
    out: list[Span] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def subtract_spans(spans: Sequence[Span], holes: Sequence[Span]) -> list[Span]:
    """*spans* without *holes*; both merged and sorted."""
    out: list[Span] = []
    index = 0
    for start, end in spans:
        current = start
        while index < len(holes) and holes[index][1] <= current:
            index += 1
        probe = index
        while probe < len(holes) and holes[probe][0] < end:
            hole_start, hole_end = holes[probe]
            if hole_start > current:
                out.append((current, hole_start))
            current = max(current, hole_end)
            if current >= end:
                break
            probe += 1
        if current < end:
            out.append((current, end))
    return out


def total_seconds(spans: Iterable[Span]) -> float:
    return sum(end - start for start, end in spans)


def _ordered(events: Sequence[ThreadEvent]) -> tuple[list[ThreadEvent], list[float]]:
    # sorted() is stable: events at the same instant keep the order the adapter read them in.
    ordered = sorted(events, key=lambda e: e.timestamp)
    return ordered, [e.timestamp.timestamp() for e in ordered]


def _boundaries(ordered: Sequence[ThreadEvent], times: Sequence[float]) -> list[float]:
    return [t for e, t in zip(ordered, times, strict=True) if e.kind in (PROMPT, TURN_END)]


def active_set(events: Sequence[ThreadEvent]) -> list[Span]:
    """The active set of one thread (definition version 1), merged and sorted."""
    ordered, times = _ordered(events)
    boundaries = _boundaries(ordered, times)
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for index, event in enumerate(ordered):
        if event.call_id is None:
            continue
        if event.kind == TOOL_START:
            starts.setdefault(event.call_id, index)
        elif event.kind == TOOL_END:
            ends.setdefault(event.call_id, index)
    added: list[Span] = []
    removed: list[Span] = []
    # Rule 1: tool runs, paired by call id.
    for call_id, first in starts.items():
        last = ends.get(call_id)
        if last is None:
            continue  # no result: the session was killed
        start_event, end_event = ordered[first], ordered[last]
        start, end = times[first], times[last]
        if end <= start:
            continue
        if start_event.human_wait or end_event.human_wait:
            removed.append((start, end))
            continue
        if start_event.subagent_wait or end_event.subagent_wait:
            continue
        if start_event.user_decided or end_event.user_decided:
            continue
        stop = min(end, start + TOOL_CAP)
        # Cut at the end of its turn: the first boundary after the call started.
        boundary = bisect_right(boundaries, start)
        if boundary < len(boundaries):
            stop = min(stop, boundaries[boundary])
        if stop > start:
            added.append((start, stop))
    # Rules 2 and 3: short gaps between consecutive events, never across a turn boundary.
    for index in range(len(ordered) - 1):
        if ordered[index].kind == TURN_END or ordered[index + 1].kind == PROMPT:
            continue
        start, end = times[index], times[index + 1]
        if 0 < end - start <= GAP_LIMIT:
            added.append((start, end))
    return subtract_spans(merge_spans(added), merge_spans(removed))


def cut_pieces(active: Sequence[Span], cuts: Sequence[float]) -> list[Span]:
    """*active* cut at every instant of *cuts* (sorted) that lies strictly inside a span."""
    out: list[Span] = []
    for start, end in active:
        index = bisect_right(cuts, start)
        current = start
        while index < len(cuts) and cuts[index] < end:
            out.append((current, cuts[index]))
            current = cuts[index]
            index += 1
        out.append((current, end))
    return out


def owner(piece: Span, usage_times: Sequence[float], boundaries: Sequence[float]) -> int | None:
    """The index in *usage_times* (sorted) of the request that owns *piece*, or ``None`` (unplaced).

    1. The first request at or after the piece's end, when it lies in the same turn;
    2. otherwise the last request at or before the piece's start;
    3. otherwise none.

    Of several requests at the same instant the first one is taken.
    """
    start, end = piece
    turn = bisect_right(boundaries, start)
    after = bisect_left(usage_times, end)
    # A request at the instant of the turn's closing boundary still belongs to the turn.
    if after < len(usage_times) and bisect_left(boundaries, usage_times[after]) == turn:
        return after
    before = bisect_right(usage_times, start) - 1
    if before >= 0:
        return bisect_left(usage_times, usage_times[before])
    return None


@dataclass(slots=True)
class ThreadTime:
    """One thread's active time, cut into pieces and given to its requests."""

    #: (index into the usage list given to :func:`attribute`, piece).
    owned: list[tuple[int, Span]] = field(default_factory=list)
    #: Seconds of pieces without a request.
    unplaced: float = 0.0
    #: The thread's active seconds (owned and unplaced).
    active: float = 0.0


def attribute(events: Sequence[ThreadEvent], usage_times: Sequence[float]) -> ThreadTime:
    """The pieces of one thread's active set and the request (an index into *usage_times*) owning each."""
    result = ThreadTime()
    if not events:
        return result
    active = active_set(events)
    result.active = total_seconds(active)
    if not active:
        return result
    ordered, times = _ordered(events)
    boundaries = _boundaries(ordered, times)
    order = sorted(range(len(usage_times)), key=lambda i: usage_times[i])
    sorted_usage = [usage_times[i] for i in order]
    cuts = sorted(set(times) | set(usage_times))
    for piece in cut_pieces(active, cuts):
        found = owner(piece, sorted_usage, boundaries)
        if found is None:
            result.unplaced += piece[1] - piece[0]
        else:
            result.owned.append((order[found], piece))
    return result


def deduplicate_thread_events(events: Iterable[ThreadEvent]) -> list[ThreadEvent]:
    """*events* without repeats: of the events with the same ``key`` the first one read is kept."""
    seen: set[str] = set()
    out: list[ThreadEvent] = []
    for event in events:
        if event.key is not None:
            if event.key in seen:
                continue
            seen.add(event.key)
        out.append(event)
    return out


@dataclass(slots=True)
class OwnedTime:
    """The pieces of all threads, given to the usage events that own them."""

    #: Index into the usage list -> the pieces it owns.
    pieces: dict[int, list[Span]] = field(default_factory=dict)
    #: The sessions (tool, session id) that have thread events: their buckets are measured.
    measured_sessions: set[tuple[str, str]] = field(default_factory=set)
    #: Unplaced seconds per tool.
    unplaced: dict[str, float] = field(default_factory=dict)


def attribute_threads(thread_events: Iterable[ThreadEvent], usage: Sequence[UsageEvent]) -> OwnedTime:
    """The pieces of every thread in *thread_events*, keyed by the index in *usage* of their owner.

    *usage* are the requests (no markers) the owners are chosen from: those of
    the same thread (tool, session, :attr:`UsageEvent.thread_id`).
    """
    by_thread: dict[ThreadKey, list[ThreadEvent]] = defaultdict(list)
    for event in thread_events:
        by_thread[(event.tool, event.session_id, event.thread_id)].append(event)
    requests: dict[ThreadKey, list[int]] = defaultdict(list)
    for index, request in enumerate(usage):
        key = (request.tool, request.session_id, request.thread_id)
        if key in by_thread:
            requests[key].append(index)
    result = OwnedTime()
    for key, events in by_thread.items():
        result.measured_sessions.add((key[0], key[1]))
        indices = requests.get(key, [])
        thread = attribute(events, [usage[i].timestamp.timestamp() for i in indices])
        for local, piece in thread.owned:
            result.pieces.setdefault(indices[local], []).append(piece)
        if thread.unplaced:
            result.unplaced[key[0]] = result.unplaced.get(key[0], 0.0) + thread.unplaced
    return result


def round_seconds(value: float) -> int:
    """*value* rounded to the nearest whole second, halves up."""
    return math.floor(value + 0.5)


def length_allowed(spans: Sequence[tuple[int, int]], active_seconds: int) -> bool:
    """The contract's rule: the spans' total length is at most ``active_seconds + 2 * len(spans) + 1``."""
    return sum(end - start for start, end in spans) <= active_seconds + 2 * len(spans) + 1


def limit_spans(spans: Sequence[tuple[int, int]], active_seconds: int, limit: int = MAX_SPANS) -> list[tuple[int, int]]:
    """At most *limit* spans: join close ones while the length rule holds, then drop the shortest."""
    spans = list(spans)
    if len(spans) <= limit:
        return spans
    length = sum(end - start for start, end in spans)
    between = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
    gaps = sorted(item for item in between if item[0] < SPAN_JOIN_GAP)
    joined: set[int] = set()
    for gap, index in gaps:
        count = len(spans) - len(joined)
        if count <= limit or length + gap > active_seconds + 2 * (count - 1) + 1:
            break
        joined.add(index)
        length += gap
    merged: list[tuple[int, int]] = []
    for index, span in enumerate(spans):
        if index - 1 in joined:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    if len(merged) > limit:
        keep = sorted(range(len(merged)), key=lambda i: (-(merged[i][1] - merged[i][0]), i))[:limit]
        merged = [merged[i] for i in sorted(keep)]
    return merged


def wire_spans(spans: Iterable[Span], active_seconds: int, window: tuple[float, float]) -> list[tuple[int, int]] | None:
    """*spans* as the contract wants them: whole seconds, inside *window*, merged, at most :data:`MAX_SPANS`.

    ``None`` when they cannot be sent with *active_seconds* (their union is
    longer than the sum allows, which only inconsistent input can cause).
    """
    low, high = window
    rounded: list[Span] = []
    for start, end in spans:
        first, last = max(round_seconds(start), math.ceil(low)), min(round_seconds(end), math.floor(high))
        if last > first:
            rounded.append((first, last))
    whole = [(int(start), int(end)) for start, end in merge_spans(rounded)]
    whole = limit_spans(whole, active_seconds)
    return whole if length_allowed(whole, active_seconds) else None
