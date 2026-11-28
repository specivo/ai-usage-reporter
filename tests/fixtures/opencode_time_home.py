"""Builds a synthetic OpenCode 2 database for measuring active time.

Everything is invented: ids, paths, commands, prompts and numbers. The CANARY-*
strings are planted in prompts, tool input and output, titles and paths; none of
them may leave the reporter. Times are epoch milliseconds, as OpenCode writes
them: a ``user`` row starts a turn, an ``idle`` row ends it, an assistant
message has ``time.created``/``streamed``/``completed`` and each ``tool`` part
``time.created``/``ran``/``completed``.

:func:`build` writes the conformance home. The expected seconds, worked out by
hand, are in :func:`build`'s docstring and in ``tests/test_opencode_active_time.py``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from tests.fixtures.opencode_home import CANARY_OUTPUT, CANARY_PATH, CANARY_PROMPT, V2_SCHEMA, ms

ROOT = "ses_0101rootaaaaaaaaaaaaaaaa"
CHILD = "ses_0102childbbbbbbbbbbbbbbb"
CWD = f"/work/{CANARY_PATH}/svc"
MODEL = "claude-sonnet-4-5"
DAY = "2026-11-05"


def t(clock: str) -> int:
    """``HH:MM:SS`` on :data:`DAY` in epoch milliseconds."""
    return ms(f"{DAY}T{clock}")


def tool(part: str, name: str, created: str, ran: str | None, completed: str | None, **state: Any) -> dict[str, Any]:
    times: dict[str, int] = {"created": t(created)}
    if ran is not None:
        times["ran"] = t(ran)
    if completed is not None:
        times["completed"] = t(completed)
    return {
        "type": "tool",
        "id": part,
        "name": name,
        "executed": True,
        "time": times,
        "state": {
            "status": state.pop("status", "completed"),
            "input": {"command": f"run {CANARY_PROMPT}", "path": CWD},
            "content": [{"type": "text", "text": CANARY_OUTPUT}],
            **state,
        },
    }


def assistant(created: str, completed: str, *parts: dict[str, Any], streamed: str | None = None) -> dict[str, Any]:
    times = {"created": t(created), "completed": t(completed)}
    if streamed is not None:
        times["streamed"] = t(streamed)
    return {
        "agent": "build",
        "content": [{"type": "text", "text": f"Done {CANARY_OUTPUT}"}, *parts],
        "finish": "tool-calls" if parts else "stop",
        "model": {"id": MODEL, "providerID": "anthropic"},
        "time": times,
        "tokens": {"input": 100, "output": 20, "reasoning": 0, "cache": {"read": 1000, "write": 0}},
        "cost": 0.001,
    }


def user(when: str) -> dict[str, Any]:
    return {"text": f"Please {CANARY_PROMPT}", "time": {"created": t(when)}}


def idle(when: str) -> dict[str, Any]:
    return {"outcome": "done", "time": {"created": t(when)}}


def rows() -> list[tuple[str, str, str, dict[str, Any]]]:
    """The rows of :func:`build`: (session, message id, type, data)."""
    return [
        # Turn 1 of the main thread.
        (ROOT, "msg_1001", "user", user("09:00:00")),
        (ROOT, "msg_1002", "assistant",
         assistant("09:00:02", "09:12:10", tool("prt_shell", "shell", "09:00:04", "09:00:06", "09:12:06"),
                   streamed="09:00:05")),
        (ROOT, "msg_1003", "assistant",
         assistant("09:12:10", "10:12:15", tool("prt_ask", "question", "09:12:11", "09:12:12", "10:12:12"),
                   streamed="09:12:12")),
        (ROOT, "msg_1004", "assistant",
         assistant("10:12:15", "10:12:30",
                   tool("prt_denied", "shell", "10:12:16", "10:12:21", "10:12:25", status="error",
                        error={"type": "permission.rejected", "message": CANARY_OUTPUT}),
                   streamed="10:12:20")),
        (ROOT, "msg_1005", "assistant",
         assistant("10:12:30", "10:32:40",
                   tool("prt_sub", "subagent", "10:12:31", "10:12:36", "10:32:36", metadata={"sessionID": CHILD}),
                   streamed="10:12:35")),
        (ROOT, "msg_1006", "idle", idle("10:32:41")),
        # Turn 2 of the main thread.
        (ROOT, "msg_1007", "user", user("11:00:00")),
        (ROOT, "msg_1008", "assistant", assistant("11:00:01", "11:00:04")),
        (ROOT, "msg_1009", "idle", idle("11:00:05")),
        # The subagent, a child session.
        (CHILD, "msg_2001", "user", user("10:12:37")),
        (CHILD, "msg_2002", "assistant",
         assistant("10:12:40", "10:13:00", tool("prt_read", "read", "10:12:45", "10:12:46", "10:12:50"),
                   streamed="10:12:45")),
        (CHILD, "msg_2003", "assistant", assistant("10:25:00", "10:25:10")),
        (CHILD, "msg_2004", "idle", idle("10:25:11")),
        # A tool still running when the session was killed: a start without an end.
        (CHILD, "msg_2005", "assistant",
         assistant("10:40:00", "10:40:01", tool("prt_hung", "shell", "10:40:00", "10:40:01", None, status="running"))),
    ]  # fmt: skip


def build(home: Path) -> Path:
    """The conformance home: one root session with a subagent, all on one model and day.

    Main thread, turn 1: prompt 09:00:00 .. tool start 09:00:06 (6 s of gaps); a 12 minute shell run
    09:00:06..09:12:06 (720, a tool run counts in full though longer than the gap limit); 09:12:06 ..
    09:12:12 (6); a ``question`` 09:12:12..10:12:12 is a person answering (nothing); 10:12:12 .. 10:12:36
    (24, the refused shell 10:12:21..10:12:25 only through the gaps); the ``subagent`` call
    10:12:36..10:32:36 waits for the child (no tool run, and its 20 minute gap is too long); 10:32:36 ..
    turn end 10:32:41 (5). Turn 2: 11:00:00 .. 11:00:05 (5). Main thread: 766 s.
    Child: prompt 10:12:37 .. 10:13:00 (23); 10:13:00 .. 10:25:00 is 12 minutes (nothing); 10:25:00 ..
    turn end 10:25:11 (11); a new message at 10:40:00 after the turn end, its hung tool started 10:40:01 and
    never ended (1 s of gap, no run). Child: 35 s. The record: 766 + 35 = 801 s.
    """
    data_dir = home / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "opencode.db")
    db.execute("PRAGMA journal_mode = DELETE")
    db.executescript(V2_SCHEMA)
    for sid, parent, title in ((ROOT, None, f"Fix {CANARY_PROMPT}"), (CHILD, ROOT, "Subagent")):
        db.execute(
            "INSERT INTO session_v2 (id, project_id, parent_id, slug, directory, title, version, time_created,"
            " time_updated) VALUES (?, 'prj_1', ?, ?, ?, ?, '2.2.5', ?, ?)",
            (sid, parent, sid[-6:], CWD, title, t("08:00:00"), t("08:00:00")),
        )
    for seq, (sid, mid, kind, data) in enumerate(rows()):
        when = data["time"]["created"]
        db.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, sid, kind, seq, when, when, json.dumps(data)),
        )
    db.commit()
    db.close()
    return data_dir
