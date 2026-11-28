"""Active time from Codex rollouts, and subagent rollouts reported under their parent session.

The fixtures are synthetic (tests/fixtures/codex_time_home.py); expected seconds are worked out by hand
in the comments.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, Registry, parse_all
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.config import Config
from ai_usage_reporter.model import MODEL, PROMPT, TOOL_END, TOOL_START, TURN_END, ThreadEvent, UsageEvent
from ai_usage_reporter.pipeline import Collected, Selection, collect, deliver
from ai_usage_reporter.record import ACTIVE_TIME_FIELDS, OPTIONAL_FIELDS, opaque_session_id
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.state import FileIdentity, State
from ai_usage_reporter.testing import validate_records
from tests.conftest import FakeServer
from tests.fixtures import codex_time_home
from tests.fixtures.codex_time_home import CHILD_MARKED, CHILD_UNMARKED, PARENT, Rollout, at

NOW = datetime(2026, 11, 7, tzinfo=UTC)
SESSION = "c0dec0de-0000-4000-8000-0000000000aa"
CANARY = "CANARY-PROMPT-5c1e"


def run(home: Path, *, state: State | None = None, selection: Selection | None = None) -> Collected:
    adapter = CodexAdapter()
    return collect(
        Config(),
        Registry(adapters={adapter.name: adapter}),
        selection=selection,
        state=state,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
        now=NOW,
    )


def wires(home: Path) -> list[dict[str, Any]]:
    records = [r.to_wire() for r in run(home).records]
    validate_records(records)
    return records


def events_of(home: Path) -> list[UsageEvent | ThreadEvent]:
    adapter = CodexAdapter()
    out: list[UsageEvent | ThreadEvent] = []
    for src in adapter.discover(DiscoveryContext(home=home, env={})):
        out.extend(parse_all(adapter, src))
    return out


def thread_events(home: Path) -> list[ThreadEvent]:
    return [e for e in events_of(home) if isinstance(e, ThreadEvent)]


def by_issue(records: list[dict[str, Any]]) -> dict[str | None, dict[str, Any]]:
    return {r.get("attribution", {}).get("issue_ref"): r for r in records}


def one(records: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(records) == 1, records
    return records[0]


# -- the adapter's events ----------------------------------------------------------------------------


def basic_turn(home: Path) -> Rollout:
    """A turn with reasoning, a command, an MCP call and a file change.

    prompt 08:00:00, reasoning 08:00:01..08:00:20, command 08:00:20..08:01:20, request 08:01:25,
    MCP call 08:01:30..08:01:31, a repeated token count 08:01:35 (no request), file change
    08:01:40..08:01:45, message 08:01:50..08:02:00, request 08:02:01, end 08:02:02. Every gap is short
    and nothing crosses a boundary: 08:00:00..08:02:02 = 122 s.
    """
    r = Rollout(SESSION).meta("07:59:58").prompt("08:00:00")
    r.reasoning("08:00:01", "08:00:20").command("08:00:20", "08:01:20").usage("08:01:25")
    r.mcp("08:01:30", "08:01:31", "specivo_show_issue", {"issue_ref": "ACME-1"}).usage("08:01:35", repeat=True)
    r.file_change("08:01:40", "08:01:45").message("08:01:50", "08:02:00").usage("08:02:01", 500, 50)
    r.end("08:02:02")
    r.write(home)
    return r


def test_a_turn_yields_the_events_the_definition_lists(tmp_path: Path) -> None:
    basic_turn(tmp_path)
    events = thread_events(tmp_path)
    assert [e.kind for e in events] == [
        PROMPT,
        MODEL,  # reasoning start
        MODEL,  # reasoning end
        TOOL_START,  # the command
        TOOL_END,
        TOOL_START,  # the MCP call
        TOOL_END,
        TOOL_START,  # the file change
        TOOL_END,
        MODEL,  # message start
        MODEL,  # message end
        TURN_END,
    ]
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)
    assert events[3].timestamp == at("08:00:20") and events[4].timestamp == at("08:01:20")
    assert events[3].call_id == events[4].call_id and events[3].call_id is not None
    assert all(e.session_id == SESSION and e.thread_id is None and e.tool == "codex" for e in events)
    assert len({e.key for e in events}) == len(events) and all(e.key for e in events)
    assert not any(e.human_wait or e.subagent_wait or e.user_decided for e in events)


def test_a_turn_gives_its_record_the_seconds_worked_out_by_hand(tmp_path: Path) -> None:
    basic_turn(tmp_path)
    record = one(wires(tmp_path))
    assert record["active_time_version"] == 1
    assert record["active_seconds"] == 122
    assert record["active_spans"] == [["2026-11-05T08:00:00Z", "2026-11-05T08:02:02Z"]]
    assert record["request_count"] == 2  # the repeated token count is no request


def test_items_are_put_at_their_own_time(tmp_path: Path) -> None:
    # A long command logged after a short message that started later: the events are sorted.
    r = Rollout(SESSION).meta("08:59:59").prompt("09:00:00")
    r.command("09:00:01", "09:03:00").reasoning("09:00:02", "09:00:05")
    r.lines.append(r.lines.pop(-2))  # the command's line is written after the reasoning's
    r.usage("09:03:01").end("09:03:02")
    r.write(tmp_path)
    events = thread_events(tmp_path)
    assert [e.kind for e in events] == [PROMPT, TOOL_START, MODEL, MODEL, TOOL_END, TURN_END]
    assert one(wires(tmp_path))["active_seconds"] == 182


def test_a_61_minute_command_is_capped(tmp_path: Path) -> None:
    # prompt 09:00:00, reasoning ..09:00:10 (10 s), command 09:00:10..10:01:10 counts its first hour
    # (3600 s; the 61 minutes between its two events are no short gap), request 10:01:15, end 10:01:20:
    # 10:01:10..10:01:20 (10 s). Total 3620.
    r = Rollout(SESSION).meta("08:59:59").prompt("09:00:00").reasoning("09:00:00", "09:00:10")
    r.command("09:00:10", "10:01:10").usage("10:01:15").end("10:01:20")
    r.write(tmp_path)
    record = one(wires(tmp_path))
    assert record["active_seconds"] == 10 + 3600 + 10
    assert record["active_spans"] == [
        ["2026-11-05T09:00:00Z", "2026-11-05T10:00:10Z"],
        ["2026-11-05T10:01:10Z", "2026-11-05T10:01:20Z"],
    ]


def test_a_turn_aborted_mid_tool(tmp_path: Path) -> None:
    # prompt 11:00:00, reasoning ..11:00:30, request 11:00:29; a command from 11:00:30 is interrupted:
    # the turn is aborted at 11:02:00 and the command's item completes at 11:02:30. Its run is cut at the
    # turn's end; the gaps after the abort (to its end, and on to the next prompt at 11:03:00) are never
    # added: 120 s. Next turn 11:03:00..11:03:22 (reasoning ..11:03:20, request 11:03:21): 22 s.
    r = Rollout(SESSION).meta("10:59:59").prompt("11:00:00").reasoning("11:00:00", "11:00:30").usage("11:00:29")
    r.end("11:02:00", aborted=True)
    r.command("11:00:30", "11:02:30", status="failed")
    r.prompt("11:03:00").reasoning("11:03:00", "11:03:20").usage("11:03:21").end("11:03:22")
    r.write(tmp_path)
    kinds = [e.kind for e in thread_events(tmp_path)]
    assert kinds[:5] == [PROMPT, MODEL, MODEL, TOOL_START, TURN_END]
    record = one(wires(tmp_path))
    assert record["active_seconds"] == 120 + 22


def test_an_aborted_turn_without_completed_at_uses_the_line_time(tmp_path: Path) -> None:
    # The command never completed (no item): prompt 11:00:00 .. reasoning end 11:00:30 .. abort written
    # at 11:01:00 with completed_at null: 60 s.
    r = Rollout(SESSION).meta("10:59:59").prompt("11:00:00").reasoning("11:00:00", "11:00:30").usage("11:00:29")
    r.end("11:01:00", aborted=True, no_time=True)
    r.write(tmp_path)
    assert one(wires(tmp_path))["active_seconds"] == 60


def test_two_turns_with_a_long_pause(tmp_path: Path) -> None:
    # Turn 1: 12:00:00..12:01:00 (60 s). The 39 minutes between the turns count nothing. Turn 2:
    # 12:40:00..12:40:40 (40 s).
    r = Rollout(SESSION).meta("11:59:59").prompt("12:00:00").reasoning("12:00:00", "12:00:50").usage("12:00:55")
    r.end("12:01:00")
    r.prompt("12:40:00").message("12:40:00", "12:40:30").usage("12:40:31").end("12:40:40")
    r.write(tmp_path)
    record = one(wires(tmp_path))
    assert record["active_seconds"] == 100
    assert record["active_spans"] == [
        ["2026-11-05T12:00:00Z", "2026-11-05T12:01:00Z"],
        ["2026-11-05T12:40:00Z", "2026-11-05T12:40:40Z"],
    ]


def test_whole_second_turn_times_take_the_line_time(tmp_path: Path) -> None:
    # task_complete says 08:00:10 (whole seconds) and its line is written at 08:00:10.800; the last item
    # ended at 08:00:10.700. The turn ends at the line's time, so the item's last 0.7 s are not cut:
    # 08:00:00.200..08:00:10.800 = 10.6 s, sent as 11.
    r = Rollout(SESSION).meta("07:59:59").prompt("08:00:00", exact="08:00:00.200")
    r.message("08:00:00.300", "08:00:10.700").usage("08:00:10.750").end("08:00:10", exact="08:00:10.800")
    r.write(tmp_path)
    events = thread_events(tmp_path)
    assert events[0].timestamp == at("08:00:00.200") and events[-1].timestamp == at("08:00:10.800")
    assert one(wires(tmp_path))["active_seconds"] == 11


def test_a_turn_time_far_from_its_line_keeps_the_payload_time(tmp_path: Path) -> None:
    r = Rollout(SESSION).meta("07:59:59").prompt("08:00:00", exact="08:00:05").message("08:00:00", "08:00:30")
    r.usage("08:00:31").end("08:00:40")
    r.write(tmp_path)
    assert thread_events(tmp_path)[0].timestamp == at("08:00:00")


def test_a_null_turn_attribution_is_still_a_prompt(tmp_path: Path) -> None:
    r = Rollout(SESSION).meta("07:59:59").prompt("08:00:00", trigger=None).message("08:00:00", "08:00:20")
    r.usage("08:00:21").end("08:00:22")
    r.write(tmp_path)
    assert thread_events(tmp_path)[0].kind == PROMPT
    assert one(wires(tmp_path))["active_seconds"] == 22


def test_wait_tools_are_classified(tmp_path: Path) -> None:
    # prompt 14:00:00, reasoning ..14:00:10, a user-input request 14:00:10..14:05:10 (a person answering:
    # removed although the gap is short), reasoning 14:05:10..14:05:20, a collaboration wait
    # 14:05:20..14:06:20 (nothing as a tool run, but its short gap counts: 60 s), a spawn 14:06:20..14:06:21
    # (a plain tool run), request 14:06:25, end 14:06:26. Total 10 + 10 + 60 + 6 = 86.
    r = Rollout(SESSION).meta("13:59:59").prompt("14:00:00").reasoning("14:00:00", "14:00:10")
    r.item("14:00:10", "14:05:10", "RequestUserInput", item_id="ask-1", questions=[CANARY])
    r.reasoning("14:05:10", "14:05:20").wait("14:05:20", "14:06:20", tool="wait")
    r.wait("14:06:20", "14:06:21", tool="spawn_agent").usage("14:06:25").end("14:06:26")
    r.write(tmp_path)
    calls = [e for e in thread_events(tmp_path) if e.call_id]
    human = [e for e in calls if e.human_wait]
    waits = [e for e in calls if e.subagent_wait]
    assert [e.call_id for e in human] == ["ask-1", "ask-1"]
    assert len(waits) == 2 and len({e.call_id for e in waits}) == 1
    record = one(wires(tmp_path))
    assert record["active_seconds"] == 86
    assert CANARY not in json.dumps(record)


def test_a_user_input_tool_by_name_waits_for_a_person(tmp_path: Path) -> None:
    r = Rollout(SESSION).meta("13:59:59").prompt("14:00:00")
    r.mcp("14:00:00", "14:03:00", "request_user_input", {"q": "CANARY-PROMPT-5c1e"}).usage("14:03:01").end("14:03:02")
    r.write(tmp_path)
    assert all(e.human_wait for e in thread_events(tmp_path) if e.call_id)
    assert one(wires(tmp_path))["active_seconds"] == 2


def test_items_that_are_no_events(tmp_path: Path) -> None:
    r = Rollout(SESSION).meta("07:59:59").prompt("08:00:00")
    r.item("08:00:00", "08:00:01", "UserMessage", content="CANARY-PROMPT-5c1e")
    r.item("08:00:02", "08:00:03", "ContextCompaction")
    r.item("08:00:04", "08:00:05", "SubAgentActivity", kind="started")
    r.item("08:00:06", "08:00:07", "SomethingNew")
    r.usage("08:00:08").end("08:00:09")
    r.write(tmp_path)
    assert [e.kind for e in thread_events(tmp_path)] == [PROMPT, TURN_END]


def test_malformed_items_and_turns(tmp_path: Path) -> None:
    r = Rollout(SESSION).meta("07:59:59").prompt("08:00:00").message("08:00:00", "08:00:10")
    r.line(at("08:00:11"), "event_msg", {"type": "item_completed", "turn_id": r.turn, "item": "not an object"})
    no_id = r.line(at("08:00:12"), "event_msg", {"type": "item_completed", "turn_id": r.turn, "item": {}})
    no_id["payload"]["item"] = {"type": "CommandExecution", "command": "CANARY-COMMAND-3b8d"}
    r.line(at("08:00:13"), "event_msg", {"type": "task_started", "turn_id": "t-x", "started_at": "soon"})
    del r.lines[-1]["timestamp"]
    r.usage("08:00:14").end("08:00:15")
    r.write(tmp_path)
    assert [e.kind for e in thread_events(tmp_path)] == [PROMPT, MODEL, MODEL, TURN_END]


def test_an_old_format_rollout_yields_no_thread_events_and_the_same_tokens(tmp_path: Path) -> None:
    codex_time_home.old_format(tmp_path)
    events = events_of(tmp_path)
    assert not any(isinstance(e, ThreadEvent) for e in events)
    record = one(wires(tmp_path))
    assert not set(ACTIVE_TIME_FIELDS) & set(record)
    assert record["tokens"] == {"input": 700, "output": 70}


def test_parse_keeps_yielding_usage_only(tmp_path: Path) -> None:
    codex_time_home.build(tmp_path)
    adapter = CodexAdapter()
    for src in adapter.discover(DiscoveryContext(home=tmp_path, env={})):
        assert all(isinstance(e, UsageEvent) for e in adapter.parse(src))


# -- subagent rollouts -------------------------------------------------------------------------------


def test_subagent_rollouts_are_threads_of_the_parent_session(tmp_path: Path) -> None:
    codex_time_home.session_with_subagents(tmp_path)
    events = events_of(tmp_path)
    assert {e.session_id for e in events} == {PARENT}
    usage = [e for e in events if isinstance(e, UsageEvent)]
    assert {e.sub_session_id for e in usage} == {None, CHILD_UNMARKED, CHILD_MARKED}
    threads = [e for e in events if isinstance(e, ThreadEvent)]
    assert {e.thread_id for e in threads} == {None, CHILD_UNMARKED, CHILD_MARKED}
    # The child's copy of the parent's history yields nothing: no events at the parent's earlier times,
    # and no copy of the parent's old-form mark.
    for child in (CHILD_UNMARKED, CHILD_MARKED):
        own = [e for e in threads if e.thread_id == child]
        assert min(e.timestamp for e in own) >= at("13:01:00")
        assert {e.issue_mark for e in usage if e.sub_session_id == child} <= {None, "ACME-41"}
    marks = {(e.issue_mark, e.sub_session_id) for e in usage if e.is_marker}
    assert marks == {("ACME-99", None), ("ACME-40", None), ("ACME-41", CHILD_MARKED)}
    # Discovery still yields one file per rollout.
    sources = list(CodexAdapter().discover(DiscoveryContext(home=tmp_path, env={})))
    assert len(sources) == 3


def test_subagent_time_and_tokens_take_the_right_issue(tmp_path: Path) -> None:
    codex_time_home.session_with_subagents(tmp_path)
    records = by_issue(wires(tmp_path))
    assert set(records) == {"ACME-99", "ACME-40", "ACME-41"}
    assert {r["session_id"] for r in records.values()} == {opaque_session_id(PARENT)}
    # ACME-99: the parent's earlier turns, 31 + 10 s.
    assert records["ACME-99"]["active_seconds"] == 41
    # ACME-40: the parent's 22 s and the unmarked subagent's 252 s (it takes the parent's issue at its start).
    assert records["ACME-40"]["active_seconds"] == 22 + 252
    assert records["ACME-40"]["tokens"] == {"input": 1000 + 2000 + 300 + 400, "output": 100 + 200 + 30 + 40}
    assert records["ACME-40"]["active_spans"] == [
        ["2026-11-05T13:00:00Z", "2026-11-05T13:00:10Z"],
        ["2026-11-05T13:01:00Z", "2026-11-05T13:05:12Z"],
        ["2026-11-05T13:20:10Z", "2026-11-05T13:20:22Z"],
    ]
    # ACME-41: the subagent that marked it, 62 s.
    assert records["ACME-41"]["active_seconds"] == 62
    assert records["ACME-41"]["tokens"] == {"input": 500, "output": 50}


@pytest.mark.parametrize("child", [CHILD_UNMARKED, CHILD_MARKED])
def test_push_session_child_selects_the_parent(tmp_path: Path, child: str) -> None:
    codex_time_home.session_with_subagents(tmp_path)
    codex_time_home.old_format(tmp_path)
    picked = run(tmp_path, selection=Selection(session=child)).records
    assert {r.session_id for r in picked} == {PARENT}
    assert len(picked) == 3


def test_a_child_without_a_history_ordinal_or_root_id_still_folds_into_its_parent(tmp_path: Path) -> None:
    parent = Rollout(PARENT).meta("12:59:59").prompt("13:00:00").message("13:00:00", "13:00:10")
    parent.usage("13:00:11").end("13:00:12")
    child = Rollout(CHILD_UNMARKED).meta("13:00:01", parent=PARENT)
    del child.lines[0]["payload"]["session_id"]
    child.prompt("13:00:01").message("13:00:01", "13:00:21").usage("13:00:22").end("13:00:23")
    parent.write(tmp_path)
    child.write(tmp_path)
    record = one(wires(tmp_path))
    assert record["session_id"] == opaque_session_id(PARENT)
    assert record["active_seconds"] == 12 + 22


def test_previously_pushed_child_records_are_cleared(tmp_path: Path) -> None:
    # Up to now the child's rollout was reported in the parent session as if it were the main thread
    # (the copied parent session_meta, the second in the file, named the session), so the marked child's
    # mark moved the parent's later requests to ACME-41. Those keys are in the state database; the next
    # push sends the new split and clears the keys it no longer has.
    codex_time_home.session_with_subagents(tmp_path)
    server = FakeServer()
    server.info["optional_fields"] = list(OPTIONAL_FIELDS)
    sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)

    class OldCodex(CodexAdapter):
        """The reporter before subagent rollouts: no threads, no sub-session, no active time."""

        parser_version = "codex/4"

        def parse_events(self, src):  # type: ignore[no-untyped-def]
            for event in super().parse_events(src):
                if isinstance(event, UsageEvent):
                    yield replace(event, sub_session_id=None)

    with State.in_memory() as state:
        old = OldCodex()
        before = collect(
            Config(),
            Registry(adapters={old.name: old}),
            state=state,
            context_for=lambda a, roots: DiscoveryContext(home=tmp_path, env={}, roots=roots),
            git=None,
            now=NOW,
        ).records
        old_issues = {r.to_wire().get("attribution", {}).get("issue_ref") for r in before}
        assert old_issues == {"ACME-99", "ACME-40", "ACME-41"}
        deliver(before, sink, state)
        old_keys = {r.idempotency_key: r for r in before}
        after = run(tmp_path, state=state).records
        new_keys = {r.idempotency_key for r in after if r.tokens.total}
        cleared = [r for r in after if r.idempotency_key in old_keys and r.idempotency_key not in new_keys]
        # Every old key is either sent again with the new totals or cleared to zero. The parent's later
        # requests no longer sit under ACME-41, so that key of the parent's model is cleared.
        assert set(old_keys) <= {r.idempotency_key for r in after}
        assert [(r.model, r.tokens.total) for r in cleared] == [(codex_time_home.MODEL, 0)]
        assert old_keys[cleared[0].idempotency_key].to_wire()["attribution"]["issue_ref"] == "ACME-41"
        summary = deliver(after, sink, state)
        assert summary.sent >= 1
    # What the server now holds per key adds up to the new split.
    latest: dict[str, dict[str, Any]] = {}
    for batch in server.posts:
        for record in batch:
            latest[record["idempotency_key"]] = record
    held = sum(r["tokens"].get("input", 0) for r in latest.values())
    assert held == 100 + 1000 + 2000 + 300 + 400 + 500
    acme41 = [r for r in latest.values() if r.get("attribution", {}).get("issue_ref") == "ACME-41" and r["tokens"]]
    assert sum(r["tokens"]["input"] for r in acme41) == 500


def test_the_parse_cache_keeps_codex_thread_events(tmp_path: Path) -> None:
    home = codex_time_home.build(tmp_path / "home")
    with State(tmp_path / "state.db") as state:
        first = [r.to_wire() for r in run(home, state=state).records]
        again = run(home, state=state)
        assert all(report.parsed == 0 for report in again.reports.values())
        assert [r.to_wire() for r in again.records] == first
        (path,) = [p for p in (home / ".codex").rglob("*.jsonl") if CHILD_UNMARKED in p.name]
        cached = state.cached_events(path, FileIdentity.of(path), CodexAdapter.parser_version)
        assert cached is not None and any(isinstance(e, ThreadEvent) and e.thread_id for e in cached)


def test_a_copied_rollout_adds_no_time(tmp_path: Path) -> None:
    home = codex_time_home.build(tmp_path / "home")
    before = wires(home)
    (path,) = [p for p in (home / ".codex").rglob("*.jsonl") if CHILD_UNMARKED in p.name]
    # The same rollout archived, and a copy under another session's name with the same lines.
    archived = home / ".codex" / "archived_sessions"
    archived.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, archived / path.name)
    assert wires(home) == before
    assert any(r["session_id"] == opaque_session_id(PARENT) for r in before)
