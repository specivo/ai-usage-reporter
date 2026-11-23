"""Builds a synthetic Hermes home: ``state.db`` for the default profile and for a ``research`` profile.

The databases are built at test time from a hand-written DDL that mirrors the
tables and columns of Hermes's ``state.db`` at ``schema_version`` 33 which the
adapter reads (a few columns the adapter ignores are kept so the probe meets a
realistic table). Everything is invented: session ids, paths, prompts and
numbers. The CANARY-* strings are planted in prompts, tool output and paths;
the conformance kit checks that none of them leaves the reporter.

Scenarios: two models and an auxiliary task in one session, a subagent child
session, a gateway route with a provider-reported cost, a subscription route
run by cron, a local engine, a second profile, and issue marks from Specivo
tool calls (an update and a created issue).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"

ROOT = "20261105_090000_a1b2c3"
CHILD = "20261105_091000_d4e5f6"
CRON = "20261105_020000_c7c8c9"
LOCAL = "20261106_100000_0a0b0c"
RESEARCH = "20261107_080000_e1e2e3"
CWD = f"/work/{CANARY_PATH}/app"

SCHEMA_V33 = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    created_source TEXT,
    user_id TEXT,
    model TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    title TEXT,
    last_activity_at REAL,
    api_call_count INTEGER DEFAULT 0,
    profile_name TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ts(text: str) -> float:
    """Epoch seconds of a UTC ISO time, as Hermes stores them."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


@dataclass(frozen=True, slots=True)
class Usage:
    session: str
    model: str
    provider: str
    base_url: str
    mode: str
    task: str
    calls: int
    tokens: tuple[int, int, int, int, int]  # input, output (with reasoning), cache read, cache write, reasoning
    first: str
    last: str
    actual: float = 0.0
    cost_source: str = "official_docs_snapshot"

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (self.session, self.model, self.provider, self.base_url, self.mode, self.task)


ANTHROPIC = "https://api.anthropic.com"
OPENROUTER = "https://openrouter.ai/api/v1"
CODEX = "https://chatgpt.com/backend-api/codex"
OLLAMA = "http://localhost:11434/v1"
NOUS = "https://inference-api.nousresearch.com/v1"

DEFAULT_USAGE = (
    Usage(ROOT, "claude-sonnet-4-5", "anthropic", ANTHROPIC, "", "", 6, (3000, 1500, 20000, 4000, 300),
          "2026-11-05T09:00:00", "2026-11-05T09:20:00"),
    Usage(ROOT, "claude-haiku-4-5", "anthropic", ANTHROPIC, "", "title_generation", 1, (200, 20, 0, 0, 0),
          "2026-11-05T09:00:05", "2026-11-05T09:00:05"),
    Usage(ROOT, "anthropic/claude-sonnet-4.6", "openrouter", OPENROUTER, "", "", 2, (900, 400, 0, 0, 0),
          "2026-11-05T09:30:00", "2026-11-05T09:35:00", actual=0.0421, cost_source="provider_cost_api"),
    Usage(CHILD, "claude-sonnet-4-5", "anthropic", ANTHROPIC, "", "", 3, (800, 350, 5000, 0, 50),
          "2026-11-05T09:10:00", "2026-11-05T09:15:00"),
    Usage(CRON, "gpt-5.1", "openai-codex", CODEX, "subscription_included", "", 4, (2500, 900, 18000, 0, 100),
          "2026-11-05T02:00:00", "2026-11-05T02:04:30", cost_source="none"),
    Usage(LOCAL, "qwen3-coder:30b", "custom", OLLAMA, "", "", 5, (7000, 1200, 0, 0, 0),
          "2026-11-06T10:00:00", "2026-11-06T10:40:00", cost_source="none"),
)  # fmt: skip

RESEARCH_USAGE = (
    Usage(RESEARCH, "Hermes-4-405B", "nous", NOUS, "", "", 2, (1500, 600, 0, 0, 0),
          "2026-11-07T08:00:00", "2026-11-07T08:06:00"),
)  # fmt: skip


def _connect(db: Path, version: int | None, schema: str) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(schema)
    if version is not None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    return conn


