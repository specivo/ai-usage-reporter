"""The Claude Code route: the hook's record, the settings files and the claude.ai organization."""

from __future__ import annotations

import hashlib
import io
import json
import zlib
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter, _keep_base_url, org_route, settings_host
from ai_usage_reporter.config import Config
from ai_usage_reporter.hooks.runner import run_hook
from ai_usage_reporter.model import UsageEvent
from ai_usage_reporter.pipeline import collect
from ai_usage_reporter.state import STATE_FILE, State

SESSION = "77777777-7777-4777-8777-777777777777"
ORGANIZATION = "CANARY-ORG-0a1b2c3d-0000-4000-8000-000000000000"
ACCOUNT = "CANARY-ACCOUNT-9e8d7c6b"
SECRET = "CANARY-SECRET-5f6e"
GATEWAY_URL = f"https://ci-bot:{SECRET}@llm-gateway.example.com:8443/anthropic?key={SECRET}"
ORG_ROUTE = "claude.ai:org-" + hashlib.sha256(ORGANIZATION.encode()).hexdigest()[:8]
COMMAND = ["/opt/tools/bin/ai-usage"]


def _transcript(home: Path, cwd: Path, *, owner: bool = True, owner_last: bool = False) -> Path:
    path = home / ".claude" / "projects" / "-work-app" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line: dict[str, Any] = {
        "type": "assistant",
        "sessionId": SESSION,
        "cwd": str(cwd),
        "timestamp": "2026-10-05T08:00:00Z",
        "requestId": "req_1",
        "message": {"id": "msg_1", "model": "claude-sonnet-4-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    owner_line = {
        "type": "session-owner",
        "accountUuid": ACCOUNT,
        "organizationUuid": ORGANIZATION,
        "sessionId": SESSION,
    }
    lines = [line]
    if owner:
        lines = [line, owner_line] if owner_last else [owner_line, line]
    path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")
    return path


def _settings(path: Path, base_url: str | None) -> None:
    env: dict[str, str] = {"ANTHROPIC_AUTH_TOKEN": SECRET, "OTHER": SECRET}
    if base_url is not None:
        env["ANTHROPIC_BASE_URL"] = base_url
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"apiKeyHelper": SECRET, "env": env, "model": "opus"}), encoding="utf-8")


def _events(home: Path, state: State | None = None) -> list[UsageEvent]:
    adapter = ClaudeCodeAdapter()
    adapter.bind_run(state, None)
    ctx = DiscoveryContext(home=home, env={})
    return [e for src in adapter.discover(ctx) for e in adapter.parse(src) if not e.is_marker]


def _hook(state_dir: Path, event: str, env: dict[str, str]) -> list[list[str]]:
    calls: list[list[str]] = []
    payload = json.dumps({"session_id": SESSION, "hook_event_name": event, "transcript_path": "/x/y.jsonl"})
    run_hook(
        "claude-code",
        (),
        stdin=io.StringIO(payload),
        stdout=io.StringIO(),
        command=COMMAND,
        state=state_dir,
        spawn=lambda argv, log: calls.append(list(argv)),
        env=env,
    )
    return calls


def _routes(state_dir: Path) -> dict[str, str]:
    with State(state_dir / STATE_FILE) as state:
        return state.session_routes("claude-code")


# -- the hook ----------------------------------------------------------------------------


def test_session_start_records_only_the_host_and_pushes_nothing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    assert _hook(state_dir, "SessionStart", {"ANTHROPIC_BASE_URL": GATEWAY_URL, "ANTHROPIC_AUTH_TOKEN": SECRET}) == []
    assert _routes(state_dir) == {SESSION: "llm-gateway.example.com"}
    assert SECRET.encode() not in (state_dir / STATE_FILE).read_bytes()


