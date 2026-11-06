"""The Gemini CLI adapter beyond the conformance kit."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext
from ai_usage_reporter.adapters.gemini_cli import GeminiCliAdapter
from ai_usage_reporter.model import UsageEvent

FIXTURES = Path(__file__).parent / "fixtures" / "gemini_cli" / "home"
MAIN = "44444444-4444-4444-8444-444444444444"
SUB = "55555555-5555-4555-8555-555555555555"


def _events(home: Path, roots: tuple[Path, ...] = ()) -> list[UsageEvent]:
    adapter = GeminiCliAdapter()
    ctx = DiscoveryContext(home=home, env={}, roots=roots)
    return [e for src in adapter.discover(ctx) for e in adapter.parse(src)]


def test_token_semantics(tmp_path: Path) -> None:
    chats = tmp_path / ".gemini" / "tmp" / "p" / "chats"
    chats.mkdir(parents=True)
    message = {
        "id": "m",
        "type": "gemini",
        "timestamp": "2026-10-05T01:00:00Z",
        "model": "gemini-2.5-pro",
        "tokens": {"input": 1000, "output": 50, "cached": 600, "thoughts": 30, "tool": 7, "total": 1087},
    }
    (chats / "session-a.json").write_text(json.dumps({"sessionId": "s", "messages": [message]}), encoding="utf-8")
    [event] = _events(tmp_path)
    assert event.tokens.as_dict() == {
        "input": 407,  # 1000 prompt - 600 cached + 7 tool-use prompt
        "output": 50,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 600,
        "reasoning": 30,
    }


def test_json_and_jsonl_copies_and_subagents(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES, tmp_path / "home")
    events = _events(tmp_path / "home")
    main = [e for e in events if e.session_id == MAIN]
    # g-1 and g-2 appear in both copies, g-3 only in the JSONL; s-1 is the subagent.
    assert sorted(e.message_id or "" for e in main) == ["g-1", "g-1", "g-2", "g-2", "g-3", "s-1"]
    sub = next(e for e in main if e.message_id == "s-1")
    assert sub.sub_session_id == SUB
    last_copy = [e for e in main if e.message_id == "g-1" and e.source_path and e.source_path.endswith(".jsonl")]
    assert last_copy[0].tokens.output == 80  # the full copy written after the partial one


def test_temporary_files_are_ignored_and_bad_lines_counted(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES, tmp_path / "home")
    adapter = GeminiCliAdapter()
    sources = list(adapter.discover(DiscoveryContext(home=tmp_path / "home", env={})))
    assert not any(".tmp-" in s.path.name for s in sources)
    for src in sources:
        list(adapter.parse(src))
    assert sum(s.stats.malformed_lines for s in sources) == 1
    assert sum(s.stats.skipped_events for s in sources) == 1


def test_sandbox_directory_and_roots(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / ".gemini", tmp_path / ".cache" / ".gemini")
    assert _events(tmp_path)
    assert _events(tmp_path / "nowhere", roots=(tmp_path / ".cache" / ".gemini",))
