"""Active time from OpenCode 2 databases (tests/fixtures/opencode_time_home.py; seconds worked out by hand there)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import DiscoveryContext, Registry, parse_all
from ai_usage_reporter.adapters.opencode import OpenCodeAdapter
from ai_usage_reporter.config import Config
from ai_usage_reporter.model import PROMPT, TOOL_END, TOOL_START, TURN_END, ThreadEvent, UsageEvent
from ai_usage_reporter.pipeline import Collected, collect
from ai_usage_reporter.testing import validate_records
from tests.fixtures import opencode_home, opencode_time_home
from tests.fixtures.opencode_time_home import CHILD, ROOT


def _home(tmp_path: Path) -> Path:
    opencode_time_home.build(tmp_path)
    return tmp_path


def _collect(home: Path) -> Collected:
    adapter = OpenCodeAdapter(sleep=lambda s: None)
    return collect(
        Config(),
        Registry(adapters={adapter.name: adapter}),
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
    )


def _events(home: Path) -> list[UsageEvent | ThreadEvent]:
    adapter = OpenCodeAdapter(sleep=lambda s: None)
    return [e for src in adapter.discover(DiscoveryContext(home=home, env={})) for e in parse_all(adapter, src)]


def _wires(home: Path) -> list[dict[str, Any]]:
    return [r.to_wire() for r in _collect(home).records]


def test_a_session_with_a_subagent_is_measured_as_worked_out(tmp_path: Path) -> None:
    (record,) = _wires(_home(tmp_path))
    validate_records([record])
    # Main thread 766 + child 35.
    assert record["active_seconds"] == 801
    assert record["active_spans"] == [
        ["2026-11-05T09:00:00Z", "2026-11-05T09:12:12Z"],
        ["2026-11-05T10:12:12Z", "2026-11-05T10:12:36Z"],
        ["2026-11-05T10:12:37Z", "2026-11-05T10:13:00Z"],
        ["2026-11-05T10:25:00Z", "2026-11-05T10:25:11Z"],
        ["2026-11-05T10:32:36Z", "2026-11-05T10:32:41Z"],
        ["2026-11-05T10:40:00Z", "2026-11-05T10:40:01Z"],
        ["2026-11-05T11:00:00Z", "2026-11-05T11:00:05Z"],
    ]


def test_the_events_and_their_flags(tmp_path: Path) -> None:
    events = [e for e in _events(_home(tmp_path)) if isinstance(e, ThreadEvent)]
    by_kind: dict[str, list[ThreadEvent]] = {}
    for event in events:
        by_kind.setdefault(event.kind, []).append(event)
    assert len(by_kind[PROMPT]) == 3 and len(by_kind[TURN_END]) == 3
    assert {e.thread_id for e in events} == {None, CHILD} and {e.session_id for e in events} == {ROOT}
    starts = {e.call_id: e for e in by_kind[TOOL_START]}
    ends = {e.call_id: e for e in by_kind[TOOL_END]}
    assert starts["prt_ask"].human_wait and starts["prt_sub"].subagent_wait and starts["prt_denied"].user_decided
    # The run starts when the tool began to execute, not when its part was created.
    assert starts["prt_shell"].timestamp.isoformat() == "2026-11-05T09:00:06+00:00"
    assert "prt_hung" in starts and "prt_hung" not in ends
    assert len({e.key for e in events}) == len(events)


def test_parse_still_yields_usage_only(tmp_path: Path) -> None:
    home = _home(tmp_path)
    adapter = OpenCodeAdapter(sleep=lambda s: None)
    parsed = [e for src in adapter.discover(DiscoveryContext(home=home, env={})) for e in adapter.parse(src)]
    assert parsed and all(isinstance(e, UsageEvent) for e in parsed)
    with_events = [e for e in _events(home) if isinstance(e, UsageEvent)]
    assert [(e.message_id, e.timestamp) for e in with_events] == [(e.message_id, e.timestamp) for e in parsed]


def test_opencode_1_and_the_json_layout_are_not_measured(tmp_path: Path) -> None:
    data_dir = opencode_home.build(tmp_path)
    db = sqlite3.connect(data_dir / "opencode.db")
    db.executescript(
        "DROP TABLE session_message; DROP TABLE session_v2;"
        " CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer, data text);"
    )
    db.execute(
        "INSERT INTO message VALUES ('msg_v1', 'ses_v1', 1, ?)",
        ('{"role": "assistant", "modelID": "m", "tokens": {"input": 5, "output": 1},'
         ' "time": {"created": 1762333200000, "completed": 1762333201000}}',),
    )  # fmt: skip
    db.commit()
    db.close()
    assert not [e for e in _events(tmp_path) if isinstance(e, ThreadEvent)]
    assert all("active_seconds" not in w for w in _wires(tmp_path))


def test_no_work_activity_is_built_from_these_events(tmp_path: Path) -> None:
    collected = _collect(_home(tmp_path))
    assert collected.records and collected.activity == []
