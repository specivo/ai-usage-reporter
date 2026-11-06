"""Claude Code: a command hook on ``SessionEnd`` and ``Stop`` in ``settings.json``.

The file is ``$CLAUDE_CONFIG_DIR/settings.json``, by default
``~/.claude/settings.json``. Claude Code passes the hook a JSON object on
standard input with ``session_id``, ``transcript_path``, ``cwd`` and
``hook_event_name``; the hook reads only the session id and the event. The
timeout is in seconds. ``SPECIVO_ISSUE`` set in the environment Claude Code
runs in reaches the push.
"""

from __future__ import annotations

from pathlib import Path

from ai_usage_reporter.hooks import Change, HookContext, json_hook_edit

TOOL = "claude-code"
EVENTS = ("SessionEnd", "Stop")
TIMEOUT_SECONDS = 10


def settings_path(ctx: HookContext) -> Path:
    override = ctx.env.get("CLAUDE_CONFIG_DIR", "").strip()
    base = ctx.expand(override) if override else ctx.home / ".claude"
    return base / "settings.json"


def install(ctx: HookContext) -> Change:
    entry: dict[str, object] = {"type": "command", "command": ctx.shell("hook", TOOL), "timeout": TIMEOUT_SECONDS}
    return json_hook_edit(tool=TOOL, path=settings_path(ctx), events=EVENTS, entry=entry)


def uninstall(ctx: HookContext) -> Change:
    return json_hook_edit(tool=TOOL, path=settings_path(ctx), events=EVENTS, entry=None)
