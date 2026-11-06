"""Builds a synthetic OpenCode data directory: a SQLite database and the older JSON storage.

The database is built at test time (a binary file would not review well), with
the tables and columns the adapter reads. Everything is invented: ids, paths,
prompts and numbers. The CANARY-* strings are planted in prompts, tool output,
titles and paths; the conformance kit checks that none of them leaves the
reporter.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"

ROOT = "ses_0001rootaaaaaaaaaaaaaaaa"
CHILD = "ses_0002childbbbbbbbbbbbbbbb"
OTHER = "ses_0003othercccccccccccccc"
LEGACY = "ses_0009legacyddddddddddddd"
CWD = f"/work/{CANARY_PATH}/app"

V2_SCHEMA = """
CREATE TABLE session_v2 (
    id text PRIMARY KEY, project_id text NOT NULL, parent_id text, slug text NOT NULL,
    directory text NOT NULL, title text, version text NOT NULL,
    cost real DEFAULT 0 NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL
);
CREATE TABLE session_message (
    id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, seq integer NOT NULL,
    time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
);
"""


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def assistant(
    when: str,
    model: str,
    provider: str,
    tokens: tuple[int, int, int, int, int] | None,
    cost: float | int | None,
    text: str = "Done.",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "agent": "build",
        "content": [{"type": "text", "text": text}, {"type": "tool", "output": CANARY_OUTPUT}],
        "finish": "stop",
        "model": {"id": model, "providerID": provider},
        "time": {"created": ms(when), "completed": ms(when) + 900},
    }
    if tokens is not None:
        inp, out, reasoning, read, write = tokens
        data["tokens"] = {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write}}
    if cost is not None:
        data["cost"] = cost
    return data


def build(home: Path) -> Path:
    """Write the data directory under *home* (``.local/share/opencode``) and return it."""
    data_dir = home / ".local" / "share" / "opencode"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "opencode.db")
    db.execute("PRAGMA journal_mode = DELETE")
    db.executescript(V2_SCHEMA)
    created = ms("2026-10-05T09:00:00")
    sessions = [
        (ROOT, None, CWD, f"Fix {CANARY_PROMPT}"),
        (CHILD, ROOT, CWD, "Subagent: explore"),
        (OTHER, None, f"/work/{CANARY_PATH}/tools", "Other work"),
    ]
    for sid, parent, directory, title in sessions:
        db.execute(
            "INSERT INTO session_v2 (id, project_id, parent_id, slug, directory, title, version, time_created,"
            " time_updated) VALUES (?, 'prj_1', ?, ?, ?, ?, '2.2.5', ?, ?)",
            (sid, parent, sid[-6:], directory, title, created, created),
        )
    messages: list[tuple[str, str, str, dict[str, Any]]] = [
        (ROOT, "msg_0001", "user", {"text": CANARY_PROMPT, "time": {"created": ms("2026-10-05T10:00:00")}}),
        (
            ROOT,
            "msg_0002",
            "assistant",
            assistant("2026-10-05T10:00:05", "claude-sonnet-4-5", "anthropic", (120, 300, 50, 9000, 2000), 0.0123),
        ),
        (
            ROOT,
            "msg_0003",
            "assistant",
            assistant("2026-10-05T10:01:00", "claude-sonnet-4-5", "anthropic", (40, 90, 0, 11000, 0), 0.02),
        ),
        # The subagent's request, folded into the root session.
        (
            CHILD,
            "msg_0004",
            "assistant",
            assistant("2026-10-05T10:00:30", "claude-sonnet-4-5", "anthropic", (500, 60, 0, 0, 1500), 0.004),
        ),
        # A failed request without tokens is skipped.
        (
            ROOT,
            "msg_0005",
            "assistant",
            {
                "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
                "error": {"message": CANARY_OUTPUT},
                "time": {"created": created},
            },
        ),
        (ROOT, "msg_0006", "idle", {"outcome": "done", "time": {"created": ms("2026-10-05T10:02:00")}}),
        # A subscription model: cost 0, so no reported cost.
        (
            OTHER,
            "msg_0101",
            "assistant",
            assistant("2026-10-05T23:59:59", "gpt-5", "openai", (800, 100, 300, 2000, 0), 0),
        ),
        # Just after midnight UTC: another day.
        (
            OTHER,
            "msg_0102",
            "assistant",
            assistant("2026-10-06T00:00:05", "gpt-5", "openai", (100, 20, 0, 2800, 0), 0.0015),
        ),
    ]
    for seq, (sid, mid, kind, data) in enumerate(messages):
        when = data["time"]["created"]
        db.execute(
            "INSERT INTO session_message (id, session_id, type, seq, time_created, time_updated, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, sid, kind, seq, when, when, json.dumps(data)),
        )
    # A row whose data is not JSON counts as malformed.
    db.execute(
        "INSERT INTO session_message VALUES ('msg_0199', ?, 'assistant', 99, ?, ?, '{broken')",
        (OTHER, created, created),
    )
    db.commit()
    db.close()
    _legacy_storage(data_dir / "storage")
    return data_dir


def _legacy_storage(storage: Path) -> None:
    """The JSON layout of releases before the SQLite database."""
    session = storage / "session" / "prj_legacy" / f"{LEGACY}.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        json.dumps({"id": LEGACY, "projectID": "prj_legacy", "directory": CWD, "title": CANARY_PROMPT,
                    "version": "0.9.0", "time": {"created": ms("2026-10-03T12:00:00")}}),
        encoding="utf-8",
    )  # fmt: skip
    message = {
        "id": "msg_legacy_1",
        "sessionID": LEGACY,
        "role": "assistant",
        "modelID": "claude-sonnet-4-5",
        "providerID": "anthropic",
        "path": {"cwd": CWD, "root": CWD},
        "tokens": {"input": 60, "output": 25, "reasoning": 0, "cache": {"read": 4000, "write": 700}},
        "cost": 0.0031,
        "time": {"created": ms("2026-10-03T12:00:10"), "completed": ms("2026-10-03T12:00:12")},
    }
    target = storage / "message" / LEGACY / "msg_legacy_1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(message), encoding="utf-8")
    user = {"id": "msg_legacy_0", "sessionID": LEGACY, "role": "user", "time": {"created": ms("2026-10-03T12:00:00")}}
    (target.parent / "msg_legacy_0.json").write_text(json.dumps(user), encoding="utf-8")
    part = storage / "part" / "msg_legacy_1" / "prt_1.json"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text(json.dumps({"type": "text", "text": CANARY_OUTPUT}), encoding="utf-8")
