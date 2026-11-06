"""Hook installers on temporary config files, and the fail-open hook runner."""

from __future__ import annotations

import io
import json
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_usage_reporter.hooks import BACKUP_INFIX, HOOK_TOOLS, HookContext, HookError, install, uninstall
from ai_usage_reporter.hooks import codex as codex_hook
from ai_usage_reporter.hooks import opencode as opencode_hook
from ai_usage_reporter.hooks.runner import DEBOUNCE_SECONDS, run_hook, run_hook_push, stamp_path

COMMAND = ("/opt/tools/bin/ai-usage",)


def _ctx(home: Path, **env: str) -> HookContext:
    return HookContext(home=home, env=env, command=COMMAND, windows=False)


def _backups(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if BACKUP_INFIX in p.name)


# -- Claude Code and Gemini CLI (settings.json) ------------------------------------------


USER_SETTINGS = {
    "model": "opus",
    "hooks": {
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "notify-send done"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "check.sh"}]}],
    },
}


@pytest.mark.parametrize(
    ("tool", "settings", "events", "timeout"),
    [
        ("claude-code", Path(".claude/settings.json"), ("SessionEnd", "Stop"), 10),
        ("gemini-cli", Path(".gemini/settings.json"), ("SessionEnd", "AfterAgent"), 10_000),
    ],
)
def test_settings_hooks_install_idempotently_and_keep_user_hooks(
    tmp_path: Path, tool: str, settings: Path, events: tuple[str, str], timeout: int
) -> None:
    path = tmp_path / settings
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(USER_SETTINGS, indent=2), encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    ctx = _ctx(tmp_path)

    first = install(tool, ctx)
    assert first.action == "updated" and first.backup is not None
    assert first.backup.read_text(encoding="utf-8") == original
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"] == USER_SETTINGS["hooks"]["PreToolUse"]  # type: ignore[index]
    for event in events:
        ours = [h for g in data["hooks"][event] for h in g["hooks"] if "ai-usage" in h["command"]]
        assert len(ours) == 1
        assert ours[0]["command"] == f"/opt/tools/bin/ai-usage hook {tool}"
        assert ours[0]["timeout"] == timeout
    if "Stop" in events:
        assert {"type": "command", "command": "notify-send done"} in data["hooks"]["Stop"][0]["hooks"]

    again = install(tool, ctx)
    assert again.action == "unchanged" and again.backup is None
    assert len(_backups(tmp_path)) == 1

    removed = uninstall(tool, ctx)
    assert removed.action == "removed"
    assert json.loads(path.read_text(encoding="utf-8")) == USER_SETTINGS
    assert uninstall(tool, ctx).action == "absent"


def test_claude_settings_follow_claude_config_dir(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "custom"))
    change = install("claude-code", ctx)
    assert change.path == tmp_path / "custom" / "settings.json" and change.action == "installed"
    assert not (tmp_path / ".claude").exists()


