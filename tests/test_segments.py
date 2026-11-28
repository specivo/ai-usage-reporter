"""Issue segments: the issues an agent marked with its Specivo calls split a session's records."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.adapters.opencode import OpenCodeAdapter
from ai_usage_reporter.config import Config, parse_config
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call, specivo_tool
from ai_usage_reporter.model import TokenCounts, UsageEvent
from ai_usage_reporter.pipeline import Collected, collect, deliver
from ai_usage_reporter.record import idempotency_key, segment_key
from ai_usage_reporter.segments import segment_issues
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.state import SENT, State
from tests.fixtures.agent_log_home import QUIET, SESSION, Transcript, build, projects_dir
from tests.fixtures.opencode_home import V2_SCHEMA, ms

NOW = datetime(2026, 10, 8, tzinfo=UTC)
DAY = date(2026, 10, 5)


def _collect(home: Path, config: Config | None = None, adapter: Any = None, **kwargs: Any) -> Collected:
    adapter = adapter or ClaudeCodeAdapter()
    return collect(
        config or Config(),
        Registry(adapters={adapter.name: adapter}),
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        now=NOW,
        **kwargs,
    )


def _wires(home: Path, config: Config | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    return [r.to_wire() for r in _collect(home, config, **kwargs).records]


def _issue(wire: dict[str, Any]) -> str | None:
    value: str | None = (wire.get("attribution") or {}).get("issue_ref")
    return value


def _by_issue(wires: list[dict[str, Any]], session: str = SESSION) -> dict[str | None, dict[str, Any]]:
    return {_issue(w): w for w in wires if w["session_id"] == session}


def _legacy(session: str = SESSION, tool: str = "claude-code", model: str = "claude-sonnet-4-5") -> str:
    return idempotency_key(tool, session, model, DAY)


# -- reading the marks -------------------------------------------------------------------


def test_marking_calls() -> None:
    assert specivo_tool("mcp__specivo__specivo_update_issue") == "update_issue"
    assert specivo_tool("mcp__any_server__specivo_timer") == "timer"
    assert specivo_tool("specivo.specivo_timer") == "timer"
    assert specivo_tool("mcp__x__notspecivo_timer") is None
    update = "mcp__s__specivo_update_issue"
    assert marking_call(update, {"issue_ref": "acme-12", "status_id": 2}) == "ACME-12"
    assert marking_call(update, {"issue_ref": "ACME-12", "assigned_to_id": 3}) == "ACME-12"
    assert marking_call(update, {"issue_ref": "ACME-12", "notes": "only a note"}) is None
    assert marking_call(update, '{"issue_ref": "ACME-7", "status_id": 3}') == "ACME-7"  # Codex: JSON text
    assert marking_call(update, {"issue_ref": "not a key", "status_id": 2}) is None
    assert marking_call("specivo_timer", {"op": "start", "issue_ref": "ACME-3"}) == "ACME-3"
    assert marking_call("specivo_timer", {"op": "stop", "issue_ref": "ACME-3"}) is None
    assert marking_call("mcp__s__specivo_create_issue", {"subject": "x"}) == CREATE
    for other in ("specivo_show_issue", "specivo_issue_development", "specivo_add_comment", "Bash"):
        assert marking_call(other, {"issue_ref": "ACME-3", "status_id": 2}) is None


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"result":"Created issue ACME-12: Login"}', "ACME-12"),
        ("Created issue acme-7: Login", "ACME-7"),
        ([{"type": "text", "text": "Created issue ACME-9"}], "ACME-9"),
        ("Error: could not create. Created issue ACME-1", None),
        ("Created issue ACME-12x", None),
        ('{"result": 42}', None),
        (None, None),
    ],
)
def test_created_issue_key_comes_from_the_start_of_the_result(content: Any, expected: str | None) -> None:
    assert created_issue(content) == expected


# -- segments ------------------------------------------------------------------------------


def _usage(ts: str, thread: str | None = None, session: str = "s1") -> UsageEvent:
    return UsageEvent(
        tool="claude-code",
        session_id=session,
        model="m",
        timestamp=datetime.fromisoformat(ts),
        tokens=TokenCounts(input=1),
        thread=thread,
    )


def _mark(ts: str, issue: str, thread: str | None = None, session: str = "s1") -> UsageEvent:
    return UsageEvent(
        tool="claude-code",
        session_id=session,
        model="",
        timestamp=datetime.fromisoformat(ts),
        tokens=TokenCounts(),
        requests=0,
        thread=thread,
        issue_mark=issue,
    )


def test_segment_rules() -> None:
    usage = [
        _usage("2026-10-05T08:00:00+00:00"),  # before the first mark: the first marked issue
        _usage("2026-10-05T09:00:00+00:00"),  # at the mark: its issue
        _usage("2026-10-05T12:00:00+00:00"),
        _usage("2026-10-05T10:30:00+00:00", thread="sub"),  # no marks: main's issue when it started
        _usage("2026-10-05T12:30:00+00:00", thread="sub"),
        _usage("2026-10-05T10:00:00+00:00", thread="own"),  # before its own first mark
        _usage("2026-10-05T13:00:00+00:00", thread="own"),
        _usage("2026-10-05T10:00:00+00:00", session="s2"),  # no marks at all
    ]
    marks = [
        _mark("2026-10-05T09:00:00+00:00", "ACME-1"),
        _mark("2026-10-05T11:00:00+00:00", "ACME-2"),
        _mark("2026-10-05T11:00:00+00:00", "ACME-2"),  # the same mark copied in another file
        _mark("2026-10-05T10:05:00+00:00", "ACME-5", thread="own"),
        _mark("2026-10-05T12:45:00+00:00", "ACME-6", thread="own"),
    ]
    assert segment_issues(usage, marks) == [
        "ACME-1",
        "ACME-1",
        "ACME-2",
        "ACME-1",
        "ACME-1",
        "ACME-5",
        "ACME-6",
        None,
    ]
    assert segment_issues(usage, []) == [None] * len(usage)


# -- records ------------------------------------------------------------------------------


def test_a_day_on_several_issues_becomes_one_record_per_issue(tmp_path: Path) -> None:
    wires = _wires(build(tmp_path))
    segments = _by_issue(wires)
    assert sorted(segments, key=str) == ["ACME-12", "ACME-13", "ACME-14", "ACME-20"]
    # Research before the first mark, the main thread until ACME-13, and the subagent without marks.
    assert segments["ACME-12"]["tokens"]["input"] == 400 + 50 + 50 + 300 + 70 + 30
    # The created issue's key comes from the tool result; the call itself already counts for it.
    assert segments["ACME-13"]["tokens"]["input"] == 50 + 200
    assert segments["ACME-14"]["tokens"]["input"] == 50 + 100
    # A subagent that marked its own issue: all its usage, before and after the mark.
    assert segments["ACME-20"]["tokens"]["input"] == 60 + 50 + 40
    assert all(w["attribution"]["issue_ref_source"] == "agent_log" for w in segments.values())


def test_the_segment_with_the_days_first_request_keeps_the_legacy_key(tmp_path: Path) -> None:
    segments = _by_issue(_wires(build(tmp_path)))
    legacy = _legacy()
    assert segments["ACME-12"]["idempotency_key"] == legacy
    for issue in ("ACME-13", "ACME-14", "ACME-20"):
        assert segments[issue]["idempotency_key"] == segment_key(legacy, issue)
    assert len({w["idempotency_key"] for w in segments.values()}) == 4


def test_a_session_without_marks_is_unchanged(tmp_path: Path) -> None:
    quiet = _by_issue(_wires(build(tmp_path)), QUIET)
    # add_comment, timer stop and an update without status or assignee mark nothing.
    assert list(quiet) == [None]
    assert quiet[None]["idempotency_key"] == _legacy(QUIET)


def test_totals_do_not_depend_on_segments(tmp_path: Path) -> None:
    home = build(tmp_path)

    def totals(wires: list[dict[str, Any]]) -> tuple[int, int]:
        return sum(w["tokens"]["input"] for w in wires), sum(w["request_count"] for w in wires)

    on = _wires(home)
    off = _wires(home, Config(agent_log_issues=False))
    assert totals(on) == totals(off)
    assert len(off) == 2 and not any(_issue(w) for w in off)
    assert {w["idempotency_key"] for w in off} == {_legacy(), _legacy(QUIET)}


def test_specivo_issue_wins_for_the_whole_session(tmp_path: Path) -> None:
    wires = _wires(build(tmp_path), session_issues={SESSION: "ACME-99"})
    mine = [w for w in wires if w["session_id"] == SESSION]
    assert len(mine) == 4  # the keys stay those of the segments
    assert {(w["attribution"]["issue_ref"], w["attribution"]["issue_ref_source"]) for w in mine} == {("ACME-99", "env")}


def test_a_later_first_mark_moves_earlier_usage_onto_the_legacy_key(tmp_path: Path) -> None:
    folder = projects_dir(tmp_path)
    t = Transcript(SESSION).prompt("2026-10-05T08:00:00.000Z").usage("2026-10-05T08:00:05.000Z", 100)
    t.write(folder / f"{SESSION}.jsonl")
    [before] = _wires(tmp_path)
    assert before["idempotency_key"] == _legacy() and _issue(before) is None
    t.call("2026-10-05T09:00:00.000Z", "mcp__specivo__specivo_timer", {"op": "start", "issue_ref": "ACME-4"})
    t.write(folder / f"{SESSION}.jsonl")
    [after] = _wires(tmp_path)
    assert after["idempotency_key"] == before["idempotency_key"]
    assert _issue(after) == "ACME-4" and after["tokens"]["input"] == 150


def test_a_failed_create_or_a_result_without_a_key_marks_nothing(tmp_path: Path) -> None:
    folder = projects_dir(tmp_path)
    t = Transcript(SESSION).usage("2026-10-05T08:00:00.000Z")
    t.call("2026-10-05T08:01:00.000Z", "mcp__specivo__specivo_create_issue", {"subject": "x"}, call_id="c1")
    t.result("2026-10-05T08:01:01.000Z", "c1", "Created issue ACME-8: x", is_error=True)
    t.call("2026-10-05T08:02:00.000Z", "mcp__specivo__specivo_create_issue", {"subject": "y"}, call_id="c2")
    t.result("2026-10-05T08:02:01.000Z", "c2", "Error: project not found")
    t.write(folder / f"{SESSION}.jsonl")
    [wire] = _wires(tmp_path)
    assert _issue(wire) is None and wire["idempotency_key"] == _legacy()


def test_parallel_subagents_on_different_issues(tmp_path: Path) -> None:
    folder = projects_dir(tmp_path)
    main = Transcript(SESSION).usage("2026-10-05T08:00:00.000Z", 10)
    main.call("2026-10-05T08:01:00.000Z", "mcp__specivo__specivo_update_issue", {"issue_ref": "ACME-1", "status_id": 2})
    main.write(folder / f"{SESSION}.jsonl")
    for name, issue in (("one", "ACME-2"), ("two", "ACME-3")):
        sub = Transcript(SESSION, sidechain=True, name=name).usage("2026-10-05T08:02:00.000Z", 20)
        sub.call("2026-10-05T08:03:00.000Z", "mcp__specivo__specivo_timer", {"op": "start", "issue_ref": issue})
        sub.usage("2026-10-05T08:04:00.000Z", 30)
        sub.write(folder / SESSION / "subagents" / f"agent-{name}.jsonl")
    segments = _by_issue(_wires(tmp_path))
    assert {k: v["tokens"]["input"] for k, v in segments.items()} == {
        "ACME-1": 10 + 50,
        "ACME-2": 20 + 50 + 30,
        "ACME-3": 20 + 50 + 30,
    }


# -- keys that lose their usage --------------------------------------------------------------


def test_a_segment_key_left_without_usage_is_sent_with_zero_tokens(tmp_path: Path, server: Any) -> None:
    home = build(tmp_path)
    sink = SpecivoSink("https://specivo.example.com", "spv_x", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        first = _collect(home, state=state)
        deliver(first.records, sink, state)
        segment_keys = {segment_key(_legacy(), i) for i in ("ACME-13", "ACME-14", "ACME-20")}
        assert segment_keys <= {r.idempotency_key for r in first.records}
        # Switched off, the day is one record again; the segment entries are zeroed, not left counting.
        second = _collect(home, Config(agent_log_issues=False), state=state)
        zeroed = [r.to_wire() for r in second.records if r.idempotency_key in segment_keys]
        assert len(zeroed) == 3
        assert all(w["tokens"] == {} and "attribution" not in w for w in zeroed)
        summary = deliver(second.records, sink, state)
        assert summary.sent == 1 + 3  # the legacy bucket grew, the three segments went to zero
        third = _collect(home, Config(agent_log_issues=False), state=state)
        assert deliver(third.records, sink, state).sent == 0


# -- Codex and OpenCode --------------------------------------------------------------------


def _codex_line(ts: str, kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload}) + "\n"


def _codex_tokens(ts: str, total: int) -> str:
    usage = {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    return _codex_line(ts, "event_msg", {"type": "token_count", "info": {"total_token_usage": usage}})


def test_codex_marks_from_function_calls_and_mcp_results(tmp_path: Path) -> None:
    session = "019a0000-0000-7000-8000-000000000001"
    path = tmp_path / ".codex" / "sessions" / "2026" / "10" / "05" / f"rollout-2026-10-05T08-00-00-{session}.jsonl"
    path.parent.mkdir(parents=True)
    update = {"issue_ref": "acme-30", "status_id": 2, "notes": "CANARY-PROMPT-5c1e"}
    lines = [
        _codex_line("2026-10-05T08:00:00Z", "session_meta", {"id": session, "cwd": "/work/app", "cli_version": "1"}),
        _codex_line("2026-10-05T08:00:00Z", "turn_context", {"model": "gpt-5-codex"}),
        _codex_tokens("2026-10-05T08:00:10Z", 100),
        _codex_line(
            "2026-10-05T09:00:00Z",
            "response_item",
            {
                "type": "function_call",
                "namespace": "mcp__specivo__",
                "name": "specivo_update_issue",
                "arguments": json.dumps(update),
                "call_id": "call_1",
            },
        ),
        _codex_line(
            "2026-10-05T09:00:01Z",
            "event_msg",
            {
                "type": "mcp_tool_call_end",
                "call_id": "call_1",
                "invocation": {"server": "specivo", "tool": "specivo_update_issue", "arguments": update},
                "result": {"Ok": {"content": [{"type": "text", "text": "Updated issue ACME-30"}], "isError": False}},
            },
        ),
        _codex_tokens("2026-10-05T09:00:05Z", 300),
        _codex_line(
            "2026-10-05T11:00:00Z",
            "response_item",
            {"type": "function_call", "name": "specivo_create_issue", "arguments": "{}", "call_id": "call_2"},
        ),
        _codex_line(
            "2026-10-05T11:00:01Z",
            "event_msg",
            {
                "type": "mcp_tool_call_end",
                "call_id": "call_2",
                "invocation": {"server": "specivo", "tool": "specivo_create_issue", "arguments": {}},
                "result": {"Ok": {"content": [{"type": "text", "text": "Created issue ACME-31: x"}], "isError": False}},
            },
        ),
        _codex_tokens("2026-10-05T11:00:05Z", 1000),
    ]
    path.write_text("".join(lines), encoding="utf-8")
    wires = [r.to_wire() for r in _collect(tmp_path, adapter=CodexAdapter()).records]
    segments = {_issue(w): w["tokens"]["input"] for w in wires}
    assert segments == {"ACME-30": 100 + 200, "ACME-31": 700}
    assert "CANARY" not in json.dumps(wires)


_CODEX_SESSION = "019a0000-0000-7000-8000-000000000002"


def _codex_item(
    ts: str,
    call_id: str,
    tool: str,
    arguments: object,
    *,
    started_ms: object = None,
    status: str = "completed",
    result: object = "default",
) -> str:
    """A current-format MCP call: ``event_msg`` / ``item_completed`` with an ``McpToolCall`` item."""
    if result == "default":
        result = {"content": [{"type": "text", "text": "CANARY-TOOL-OUTPUT-9d07"}], "isError": False}
    payload: dict[str, Any] = {
        "type": "item_completed",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item": {
            "type": "McpToolCall",
            "id": call_id,
            "server": "specivo",
            "tool": tool,
            "arguments": arguments,
            "status": status,
            "duration": {"secs": 0, "nanos": 500},
            "result": result,
        },
    }
    if started_ms is not None:
        payload["started_at_ms"] = started_ms
    return _codex_line(ts, "event_msg", payload)


def _codex_rollout(home: Path, calls: list[str]) -> None:
    """A rollout with 100 input tokens at 08:00, the *calls* (all at 09:00), and 200 more tokens at 10:00."""
    path = home / ".codex" / "sessions" / "2026" / "10" / "05" / f"rollout-2026-10-05T08-00-00-{_CODEX_SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        _codex_line("2026-10-05T08:00:00Z", "session_meta", {"id": _CODEX_SESSION, "cli_version": "0.60.0"}),
        _codex_line("2026-10-05T08:00:00Z", "turn_context", {"model": "gpt-5-codex"}),
        _codex_tokens("2026-10-05T08:00:10Z", 100),
        *calls,
        _codex_tokens("2026-10-05T10:00:00Z", 300),
    ]
    path.write_text("".join(lines), encoding="utf-8")


def _codex_marks(home: Path) -> list[tuple[str | None, datetime]]:
    [src] = CodexAdapter().discover(DiscoveryContext(home=home, env={}))
    return [(e.issue_mark, e.timestamp) for e in CodexAdapter().parse(src) if e.issue_mark is not None]


AT_9 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
#: The call started a second before its line was written (epoch milliseconds).
STARTED = datetime(2026, 10, 5, 8, 59, 59, tzinfo=UTC)
STARTED_MS = int(STARTED.timestamp() * 1000)


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("specivo_update_issue", {"issue_ref": "acme-40", "status_id": 2, "notes": "CANARY-PROMPT-5c1e"}, "ACME-40"),
        ("specivo_update_issue", {"issue_ref": "ACME-40", "assigned_to_id": 5}, "ACME-40"),
        ("specivo_update_issue", '{"issue_ref": "ACME-40", "status_id": 2}', "ACME-40"),  # JSON text
        ("specivo_update_issue", {"issue_ref": "ACME-40", "notes": "CANARY-PROMPT-5c1e"}, None),
        ("specivo_timer", {"op": "start", "issue_ref": "ACME-41"}, "ACME-41"),
        ("specivo_timer", {"op": "stop", "issue_ref": "ACME-41"}, None),
        ("specivo_show_issue", {"issue_ref": "ACME-42"}, None),
        ("mcp__work__specivo_update_issue", {"issue_ref": "ACME-43", "status_id": 2}, "ACME-43"),
        ("specivo_update_issue", "not json", None),
    ],
)
def test_codex_mcp_item_marks(tmp_path: Path, tool: str, arguments: object, expected: str | None) -> None:
    _codex_rollout(tmp_path, [_codex_item("2026-10-05T09:00:00Z", "exec-1", tool, arguments, started_ms=STARTED_MS)])
    assert _codex_marks(tmp_path) == ([(expected, STARTED)] if expected else [])


def test_codex_mcp_item_create_marks_the_created_issue(tmp_path: Path) -> None:
    created = {"content": [{"type": "text", "text": "Created issue ACME-44: CANARY-TOOL-OUTPUT-9d07"}]}
    args = {"project_key": "ACME", "subject": "CANARY-PROMPT-5c1e"}
    unparsable = {"content": [{"type": "text", "text": "Something else ACME-45"}], "isError": False}
    _codex_rollout(
        tmp_path,
        [
            _codex_item("2026-10-05T09:00:00Z", "exec-1", "specivo_create_issue", args, result=created),
            _codex_item("2026-10-05T09:00:01Z", "exec-2", "specivo_create_issue", args, result=unparsable),
        ],
    )
    # Without started_at_ms the line's timestamp is the call's time.
    assert _codex_marks(tmp_path) == [("ACME-44", AT_9)]


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("failed", {"content": [{"type": "text", "text": "Created issue ACME-46: x"}], "isError": False}),
        ("completed", {"content": [{"type": "text", "text": "Created issue ACME-46: x"}], "isError": True}),
        ("completed", None),
        ("failed", None),
    ],
)
def test_codex_mcp_item_errors_mark_nothing(tmp_path: Path, status: str, result: object) -> None:
    update = {"issue_ref": "ACME-46", "status_id": 2}
    _codex_rollout(
        tmp_path,
        [
            _codex_item("2026-10-05T09:00:00Z", "exec-1", "specivo_update_issue", update, status=status, result=result),
            _codex_item("2026-10-05T09:00:01Z", "exec-2", "specivo_create_issue", {}, status=status, result=result),
        ],
    )
    assert _codex_marks(tmp_path) == []


def test_codex_one_mark_per_call_across_log_forms(tmp_path: Path) -> None:
    update = {"issue_ref": "ACME-47", "status_id": 2}
    created = {"content": [{"type": "text", "text": "Created issue ACME-48: x"}], "isError": False}
    _codex_rollout(
        tmp_path,
        [
            # An update logged as a function call and again as an item: one mark, at the first.
            _codex_line(
                "2026-10-05T09:00:00Z",
                "response_item",
                {
                    "type": "function_call",
                    "name": "specivo_update_issue",
                    "arguments": json.dumps(update),
                    "call_id": "c1",
                },
            ),
            _codex_item("2026-10-05T09:00:02Z", "c1", "specivo_update_issue", update),
            # A create announced as a function call is completed by its item, at the function call's time.
            _codex_line(
                "2026-10-05T09:30:00Z",
                "response_item",
                {"type": "function_call", "name": "specivo_create_issue", "arguments": "{}", "call_id": "c2"},
            ),
            _codex_item("2026-10-05T09:30:02Z", "c2", "specivo_create_issue", {}, result=created),
            _codex_line(
                "2026-10-05T09:30:03Z",
                "event_msg",
                {
                    "type": "mcp_tool_call_end",
                    "call_id": "c2",
                    "invocation": {"tool": "specivo_create_issue", "arguments": {}},
                    "result": {"Ok": created},
                },
            ),
        ],
    )
    assert _codex_marks(tmp_path) == [
        ("ACME-47", AT_9),
        ("ACME-48", datetime(2026, 10, 5, 9, 30, tzinfo=UTC)),
    ]


def test_codex_mcp_items_split_the_session(tmp_path: Path) -> None:
    update = {"issue_ref": "acme-49", "status_id": 2, "notes": "CANARY-PROMPT-5c1e"}
    _codex_rollout(tmp_path, [_codex_item("2026-10-05T09:00:00Z", "exec-1", "specivo_update_issue", update)])
    wires = [r.to_wire() for r in _collect(tmp_path, adapter=CodexAdapter()).records]
    assert [(w["attribution"]["issue_ref"], w["attribution"]["issue_ref_source"]) for w in wires] == [
        ("ACME-49", "agent_log")
    ]
    assert wires[0]["tokens"]["input"] == 300
    assert "CANARY" not in json.dumps(wires)


def test_opencode_marks_from_tool_parts(tmp_path: Path) -> None:
    data_dir = tmp_path / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True)
    db = sqlite3.connect(data_dir / "opencode.db")
    db.executescript(V2_SCHEMA)
    for sid, parent in (("ses_root", None), ("ses_child", "ses_root")):
        db.execute(
            "INSERT INTO session_v2 (id, project_id, parent_id, slug, directory, title, version, time_created,"
            " time_updated) VALUES (?, 'prj', ?, ?, '/work/app', 't', '2.2.5', 0, 0)",
            (sid, parent, sid),
        )

    def message(mid: str, sid: str, when: str, parts: list[dict[str, Any]]) -> None:
        data = {
            "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
            "tokens": {"input": 10, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "time": {"created": ms(when)},
            "content": parts,
        }
        db.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES (?, ?, 'assistant', 0, ?, ?, ?)",
            (mid, sid, ms(when), ms(when), json.dumps(data)),
        )

    code_call = {
        "type": "tool",
        "name": "execute",
        "state": {
            "status": "completed",
            "input": {"code": "CANARY-PROMPT-5c1e"},
            "metadata": {
                "toolCalls": [
                    {"tool": "specivo.specivo_update_issue", "input": {"issue_ref": "ACME-40", "status_id": 2}}
                ]
            },
        },
    }
    direct = {
        "type": "tool",
        "name": "specivo_specivo_timer",
        "time": {"created": ms("2026-10-05T10:00:00")},
        "state": {"status": "completed", "input": {"op": "start", "issue_ref": "ACME-41"}, "content": []},
    }
    message("m1", "ses_root", "2026-10-05T08:00:00", [])
    message("m2", "ses_root", "2026-10-05T09:00:00", [code_call])
    message("m3", "ses_child", "2026-10-05T09:30:00", [])
    message("m4", "ses_child", "2026-10-05T10:00:00", [direct])
    message("m5", "ses_root", "2026-10-05T11:00:00", [])
    db.commit()
    db.close()
    wires = [r.to_wire() for r in _collect(tmp_path, adapter=OpenCodeAdapter(sleep=lambda s: None)).records]
    # The child session is a thread of the root session; it marked ACME-41, so all its usage counts there.
    assert {_issue(w): w["tokens"]["input"] for w in wires} == {"ACME-40": 10 + 10 + 10, "ACME-41": 10 + 10}


def test_the_config_switch() -> None:
    assert Config().agent_log_issues is True
    assert parse_config({"agent_log_issues": False}).agent_log_issues is False
    with pytest.raises(ValueError, match="agent_log_issues"):
        parse_config({"agent_log_issues": "no"})


def test_ledger_rows_are_sent_status(tmp_path: Path, server: Any) -> None:
    home = build(tmp_path)
    sink = SpecivoSink("https://specivo.example.com", "spv_x", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        records = _collect(home, state=state).records
        deliver(records, sink, state)
        assert all((row := state.bucket(r.idempotency_key)) and row.status == SENT for r in records)