def session(
    conn: sqlite3.Connection,
    sid: str,
    source: str,
    started: str,
    *,
    parent: str | None = None,
    profile: str | None = None,
    cwd: str | None = CWD,
    branch: str | None = "main",
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, source, created_source, parent_session_id, started_at, cwd, git_branch,"
        " profile_name, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, source, source, parent, ts(started), cwd, branch, profile, f"Title {CANARY_PROMPT}"),
    )


def put_usage(conn: sqlite3.Connection, usage: Usage) -> None:
    inp, out, c_read, c_write, reasoning = usage.tokens
    conn.execute(
        "INSERT OR REPLACE INTO session_model_usage (session_id, model, billing_provider, billing_base_url,"
        " billing_mode, task, api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, cost_source, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *usage.key,
            usage.calls,
            inp,
            out,
            c_read,
            c_write,
            reasoning,
            0.05,
            usage.actual,
            "actual" if usage.actual else "estimated",
            usage.cost_source,
            ts(usage.first),
            ts(usage.last),
        ),
    )


def message(
    conn: sqlite3.Connection,
    sid: str,
    role: str,
    when: str,
    content: str | None = None,
    *,
    tool_calls: list[dict[str, object]] | None = None,
    tool_call_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sid, role, content, tool_call_id, json.dumps(tool_calls) if tool_calls else None, ts(when)),
    )


def call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    """A tool call as Hermes stores it (OpenAI shape, arguments as JSON text)."""
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def build_default(db: Path, *, version: int | None = 33, schema: str = SCHEMA_V33, data: bool = True) -> Path:
    """The default profile's database; *data* false leaves it empty (for schemas the rows do not fit)."""
    conn = _connect(db, version, schema)
    if not data:
        conn.commit()
        conn.close()
        return db
    with conn:
        session(conn, ROOT, "cli", "2026-11-05T09:00:00")
        session(conn, CHILD, "subagent", "2026-11-05T09:10:00", parent=ROOT)
        session(conn, CRON, "cron", "2026-11-05T02:00:00", cwd=None, branch=None)
        session(conn, LOCAL, "cli", "2026-11-06T10:00:00")
        for usage in DEFAULT_USAGE:
            put_usage(conn, usage)
        message(conn, ROOT, "user", "2026-11-05T09:00:00", f"Fix the export {CANARY_PROMPT}")
        message(
            conn,
            ROOT,
            "assistant",
            "2026-11-05T09:05:00",
            None,
            tool_calls=[call("call_1", "mcp__specivo__specivo_update_issue", {"issue_ref": "acme-12", "status_id": 2})],
        )
        message(conn, ROOT, "tool", "2026-11-05T09:05:01", f"Updated {CANARY_OUTPUT}", tool_call_id="call_1")
        message(
            conn,
            ROOT,
            "assistant",
            "2026-11-05T09:25:00",
            None,
            tool_calls=[
                call("call_2", "mcp__specivo__specivo_create_issue", {"subject": f"Export {CANARY_PROMPT}"}),
                call("call_3", "mcp__specivo__specivo_show_issue", {"issue_ref": "ACME-1"}),
            ],
        )
        message(conn, ROOT, "tool", "2026-11-05T09:25:01", "Created issue ACME-13: Export", tool_call_id="call_2")
        message(conn, CHILD, "assistant", "2026-11-05T09:11:00", f"Looked at {CANARY_OUTPUT}")
    conn.close()
    return db


def build_research(db: Path, *, version: int | None = 33) -> Path:
    with _connect(db, version, SCHEMA_V33) as conn:
        session(conn, RESEARCH, "cli", "2026-11-07T08:00:00", profile="research", cwd=None, branch=None)
        for usage in RESEARCH_USAGE:
            put_usage(conn, usage)
    conn.close()
    return db


def build(home: Path) -> Path:
    """``home/.hermes`` with the default profile's database and a ``research`` profile; returns the Hermes home."""
    hermes = home / ".hermes"
    build_default(hermes / "state.db")
    build_research(hermes / "profiles" / "research" / "state.db")
    return hermes


def update_usage(db: Path, usage: Usage) -> None:
    """Write *usage* over its row (Hermes's running totals after more requests)."""
    conn = sqlite3.connect(db)
    with conn:
        put_usage(conn, usage)
    conn.close()