def test_settings_that_are_not_plain_json_are_left_alone(tmp_path: Path) -> None:
    path = tmp_path / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{\n  // a comment\n  "theme": "dark"\n}\n', encoding="utf-8")
    with pytest.raises(HookError):
        install("gemini-cli", _ctx(tmp_path))
    assert "// a comment" in path.read_text(encoding="utf-8")
    assert _backups(tmp_path) == []


# -- Codex (config.toml notify) ----------------------------------------------------------


CODEX_CONFIG = """\
# My Codex settings
model = "gpt-5-codex"
approval_policy = "on-request"

[tui]
notifications = true

[mcp_servers.docs]
command = "docs-server"
args = ["--port", "1"]
"""


def _toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_codex_notify_is_added_before_the_first_table_and_removed_again(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(CODEX_CONFIG, encoding="utf-8")
    ctx = _ctx(tmp_path)
    change = install("codex", ctx)
    assert change.action == "updated" and change.backup is not None
    data = _toml(path)
    assert data["notify"] == [*COMMAND, "hook", "codex"]
    assert data["tui"] == {"notifications": True} and data["model"] == "gpt-5-codex"
    assert install("codex", ctx).action == "unchanged"
    assert uninstall("codex", ctx).action == "removed"
    assert path.read_text(encoding="utf-8") == CODEX_CONFIG


def test_codex_keeps_and_restores_an_existing_notify_program(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    mine = 'notify = [\n  "python3",\n  "/home/me/notify.py", # ring a bell\n]\n'
    path.write_text(mine + CODEX_CONFIG, encoding="utf-8")
    ctx = _ctx(tmp_path)
    install("codex", ctx)
    notify = _toml(path)["notify"]
    assert isinstance(notify, list)
    assert notify[:3] == [*COMMAND, "hook", "codex"]
    assert codex_hook.chained(notify) == ["python3", "/home/me/notify.py"]
    # Installing again does not chain the reporter to itself.
    assert install("codex", ctx).action == "unchanged"
    uninstall("codex", ctx)
    assert _toml(path)["notify"] == ["python3", "/home/me/notify.py"]
    assert _toml(path)["mcp_servers"] == {"docs": {"command": "docs-server", "args": ["--port", "1"]}}


def test_codex_creates_and_deletes_nothing_it_does_not_own(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, CODEX_HOME=str(tmp_path / "codex-home"))
    assert install("codex", ctx).action == "installed"
    assert _toml(tmp_path / "codex-home" / "config.toml")["notify"] == [*COMMAND, "hook", "codex"]
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('notify = ["mine"]\n', encoding="utf-8")
    assert uninstall("codex", _ctx(tmp_path)).action == "absent"
    assert path.read_text(encoding="utf-8") == 'notify = ["mine"]\n'


def test_codex_refuses_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("model = \n", encoding="utf-8")
    with pytest.raises(HookError):
        install("codex", _ctx(tmp_path))


def test_codex_notify_in_a_quoted_key_and_with_brackets_in_strings(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    text = '"notify" = ["sh", "-c", "echo ] [ done"]\nmodel = "x"\n'
    path.write_text(text, encoding="utf-8")
    install("codex", _ctx(tmp_path))
    data = _toml(path)
    assert data["model"] == "x"
    uninstall("codex", _ctx(tmp_path))
    assert _toml(path)["notify"] == ["sh", "-c", "echo ] [ done"]


# -- OpenCode (plugin file) ----------------------------------------------------------------


def test_opencode_plugin_install_and_uninstall(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    change = install("opencode", ctx)
    plugin = tmp_path / "cfg" / "opencode" / "plugins" / opencode_hook.FILE_NAME
    assert change.action == "installed" and change.path == plugin
    text = plugin.read_text(encoding="utf-8")
    assert json.dumps(list(COMMAND)) in text
    assert '"push", "--source", "opencode", "--session"' in text
    assert "session.idle" in text and "export default" in text and 'id: "specivo-ai-usage-reporter"' in text
    assert install("opencode", ctx).action == "unchanged"
    # A new reporter location rewrites the plugin, with a backup outside plugins/.
    moved = HookContext(home=tmp_path, env=ctx.env, command=("/elsewhere/ai-usage",), windows=False)
    updated = install("opencode", moved)
    assert updated.action == "updated" and updated.backup is not None
    assert updated.backup.parent.name == "ai-usage-backups"
    assert [p.name for p in plugin.parent.iterdir()] == [opencode_hook.FILE_NAME]
    assert uninstall("opencode", moved).action == "removed" and not plugin.exists()


def test_opencode_never_overwrites_someone_elses_plugin(tmp_path: Path) -> None:
    plugin = tmp_path / ".config" / "opencode" / "plugins" / opencode_hook.FILE_NAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export default {}\n", encoding="utf-8")
    with pytest.raises(HookError):
        install("opencode", _ctx(tmp_path))
    with pytest.raises(HookError):
        uninstall("opencode", _ctx(tmp_path))
    assert plugin.read_text(encoding="utf-8") == "export default {}\n"


def test_every_tool_has_an_installer(tmp_path: Path) -> None:
    for tool in HOOK_TOOLS:
        assert install(tool, _ctx(tmp_path)).action == "installed"
        assert uninstall(tool, _ctx(tmp_path)).action == "removed"
    with pytest.raises(HookError):
        install("nope", _ctx(tmp_path))


# -- the runner ----------------------------------------------------------------------------


class Spawned:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, argv: Sequence[str], log: Path) -> None:
        if self.fail:
            raise OSError("no such program")
        self.calls.append(list(argv))


def _run(tool: str, tmp_path: Path, *, stdin: str = "", args: Sequence[str] = (), spawn: Spawned) -> tuple[int, str]:
    out = io.StringIO()
    code = run_hook(
        tool, args, stdin=io.StringIO(stdin), stdout=out, command=COMMAND, state=tmp_path / "state", spawn=spawn
    )
    return code, out.getvalue()


def test_claude_stop_is_debounced_and_session_end_is_not(tmp_path: Path) -> None:
    spawn = Spawned()
    payload = {"session_id": "abc-123", "transcript_path": "/x/CANARY.jsonl", "hook_event_name": "Stop"}
    code, out = _run("claude-code", tmp_path, stdin=json.dumps(payload), spawn=spawn)
    assert (code, out) == (0, "")
    [argv] = spawn.calls
    assert argv[:6] == [*COMMAND, "hook-push", "--tool", "claude-code", "--session", "abc-123"]
    assert argv[-2:] == ["--delay", str(DEBOUNCE_SECONDS)]
    _run("claude-code", tmp_path, stdin=json.dumps({**payload, "hook_event_name": "SessionEnd"}), spawn=spawn)
    assert spawn.calls[1][-2:] == ["--delay", "0"]


def test_gemini_answers_with_an_empty_json_object(tmp_path: Path) -> None:
    spawn = Spawned()
    payload = {"session_id": "g-1", "hook_event_name": "AfterAgent", "prompt": "CANARY", "prompt_response": "x"}
    code, out = _run("gemini-cli", tmp_path, stdin=json.dumps(payload), spawn=spawn)
    assert (code, out.strip()) == (0, "{}")
    assert spawn.calls[0][5] == "g-1"
    assert not any("CANARY" in part for part in spawn.calls[0])


def test_codex_payload_and_chained_program(tmp_path: Path) -> None:
    spawn = Spawned()
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "0199ABCD-0000-7000-8000-000000000001"})
    _run("codex", tmp_path, args=["--chain", json.dumps(["notify.sh", "-q"]), payload], spawn=spawn)
    assert spawn.calls[0] == ["notify.sh", "-q", payload]
    assert spawn.calls[1][5] == "0199abcd-0000-7000-8000-000000000001"
    spawn.calls.clear()
    _run("codex", tmp_path, args=[json.dumps({"type": "something-else", "thread-id": "x"})], spawn=spawn)
    assert spawn.calls == []


@pytest.mark.parametrize(
    ("tool", "stdin", "args"),
    [
        ("claude-code", "not json", ()),
        ("claude-code", json.dumps({"session_id": "../../etc"}), ()),
        ("claude-code", json.dumps(["a"]), ()),
        ("codex", "", ("{broken",)),
        ("codex", "", ()),
        ("unknown-tool", "", ()),
    ],
)
def test_the_runner_always_exits_zero(tmp_path: Path, tool: str, stdin: str, args: tuple[str, ...]) -> None:
    spawn = Spawned()
    assert _run(tool, tmp_path, stdin=stdin, args=args, spawn=spawn)[0] == 0
    assert spawn.calls == []


def test_the_runner_survives_a_failing_spawn(tmp_path: Path) -> None:
    payload = json.dumps({"session_id": "abc", "hook_event_name": "Stop"})
    assert _run("claude-code", tmp_path, stdin=payload, spawn=Spawned(fail=True))[0] == 0


def test_hook_push_waits_and_only_the_latest_call_pushes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    pushes: list[list[str]] = []
    sleeps: list[float] = []
    stamp = stamp_path(state, "claude-code", "abc")
    stamp.parent.mkdir(parents=True)
    stamp.write_text("newer", encoding="ascii")

    def push(argv: list[str]) -> int:
        pushes.append(argv)
        return 0

    run_hook_push("claude-code", "abc", "older", 30, state=state, push=push, sleep=sleeps.append)
    assert pushes == [] and sleeps == [30]
    run_hook_push("claude-code", "abc", "newer", 30, state=state, push=push, sleep=sleeps.append)
    assert pushes == [["push", "--source", "claude-code", "--session", "abc"]]
    run_hook_push("claude-code", "abc", "any", 0, state=state, push=push, sleep=sleeps.append)
    assert len(pushes) == 2 and sleeps == [30, 30]
