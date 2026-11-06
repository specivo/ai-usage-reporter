"""Discovery and parsing details of the built-in adapters."""

from __future__ import annotations

import json
import os
import shutil
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, adapter_hook, load_adapters
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter, session_from_path
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.dedup import deduplicate
from ai_usage_reporter.model import UsageEvent


def _events(adapter: Any, ctx: DiscoveryContext) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    for src in adapter.discover(ctx):
        events.extend(adapter.parse(src))
    return deduplicate(adapter, events)


def test_claude_default_directories(tmp_path: Path, fixture_homes: dict[str, Path]) -> None:
    home = fixture_homes["claude"].parent
    xdg = home / ".config" / "claude"
    shutil.copytree(fixture_homes["claude"], xdg)
    adapter = ClaudeCodeAdapter()
    files = list(adapter.discover(DiscoveryContext(home=home, env={})))
    assert len(files) == 6  # both directories are read ...
    events = _events(adapter, DiscoveryContext(home=home, env={}))
    single = _events(adapter, DiscoveryContext(home=home, env={"CLAUDE_CONFIG_DIR": str(fixture_homes["claude"])}))
    assert len(events) == len(single)  # ... and the copies are counted once


def test_claude_config_dir_may_name_projects(fixture_homes: dict[str, Path], tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter()
    env = {"CLAUDE_CONFIG_DIR": str(fixture_homes["claude"] / "projects")}
    assert len(list(adapter.discover(DiscoveryContext(home=tmp_path, env=env)))) == 3
    missing = {"CLAUDE_CONFIG_DIR": str(tmp_path / "nowhere")}
    assert list(adapter.discover(DiscoveryContext(home=fixture_homes["claude"].parent, env=missing))) == []


def test_claude_session_from_path() -> None:
    base = Path("projects") / "proj"
    assert session_from_path(base / "abc.jsonl") == "abc"
    assert session_from_path(base / "abc" / "subagents" / "agent-1.jsonl") == "abc"
    assert session_from_path(base / "abc" / "subagents" / "nested" / "agent-1.jsonl") == "abc"


def test_claude_counts_malformed_lines(fixture_homes: dict[str, Path]) -> None:
    adapter = ClaudeCodeAdapter()
    sources = list(adapter.discover(DiscoveryContext(home=Path("/"), env={}, roots=(fixture_homes["claude"],))))
    for src in sources:
        list(adapter.parse(src))
    assert sum(s.stats.malformed_lines for s in sources) == 2


def test_claude_hook_metadata() -> None:
    hook = adapter_hook(ClaudeCodeAdapter())
    assert hook is not None and "SessionEnd" in hook.events


def _codex_session(home: Path, folder: str, session: str, model: str, total: int) -> Path:
    path = home / ".codex" / folder / f"rollout-2026-10-05T08-00-00-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"timestamp": "2026-10-05T08:00:00Z", "type": "session_meta", "payload": {"id": session}},
        {"timestamp": "2026-10-05T08:00:01Z", "type": "turn_context", "payload": {"model": model}},
        {
            "timestamp": "2026-10-05T08:00:02Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total}}},
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


SESSION = "44444444-4444-4444-8444-444444444444"


def test_codex_prefers_active_over_archived(tmp_path: Path) -> None:
    _codex_session(tmp_path, "archived_sessions", SESSION, "archived-model", 10)
    _codex_session(tmp_path, "sessions/2026/10/05", SESSION, "active-model", 20)
    events = _events(CodexAdapter(), DiscoveryContext(home=tmp_path, env={}))
    assert [(e.model, e.tokens.input) for e in events] == [("active-model", 20)]


def test_codex_home_and_compressed_rollouts(tmp_path: Path) -> None:
    zstandard = pytest.importorskip("zstandard")
    codex_home = tmp_path / "elsewhere"
    plain = _codex_session(tmp_path, "sessions", SESSION, "gpt-5-codex", 30)
    target = codex_home / "sessions" / (plain.name + ".zst")
    target.parent.mkdir(parents=True)
    target.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    events = _events(CodexAdapter(), DiscoveryContext(home=tmp_path, env={"CODEX_HOME": str(codex_home)}))
    assert [(e.model, e.tokens.input) for e in events] == [("gpt-5-codex", 30)]
    # A plain copy in the same folder is read instead of the compressed one.
    shutil.copy2(plain, codex_home / "sessions" / plain.name)
    sources = list(CodexAdapter().discover(DiscoveryContext(home=tmp_path, env={"CODEX_HOME": str(codex_home)})))
    assert [s.path.suffix for s in sources] == [".jsonl"]


def test_codex_without_model_metadata(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "sessions" / f"rollout-x-{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    line = {
        "timestamp": "2026-10-05T08:00:02Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 1}}},
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    [event] = _events(CodexAdapter(), DiscoveryContext(home=tmp_path, env={}))
    assert (event.model, event.session_id, event.tokens.input, event.tokens.output) == ("unknown", SESSION, 5, 1)


class PluginAdapter:
    name = "example-tool"
    provider = "example"
    parser_version = "1"

    def discover(self, ctx: DiscoveryContext) -> list[Any]:
        return []

    def parse(self, src: Any) -> list[UsageEvent]:
        return []

    def dedup_key(self, event: UsageEvent) -> str | None:
        return None


def test_plugins_load_from_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    import ai_usage_reporter.adapters as adapters

    points = [
        EntryPoint("example", f"{__name__}:PluginAdapter", adapters.ENTRY_POINT_GROUP),
        EntryPoint("broken", "no_such_module:Thing", adapters.ENTRY_POINT_GROUP),
        EntryPoint("clash", "ai_usage_reporter.adapters.codex:CodexAdapter", adapters.ENTRY_POINT_GROUP),
    ]
    monkeypatch.setattr(adapters, "entry_points", lambda group: points if group == adapters.ENTRY_POINT_GROUP else [])
    registry = load_adapters()
    assert set(registry.adapters) == {"claude-code", "codex", "example-tool"}
    assert len(registry.errors) == 2


def test_unreadable_files_do_not_stop_parsing(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "sessions" / f"rollout-x-{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe garbage \x00\n" + os.urandom(64))
    sources = list(CodexAdapter().discover(DiscoveryContext(home=tmp_path, env={})))
    assert list(CodexAdapter().parse(sources[0])) == []
    assert sources[0].stats.malformed_lines >= 1
