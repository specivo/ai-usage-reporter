"""OpenCode: a plugin file in the ``plugins`` directory of OpenCode's global config.

The directory is ``$XDG_CONFIG_HOME/opencode/plugins`` (default
``~/.config/opencode/plugins``); OpenCode loads every plugin file in it. An
OpenCode 2 plugin module exports an object with an ``id`` and a
``setup(ctx)`` function; ``ctx.event.subscribe()`` yields the server's events.
When a session becomes idle (``session.idle``, or ``session.status`` with an
idle status) the plugin waits a little, so a burst of turns makes one push,
and then starts ``ai-usage push --source opencode --session <id>`` detached,
without waiting for it. Every step is wrapped so that a failure never
reaches OpenCode, and at most one push per session runs at a time.

``SPECIVO_ISSUE`` set in the environment OpenCode runs in reaches the push.
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_usage_reporter.hooks import Change, HookContext, HookError, read_text, replace_file

TOOL = "opencode"
FILE_NAME = "specivo-ai-usage-reporter.js"
MARKER = "// specivo-ai-usage-reporter plugin"
DEBOUNCE_MS = 20_000

PLUGIN_TEMPLATE = """\
{marker}: written by "ai-usage hooks install --tool opencode".
// It reports each session's token usage to Specivo when the session becomes
// idle. "ai-usage hooks uninstall --tool opencode" removes it. Edits are lost
// on the next install.
import {{ spawn }} from "node:child_process";

const COMMAND = {command};
const DEBOUNCE_MS = {debounce};
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$/;
const timers = new Map();
const running = new Set();

const push = (sessionID) => {{
  if (running.has(sessionID)) return;
  try {{
    running.add(sessionID);
    const child = spawn(
      COMMAND[0],
      [...COMMAND.slice(1), "push", "--source", "opencode", "--session", sessionID],
      {{ detached: true, stdio: "ignore", windowsHide: true }},
    );
    const done = () => running.delete(sessionID);
    child.on("error", done);
    child.on("exit", done);
    child.unref();
  }} catch {{
    running.delete(sessionID);
  }}
}};

const schedule = (sessionID) => {{
  if (typeof sessionID !== "string" || !SESSION_ID.test(sessionID)) return;
  clearTimeout(timers.get(sessionID));
  const timer = setTimeout(() => {{
    timers.delete(sessionID);
    push(sessionID);
  }}, DEBOUNCE_MS);
  timer.unref?.();
  timers.set(sessionID, timer);
}};

const idleSession = (event) => {{
  const data = event?.data ?? event?.properties ?? {{}};
  if (event?.type === "session.idle") return data.sessionID;
  if (event?.type === "session.status" && data.status?.type === "idle") return data.sessionID;
  return undefined;
}};

const watch = async (events) => {{
  try {{
    for await (const event of events.subscribe()) {{
      try {{
        const sessionID = idleSession(event);
        if (sessionID) schedule(sessionID);
      }} catch {{}}
    }}
  }} catch {{}}
}};

export default {{
  id: "specivo-ai-usage-reporter",
  async setup(ctx) {{
    try {{
      if (ctx?.event?.subscribe) watch(ctx.event);
    }} catch {{}}
    return () => {{
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    }};
  }},
}};
"""


def plugin_path(ctx: HookContext) -> Path:
    xdg = ctx.env.get("XDG_CONFIG_HOME", "").strip()
    base = (ctx.expand(xdg) if xdg else ctx.home / ".config") / "opencode"
    return base / "plugins" / FILE_NAME


def backup_dir(ctx: HookContext) -> Path:
    """Backups go beside ``plugins/``, never into it, where OpenCode would load them."""
    return plugin_path(ctx).parent.parent / "ai-usage-backups"


def render(ctx: HookContext) -> str:
    return PLUGIN_TEMPLATE.format(marker=MARKER, command=json.dumps(list(ctx.command)), debounce=DEBOUNCE_MS)


def install(ctx: HookContext) -> Change:
    path = plugin_path(ctx)
    old = read_text(path)
    if old is not None and MARKER not in old:
        raise HookError(f"{path.name} exists and was not written by this reporter; left unchanged")
    return replace_file(TOOL, path, old, render(ctx), installing=True, backup_dir=backup_dir(ctx))


def uninstall(ctx: HookContext) -> Change:
    path = plugin_path(ctx)
    old = read_text(path)
    if old is None:
        return Change(TOOL, path, "absent")
    if MARKER not in old:
        raise HookError(f"{path.name} was not written by this reporter; left unchanged")
    return replace_file(TOOL, path, old, None, installing=False, backup_dir=backup_dir(ctx))
