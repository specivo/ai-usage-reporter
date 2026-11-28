"""Coarse active time from Gemini CLI recordings (tests/fixtures/gemini_time_home.py; seconds worked out there)."""

from __future__ import annotations

import json
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext, Registry, parse_all
from ai_usage_reporter.adapters.gemini_cli import GeminiCliAdapter
from ai_usage_reporter.config import Config
from ai_usage_reporter.model import MODEL, PROMPT, ThreadEvent, UsageEvent
from ai_usage_reporter.pipeline import Collected, collect
from ai_usage_reporter.testing import validate_records
from tests.fixtures import gemini_time_home
from tests.fixtures.gemini_time_home import SESSION, SUBAGENT


def _collect(home: Path) -> Collected:
    adapter = GeminiCliAdapter()
    return collect(
        Config(),
        Registry(adapters={adapter.name: adapter}),
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
    )


def _thread_events(home: Path) -> list[ThreadEvent]:
    adapter = GeminiCliAdapter()
    return [
        e
        for src in adapter.discover(DiscoveryContext(home=home, env={}))
        for e in parse_all(adapter, src)
        if isinstance(e, ThreadEvent)
    ]


def test_a_session_with_a_subagent_is_measured_as_worked_out(tmp_path: Path) -> None:
    collected = _collect(gemini_time_home.build(tmp_path))
    (record,) = [r.to_wire() for r in collected.records]
    validate_records([record])
    # Main thread 485 + subagent 60; the subagent's time lies inside the main thread's first span.
    assert record["active_seconds"] == 545
    assert record["active_spans"] == [
        ["2026-11-05T09:00:00Z", "2026-11-05T09:08:00Z"],
        ["2026-11-05T09:21:00Z", "2026-11-05T09:21:05Z"],
    ]
    assert collected.activity == []


def test_only_prompts_and_model_events(tmp_path: Path) -> None:
    events = _thread_events(gemini_time_home.build(tmp_path))
    assert {e.kind for e in events} == {PROMPT, MODEL}
    main = [e for e in events if e.thread_id is None]
    # Two prompts (the function response is none); four answers and two thoughts.
    assert sum(e.kind == PROMPT for e in main) == 2 and sum(e.kind == MODEL for e in main) == 6
    assert {e.thread_id for e in events} == {None, SUBAGENT} and {e.session_id for e in events} == {SESSION}


def test_the_json_and_jsonl_copies_count_once(tmp_path: Path) -> None:
    home = gemini_time_home.build(tmp_path)
    folder = gemini_time_home.chats(home)
    source = folder / "session-2026-11-05T09-00-7e7e7e7e.jsonl"
    lines = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    document = {**lines[0], "messages": lines[1:]}
    source.with_suffix(".json").write_text(json.dumps(document), encoding="utf-8")
    (record,) = [r.to_wire() for r in _collect(home).records]
    assert record["active_seconds"] == 545


def test_parse_still_yields_usage_only(tmp_path: Path) -> None:
    home = gemini_time_home.build(tmp_path)
    adapter = GeminiCliAdapter()
    parsed = [e for src in adapter.discover(DiscoveryContext(home=home, env={})) for e in adapter.parse(src)]
    assert len(parsed) == 6 and all(isinstance(e, UsageEvent) for e in parsed)
