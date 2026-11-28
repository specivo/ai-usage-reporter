"""Active time, definition version 1: one test per rule and constant, written from the definition.

Times are seconds after T0; every expected value is worked out by hand next to the case.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from ai_usage_reporter.activity import (
    ACTIVE_TIME_VERSION,
    GAP_LIMIT,
    MAX_SPANS,
    TOOL_CAP,
    active_set,
    attribute,
    attribute_threads,
    cut_pieces,
    deduplicate_thread_events,
    length_allowed,
    limit_spans,
    merge_spans,
    owner,
    subtract_spans,
    total_seconds,
    wire_spans,
)
from ai_usage_reporter.model import MODEL, PROMPT, TOOL_END, TOOL_START, TURN_END, ThreadEvent, TokenCounts, UsageEvent

T0 = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)
BASE = T0.timestamp()


def ev(
    kind: str,
    at: float,
    call: str | None = None,
    *,
    thread: str | None = None,
    human_wait: bool = False,
    subagent_wait: bool = False,
    user_decided: bool = False,
) -> ThreadEvent:
    return ThreadEvent(
        tool="claude-code",
        session_id="s1",
        thread_id=thread,
        timestamp=T0 + timedelta(seconds=at),
        kind=kind,
        call_id=call,
        human_wait=human_wait,
        subagent_wait=subagent_wait,
        user_decided=user_decided,
    )


def seconds(events: list[ThreadEvent]) -> float:
    return total_seconds(active_set(events))


def rel(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(a - BASE, b - BASE) for a, b in spans]


# -- constants -----------------------------------------------------------------------------------


def test_constants_of_version_1() -> None:
    assert ACTIVE_TIME_VERSION == 1
    assert GAP_LIMIT == 600
    assert TOOL_CAP == 3600
    assert MAX_SPANS == 1000


# -- rule 2: short gaps ----------------------------------------------------------------------------


def test_a_gap_of_600_seconds_counts() -> None:
    # prompt 0, model 600: the one gap is exactly the limit.
    assert seconds([ev(PROMPT, 0), ev(MODEL, 600)]) == 600


def test_a_gap_of_601_seconds_counts_nothing() -> None:
    assert seconds([ev(PROMPT, 0), ev(MODEL, 601)]) == 0


def test_short_gaps_add_up_and_a_long_one_is_left_out() -> None:
    # 0 -> 30 (30) -> 90 (60) -> 1000 (910, too long) -> 1100 (100) = 190
    events = [ev(PROMPT, 0), ev(MODEL, 30), ev(MODEL, 90), ev(MODEL, 1000), ev(MODEL, 1100)]
    assert seconds(events) == 190
    assert rel(active_set(events)) == [(0, 90), (1000, 1100)]


def test_events_out_of_order_are_sorted_first() -> None:
    assert seconds([ev(MODEL, 30), ev(PROMPT, 0)]) == 30


def test_no_events_and_one_event_are_no_time() -> None:
    assert seconds([]) == 0
    assert seconds([ev(PROMPT, 0)]) == 0


# -- rule 3: turn boundaries -----------------------------------------------------------------------


def test_a_gap_ending_at_a_prompt_never_counts() -> None:
    # model 0, the person answers 60 s later: the 60 s are the person's, not the agent's.
    assert seconds([ev(PROMPT, -10), ev(MODEL, 0), ev(PROMPT, 60), ev(MODEL, 70)]) == 10 + 10


def test_a_gap_starting_at_a_turn_end_never_counts() -> None:
    # prompt 0, model 20, turn_end 21, model 40 (a late line): 20 + 1, the 19 s after turn_end do not count.
    assert seconds([ev(PROMPT, 0), ev(MODEL, 20), ev(TURN_END, 21), ev(MODEL, 40)]) == 21


# -- rule 1: tool runs -----------------------------------------------------------------------------


def test_a_tool_run_counts_in_full_beyond_the_gap_limit() -> None:
    # A 6 minute test run plus a 16 minute one: nothing is logged while they run, both count.
    events = [ev(PROMPT, 0), ev(MODEL, 5), ev(TOOL_START, 5, "a"), ev(TOOL_END, 965, "a"), ev(MODEL, 970)]
    assert seconds(events) == 970


def test_a_tool_run_of_61_minutes_counts_60() -> None:
    events = [ev(PROMPT, 0), ev(TOOL_START, 0, "a"), ev(TOOL_END, 61 * 60, "a")]
    assert seconds(events) == 3600


def test_a_tool_run_of_exactly_the_cap_counts_in_full() -> None:
    assert seconds([ev(TOOL_START, 0, "a"), ev(TOOL_END, 3600, "a")]) == 3600


def test_tool_runs_pair_by_call_id_not_by_adjacency() -> None:
    # start a at 0, start b at 100, end b at 800 (700 s), end a at 1500: a ran 1500 s.
    events = [
        ev(PROMPT, 0),
        ev(TOOL_START, 0, "a"),
        ev(TOOL_START, 100, "b"),
        ev(TOOL_END, 800, "b"),
        ev(TOOL_END, 1500, "a"),
    ]
    assert seconds(events) == 1500


def test_an_event_inside_a_tool_run_does_not_break_it() -> None:
    # A model line at 400 between start (0) and end (1300): the run still counts 0..1300.
    events = [ev(TOOL_START, 0, "a"), ev(MODEL, 400), ev(TOOL_END, 1300, "a")]
    assert seconds(events) == 1300


def test_parallel_tool_calls_count_once() -> None:
    # Three calls from one message, 0..700, 0..900 and 0..800: the union is 900 s, not 2400.
    events = [ev(TOOL_START, 0, c) for c in "abc"] + [
        ev(TOOL_END, 700, "a"),
        ev(TOOL_END, 900, "b"),
        ev(TOOL_END, 800, "c"),
    ]
    assert seconds(events) == 900


def test_a_tool_call_without_a_result_contributes_nothing() -> None:
    # The session was killed during the call: only the 10 s before it count.
    assert seconds([ev(PROMPT, 0), ev(MODEL, 10), ev(TOOL_START, 10, "a")]) == 10


def test_a_result_without_a_start_is_still_an_event_for_the_gap_rule() -> None:
    assert seconds([ev(MODEL, 0), ev(TOOL_END, 20, "orphan")]) == 20


def test_a_tool_run_is_cut_at_the_end_of_its_turn() -> None:
    # The run starts at 0, the person writes again at 1000 (a prompt), the result is logged at 2000.
    events = [ev(TOOL_START, 0, "a"), ev(PROMPT, 1000), ev(TOOL_END, 2000, "a")]
    assert seconds(events) == 1000
    assert seconds([ev(TOOL_START, 0, "a"), ev(TURN_END, 700), ev(TOOL_END, 900, "a")]) == 700


# -- the three exceptions --------------------------------------------------------------------------


@pytest.mark.parametrize("duration", [20, 3 * 3600])
def test_a_human_wait_tool_counts_nothing(duration: int) -> None:
    # AskUserQuestion for 20 s (rule 2 would add it) and for 3 h: both count 0; the 5 s before count.
    events = [
        ev(PROMPT, -5),
        ev(MODEL, 0),
        ev(TOOL_START, 0, "q", human_wait=True),
        ev(TOOL_END, duration, "q"),
    ]
    assert seconds(events) == 5


def test_a_human_wait_flag_on_the_result_is_enough() -> None:
    assert seconds([ev(TOOL_START, 0, "q"), ev(TOOL_END, 20, "q", human_wait=True)]) == 0


def test_a_human_wait_is_removed_even_from_a_parallel_tool_run() -> None:
    # A 300 s run (0..300) beside a question asked at 100 and answered at 200: 300 - 100 = 200.
    events = [
        ev(TOOL_START, 0, "a"),
        ev(TOOL_START, 100, "q", human_wait=True),
        ev(TOOL_END, 200, "q"),
        ev(TOOL_END, 300, "a"),
    ]
    assert seconds(events) == 200


def test_a_subagent_wait_is_no_tool_run_but_the_gap_rule_applies() -> None:
    # A foreground subagent for 20 min: nothing (the child counts it).
    assert seconds([ev(TOOL_START, 0, "t"), ev(TOOL_END, 1200, "t", subagent_wait=True)]) == 0
    # One of 5 min: rule 2 still adds the gap between the two events.
    assert seconds([ev(TOOL_START, 0, "t"), ev(TOOL_END, 300, "t", subagent_wait=True)]) == 300


@pytest.mark.parametrize(("duration", "expected"), [(30, 30), (20 * 60, 0)])
def test_a_hand_approved_run_is_decided_by_the_gap_rule(duration: int, expected: int) -> None:
    # A Bash call a person approved: 30 s counts through rule 2, 20 min counts nothing.
    events = [ev(TOOL_START, 0, "b"), ev(TOOL_END, duration, "b", user_decided=True)]
    assert seconds(events) == expected


# -- properties ------------------------------------------------------------------------------------


def _random_thread(rng: random.Random) -> list[ThreadEvent]:
    events: list[ThreadEvent] = []
    at = 0.0
    open_calls: list[str] = []
    for n in range(rng.randint(0, 40)):
        at += rng.choice([0, 1, 5, 30, 200, 599, 600, 601, 1800, 4000])
        kind = rng.choice([PROMPT, MODEL, MODEL, TOOL_START, TOOL_END, TURN_END])
        human, child, decided = (rng.random() < 0.1 for _ in range(3))
        if kind == TOOL_START:
            open_calls.append(f"c{n}")
            events.append(ev(kind, at, f"c{n}", human_wait=human, subagent_wait=child, user_decided=decided))
        elif kind == TOOL_END and open_calls:
            call = open_calls.pop(rng.randrange(len(open_calls)))
            events.append(ev(kind, at, call, human_wait=human, subagent_wait=child, user_decided=decided))
        else:
            events.append(ev(kind if kind != TOOL_END else MODEL, at))
    return events


def test_properties_on_random_threads() -> None:
    rng = random.Random(20261105)
    for _ in range(400):
        threads = [_random_thread(rng) for _ in range(3)]
        sets = [active_set(t) for t in threads]
        for spans in sets:
            # Merged, sorted, positive, not touching.
            assert all(b > a for a, b in spans)
            assert all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1))
        agent = sum(total_seconds(s) for s in sets)
        wall = total_seconds(merge_spans(s for spans in sets for s in spans))
        assert wall <= agent + 1e-6
        # Adding an event never makes the active set shorter than nothing, and the pieces add up.
        for thread, spans in zip(threads, sets, strict=True):
            times = sorted({e.timestamp.timestamp() for e in thread})
            pieces = cut_pieces(spans, times)
            assert abs(total_seconds(pieces) - total_seconds(spans)) < 1e-6


# -- set operations --------------------------------------------------------------------------------


def test_merge_joins_overlapping_and_touching_spans() -> None:
    assert merge_spans([(5, 6), (0, 2), (2, 3), (1, 1.5), (7, 7)]) == [(0, 3), (5, 6)]


def test_subtract_cuts_holes() -> None:
    assert subtract_spans([(0, 10), (20, 30)], [(2, 3), (8, 22), (29, 40)]) == [(0, 2), (3, 8), (22, 29)]
    assert subtract_spans([(0, 10)], []) == [(0, 10)]
    assert subtract_spans([(0, 10)], [(0, 10)]) == []


# -- pieces and the owner rule ---------------------------------------------------------------------


def test_pieces_are_cut_at_every_event() -> None:
    assert cut_pieces([(0, 10)], [0, 3, 7, 10, 12]) == [(0, 3), (3, 7), (7, 10)]


def test_owner_prefers_the_next_request_in_the_same_turn() -> None:
    # Requests at 10 and 50; a piece 20..30 in the same turn goes to the request at 50 that ends it.
    assert owner((20, 30), [10, 50], []) == 1


def test_owner_falls_back_to_the_last_request_before_when_the_turn_ends_first() -> None:
    # A prompt at 40 ends the turn before the request at 50: the piece goes back to the request at 10.
    assert owner((20, 30), [10, 50], [40]) == 0


def test_owner_takes_the_request_at_the_turns_closing_boundary() -> None:
    # The final answer at 30 and turn_end at 30: the request still belongs to the turn.
    assert owner((20, 30), [30], [30]) == 0


def test_a_piece_without_any_request_is_unplaced() -> None:
    assert owner((20, 30), [], []) is None
    assert owner((20, 30), [50], [40]) is None


def test_attribute_gives_every_piece_to_one_request() -> None:
    # prompt 0, request 10, tool 10..400, request 410, prompt 1000, request 1010.
    events = [
        ev(PROMPT, 0),
        ev(MODEL, 10),
        ev(TOOL_START, 10, "a"),
        ev(TOOL_END, 400, "a"),
        ev(MODEL, 410),
        ev(PROMPT, 1000),
        ev(MODEL, 1010),
    ]
    usage = [BASE + 410, BASE + 10, BASE + 1010]  # unsorted on purpose
    result = attribute(events, usage)
    by_request: dict[int, float] = {}
    for index, (a, b) in result.owned:
        by_request[index] = by_request.get(index, 0) + b - a
    # 0..10 -> request 10 (index 1); 10..400 and 400..410 -> request 410 (index 0); 1000..1010 -> index 2.
    assert by_request == {1: 10, 0: 400, 2: 10}
    assert result.unplaced == 0 and result.active == 420


def test_time_after_the_last_request_goes_back_to_it() -> None:
    # The last request at 10, a tool run 10..100 and its result: no later request, so the request at 10.
    events = [ev(PROMPT, 0), ev(MODEL, 10), ev(TOOL_START, 10, "a"), ev(TOOL_END, 100, "a")]
    result = attribute(events, [BASE + 10])
    assert sum(b - a for _, (a, b) in result.owned) == 100 and result.unplaced == 0


def test_a_thread_without_requests_is_unplaced() -> None:
    result = attribute([ev(PROMPT, 0), ev(MODEL, 30)], [])
    assert result.owned == [] and result.unplaced == 30


def _usage(at: float, thread: str | None = None, session: str = "s1") -> UsageEvent:
    return UsageEvent(
        tool="claude-code",
        session_id=session,
        model="m",
        timestamp=T0 + timedelta(seconds=at),
        tokens=TokenCounts(input=1),
        thread=thread,
    )


def test_attribute_threads_keeps_threads_apart() -> None:
    # Main thread 0..60 and a subagent 0..30 at the same time: each piece goes to its own thread's request.
    events = [ev(PROMPT, 0), ev(MODEL, 60), ev(PROMPT, 0, thread="agent-x"), ev(MODEL, 30, thread="agent-x")]
    usage = [_usage(60), _usage(30, "agent-x"), _usage(5, session="other")]
    owned = attribute_threads(events, usage)
    assert {i: total_seconds(p) for i, p in owned.pieces.items()} == {0: 60, 1: 30}
    assert owned.measured_sessions == {("claude-code", "s1")}
    assert owned.unplaced == {}


def test_thread_events_are_counted_once_by_key() -> None:
    a = ThreadEvent("claude-code", "s1", None, T0, PROMPT, key="u1|prompt|")
    b = ThreadEvent("claude-code", "s2", None, T0, PROMPT, key="u1|prompt|")
    c = ThreadEvent("claude-code", "s1", None, T0, PROMPT)
    assert deduplicate_thread_events([a, b, c, c]) == [a, c, c]


# -- spans on the wire -----------------------------------------------------------------------------


WIDE = (0.0, 4e9)


def test_wire_spans_round_to_whole_seconds_and_merge_what_then_touches() -> None:
    # 10.4..20.5 -> 10..21; 20.6..30.2 -> 21..30 touches it and is joined; 40.2..40.4 becomes empty.
    assert wire_spans([(10.4, 20.5), (20.6, 30.2), (40.2, 40.4)], 20, WIDE) == [(10, 30)]


def test_wire_spans_are_clipped_to_the_window() -> None:
    assert wire_spans([(0, 100), (150, 300)], 250, (50.0, 200.0)) == [(50, 100), (150, 200)]


def test_wire_spans_refuse_a_union_longer_than_the_sum() -> None:
    assert wire_spans([(0, 100)], 10, WIDE) is None
    # The allowance: 2 per span plus 1.
    assert wire_spans([(0, 100)], 97, WIDE) == [(0, 100)]
    assert length_allowed([(0, 100)], 97) and not length_allowed([(0, 100)], 96)


def test_more_than_1000_spans_join_close_ones_first() -> None:
    # 1500 spans of 10 s, 30 s apart: joining 500 gaps adds 15000 s, allowed only with that much active time.
    spans = [(i * 40, i * 40 + 10) for i in range(1500)]
    generous = limit_spans(spans, active_seconds=15000 + 15000)
    assert len(generous) == 1000
    assert sum(b - a for a, b in generous) == 15000 + 500 * 30
    assert length_allowed(generous, 30000)


def test_more_than_1000_spans_drop_the_shortest_when_joining_breaks_the_length_rule() -> None:
    # Spans of growing length, 30 s apart, active seconds exactly their sum: no join fits, so the
    # 200 shortest are dropped and the 1000 longest stay, in order.
    spans = []
    at = 0
    for i in range(1200):
        spans.append((at, at + 5 + i))
        at += 5 + i + 30
    total = sum(b - a for a, b in spans)
    limited = limit_spans(spans, active_seconds=total - 2 * 1200)
    assert len(limited) == 1000
    assert limited == spans[200:]


def test_spans_far_apart_are_dropped_not_joined() -> None:
    spans = [(i * 1000, i * 1000 + (i % 7) + 1) for i in range(1001)]
    total = sum(b - a for a, b in spans)
    limited = limit_spans(spans, total)
    assert len(limited) == 1000 and len(set(limited)) == 1000
    # One of the shortest (1 s, every seventh) is gone: of equal ones the latest goes.
    assert limited == [s for i, s in enumerate(spans) if i != 994]


def test_a_thousand_spans_are_left_alone() -> None:
    spans = [(i * 40, i * 40 + 10) for i in range(1000)]
    assert limit_spans(spans, 10000) == spans
