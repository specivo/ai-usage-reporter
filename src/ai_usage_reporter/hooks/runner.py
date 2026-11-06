"""``ai-usage hook <tool>``: what the AI tools run, and ``ai-usage hook-push``, what it starts.

``hook`` reads the session id from what the tool passes (standard input for
Claude Code and Gemini CLI, the last argument for Codex), starts
``ai-usage hook-push`` detached in the background and returns at once.
``hook-push`` waits out the debounce delay and then runs
``push --source <tool> --session <id>``.

**Debounce.** Turn events (Claude Code ``Stop``, Gemini CLI ``AfterAgent``,
Codex ``agent-turn-complete``) come often. Each one writes a fresh token to a
small stamp file for the session; its ``hook-push`` waits
:data:`DEBOUNCE_SECONDS` and pushes only if its token is still the latest,
so a burst of turns makes one push, shortly after the last one. A session
end pushes at once.

**Fail open.** ``hook`` never raises, never prints to standard error, and
always exits 0: a broken reporter must not block, slow down or confuse the
tool (Claude Code and Gemini CLI treat exit code 2 as "block"). The
background push writes its own short summary to ``hooks.log`` in the state
directory; it holds counts and statuses, never content.

Nothing the tool passes is kept or logged except the session id: hook
payloads also carry prompts and responses, which are read past and dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

#: How long a turn event waits for more turns of the same session.
DEBOUNCE_SECONDS = 30
#: The most of a hook payload that is read.
MAX_PAYLOAD_BYTES = 1024 * 1024
#: hooks.log is started again when it grows past this.
MAX_LOG_BYTES = 512 * 1024
#: Events that end a session: push without waiting.
FINAL_EVENTS = frozenset({"SessionEnd"})

SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
STDIN_TOOLS = ("claude-code", "gemini-cli")

Spawn = Callable[[Sequence[str], Path], None]


def spawn_detached(argv: Sequence[str], log: Path) -> None:
    """Start *argv* in the background, detached from the tool, with output going to *log*."""
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size > MAX_LOG_BYTES:
            log.replace(log.with_name(log.name + ".1"))
    except OSError:
        pass
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if sys.platform.startswith("win"):
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        options["creationflags"] = flags
    else:
        options["start_new_session"] = True
    with log.open("ab") as out:
        subprocess.Popen(list(argv), stdout=out, stderr=out, **options)


def stamp_path(state: Path, tool: str, session: str) -> Path:
    digest = hashlib.sha256(f"{tool}|{session}".encode()).hexdigest()[:24]
    return state / "hooks" / f"{tool}-{digest}.stamp"


def _session_from_stdin(stdin: TextIO | None) -> tuple[str | None, str]:
    if stdin is None or stdin.isatty():
        return None, ""
    payload = json.loads(stdin.read(MAX_PAYLOAD_BYTES))
    if not isinstance(payload, dict):
        return None, ""
    session = payload.get("session_id")
    event = payload.get("hook_event_name")
    return (session if isinstance(session, str) else None), (event if isinstance(event, str) else "")


def _codex(args: Sequence[str]) -> tuple[str | None, list[str] | None, str | None]:
    """The session, the chained notify program and the raw payload from Codex's arguments."""
    rest = list(args)
    chain: list[str] | None = None
    if len(rest) >= 2 and rest[0] == "--chain":
        loaded = json.loads(rest[1])
        if isinstance(loaded, list) and loaded and all(isinstance(v, str) for v in loaded):
            chain = loaded
        rest = rest[2:]
    if not rest:
        return None, chain, None
    raw = rest[-1]
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
        return None, chain, raw
    session = payload.get("thread-id")
    return (session.lower() if isinstance(session, str) else None), chain, raw


def run_hook(
    tool: str,
    args: Sequence[str],
    *,
    stdin: TextIO | None,
    stdout: TextIO,
    command: Sequence[str],
    state: Path,
    spawn: Spawn = spawn_detached,
) -> int:
    """Handle one hook call. Always returns 0."""
    log = state / "hooks.log"
    try:
        if tool == "gemini-cli":
            # Gemini CLI reads standard output as JSON on exit 0: answer "no decision".
            stdout.write("{}\n")
            stdout.flush()
        chain: list[str] | None = None
        raw: str | None = None
        if tool in STDIN_TOOLS:
            session, event = _session_from_stdin(stdin)
        elif tool == "codex":
            session, chain, raw = _codex(args)
            event = "agent-turn-complete"
        else:
            return 0
        if chain and raw is not None:
            # The user's own notify program comes first; the reporter must not hold it up.
            try:
                spawn([*chain, raw], log)
            except Exception:
                pass
        if not session or not SESSION_ID.match(session):
            return 0
        delay = 0 if event in FINAL_EVENTS else DEBOUNCE_SECONDS
        nonce = secrets.token_hex(8)
        stamp = stamp_path(state, tool, session)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(nonce, encoding="ascii")
        argv = [*command, "hook-push", "--tool", tool, "--session", session, "--nonce", nonce, "--delay", str(delay)]
        spawn(argv, log)
    except Exception:
        pass
    return 0


def run_hook_push(
    tool: str,
    session: str,
    nonce: str,
    delay: float,
    *,
    state: Path,
    push: Callable[[list[str]], int],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait out the debounce, then push the session unless a newer hook call took over."""
    if delay > 0:
        sleep(delay)
        try:
            latest = stamp_path(state, tool, session).read_text(encoding="ascii").strip()
        except OSError:
            latest = nonce
        if latest != nonce:
            return 0
    return push(["push", "--source", tool, "--session", session])