def test_a_session_without_a_base_url_is_recorded_as_direct(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _hook(state_dir, "SessionStart", {})
    assert _routes(state_dir) == {SESSION: ""}


def test_later_events_record_only_a_missing_route_and_still_push(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    [push] = _hook(state_dir, "Stop", {"ANTHROPIC_BASE_URL": "https://first.example.com"})
    assert push[1:3] == ["hook-push", "--tool"]
    assert _routes(state_dir) == {SESSION: "first.example.com"}
    _hook(state_dir, "SessionEnd", {"ANTHROPIC_BASE_URL": "https://second.example.com"})
    assert _routes(state_dir) == {SESSION: "first.example.com"}
    # A new start (a resumed session) records again.
    _hook(state_dir, "SessionStart", {"ANTHROPIC_BASE_URL": "https://second.example.com"})
    assert _routes(state_dir) == {SESSION: "second.example.com"}


def test_a_broken_state_database_does_not_fail_the_hook(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    (state_dir / STATE_FILE).mkdir(parents=True)  # a directory where the database should be
    assert _hook(state_dir, "SessionStart", {"ANTHROPIC_BASE_URL": GATEWAY_URL}) == []
    assert len(_hook(state_dir, "Stop", {})) == 1


# -- the adapter ---------------------------------------------------------------------------


def test_the_hook_record_wins(tmp_path: Path) -> None:
    cwd = tmp_path / "work" / "app"
    _transcript(tmp_path, cwd)
    _settings(cwd / ".claude" / "settings.json", "https://settings.example.com")
    with State.in_memory() as state:
        state.db.execute(
            "INSERT INTO session_routes VALUES ('claude-code', ?, 'hook.example.com', '2026-10-05T08:00:00+00:00')",
            (SESSION,),
        )
        [event] = _events(tmp_path, state)
    assert (event.route, event.provider) == ("hook.example.com", "anthropic")


def test_a_direct_session_from_the_hook_skips_the_settings(tmp_path: Path) -> None:
    cwd = tmp_path / "work" / "app"
    _transcript(tmp_path, cwd)
    _settings(cwd / ".claude" / "settings.json", "https://settings.example.com")
    with State.in_memory() as state:
        state.db.execute(
            "INSERT INTO session_routes VALUES ('claude-code', ?, '', '2026-10-05T08:00:00+00:00')", (SESSION,)
        )
        [event] = _events(tmp_path, state)
    assert event.route == ORG_ROUTE


@pytest.mark.parametrize(
    ("local", "project", "user", "expected"),
    [
        ("https://local.example.com", "https://project.example.com", "https://user.example.com", "local.example.com"),
        (None, "https://project.example.com", "https://user.example.com", "project.example.com"),
        (None, None, GATEWAY_URL, "llm-gateway.example.com"),
        (None, None, None, ORG_ROUTE),
    ],
)
def test_settings_files_without_a_hook_record(
    tmp_path: Path, local: str | None, project: str | None, user: str | None, expected: str
) -> None:
    cwd = tmp_path / "work" / "app"
    _transcript(tmp_path, cwd)
    _settings(cwd / ".claude" / "settings.local.json", local)
    _settings(cwd / ".claude" / "settings.json", project)
    _settings(tmp_path / ".claude" / "settings.json", user)
    [event] = _events(tmp_path)
    assert event.route == expected


@pytest.mark.parametrize("owner_last", [False, True])
def test_the_organization_is_sent_hashed_only(tmp_path: Path, owner_last: bool) -> None:
    _transcript(tmp_path, tmp_path / "nowhere", owner_last=owner_last)
    [event] = _events(tmp_path)
    assert event.route == ORG_ROUTE == org_route(ORGANIZATION)
    assert len(ORG_ROUTE) == len("claude.ai:org-") + 8
    cached = json.dumps(event.to_cache())
    assert ORGANIZATION not in cached and ACCOUNT not in cached


def test_no_route_without_any_source(tmp_path: Path) -> None:
    _transcript(tmp_path, tmp_path / "nowhere", owner=False)
    [event] = _events(tmp_path)
    assert event.route is None


def test_settings_keep_nothing_but_the_base_url(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _settings(path, GATEWAY_URL)
    assert settings_host(path) == "llm-gateway.example.com"
    # Only the base URL survives parsing; tokens and every other key are dropped while the file is read.
    parsed = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_keep_base_url)
    assert parsed == {"env": {"ANTHROPIC_BASE_URL": GATEWAY_URL}}
    path.write_text("{not json", encoding="utf-8")
    assert settings_host(path) is None
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": 42}}), encoding="utf-8")
    assert settings_host(path) is None


def test_the_route_reaches_the_record_and_no_secret_leaves(tmp_path: Path) -> None:
    cwd = tmp_path / "work" / "app"
    _transcript(tmp_path, cwd)
    _settings(tmp_path / ".claude" / "settings.json", GATEWAY_URL)
    registry = Registry(adapters={"claude-code": ClaudeCodeAdapter()})
    with State.in_memory() as state:
        collected = collect(
            Config(), registry, state=state, context_for=lambda a, roots: DiscoveryContext(home=tmp_path, env={})
        )
        cached = b"".join(zlib.decompress(row[0]) for row in state.db.execute("SELECT events FROM files"))
    [record] = [r.to_wire() for r in collected.records]
    assert (record["route"], record["provider"]) == ("llm-gateway.example.com", "anthropic")
    text = json.dumps(record)
    for secret in (SECRET, ORGANIZATION, ACCOUNT):
        assert secret not in text
    assert SECRET.encode() not in cached
