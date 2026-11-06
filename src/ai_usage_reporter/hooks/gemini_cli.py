"""Gemini CLI: a command hook on ``SessionEnd`` and ``AfterAgent`` in ``~/.gemini/settings.json``.

Gemini CLI passes the hook a JSON object on standard input with
``session_id``, ``transcript_path``, ``cwd``, ``hook_event_name`` and
``timestamp`` (``AfterAgent`` adds the prompt and the response, which the
hook never reads). The timeout is in milliseconds. On exit 0 Gemini CLI reads
standard output as JSON, so the hook prints ``{}``. Hooks run with a reduced
environment; ``SPECIVO_ISSUE`` may not reach them (use
``SPECIVO_ISSUE=... ai-usage push --session <id>`` by hand then). ``/hooks``
inside Gemini CLI lists, enables and disables hooks.
"""

from __future__ import annotations

from pathlib import Path

from ai_usage_reporter.hooks import Change, HookContext, json_hook_edit

TOOL = "gemini-cli"
EVENTS = ("SessionEnd", "AfterAgent")
TIMEOUT_MS = 10_000
HOOK_NAME = "specivo-ai-usage"


def settings_path(ctx: HookContext) -> Path:
    return ctx.home / ".gemini" / "settings.json"


def install(ctx: HookContext) -> Change:
    entry: dict[str, object] = {
        "name": HOOK_NAME,
        "type": "command",
        "command": ctx.shell("hook", TOOL),
        "timeout": TIMEOUT_MS,
        "description": "Report this session's token usage to Specivo",
    }
    return json_hook_edit(tool=TOOL, path=settings_path(ctx), events=EVENTS, entry=entry)


def uninstall(ctx: HookContext) -> Change:
    return json_hook_edit(tool=TOOL, path=settings_path(ctx), events=EVENTS, entry=None)
