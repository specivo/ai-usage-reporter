"""Local state in SQLite (``state.db`` in the state directory).

- ``files``: one row per log file with its identity (inode, device, size,
  mtime) and the parser version that read it, plus the parsed usage events
  and thread events (for active time).
  A file whose identity and parser version are unchanged is not parsed again.
  These rows hold local paths and working directories; they never leave the
  machine.
- ``buckets``: one row per sent record: its idempotency key, the hash and the
  JSON of the last payload (the privacy-filtered wire record, nothing more),
  the last status and error code. Only buckets whose payload changed are
  sent again. Rows with status ``pending`` are the retry queue; they are sent
  from the stored payload even after the tool deleted its logs.
- ``session_hints``: the ``SPECIVO_ISSUE`` captured for a session.
- ``runs``: a summary of each push, for ``status`` and ``doctor``.
- ``meta``: the schema version, and whether the server refused
  ``issue_ref_source: "agent_log"`` lately (see :mod:`ai_usage_reporter.pipeline`).
- ``hermes_counters`` and ``hermes_usage``: Hermes keeps only running totals
  per session and model, so the reporter remembers the totals it last saw
  (``hermes_counters``) and the growth it observed per UTC day
  (``hermes_usage``). See :mod:`ai_usage_reporter.adapters.hermes`. Local only,
  like ``files``.
- ``session_routes``: the route a tool's hook saw when a session started (for
  Claude Code, the host of ``ANTHROPIC_BASE_URL``; ``''`` when it was unset).
  Only the host is kept, never the URL. See
  :mod:`ai_usage_reporter.adapters.claude_code`.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_usage_reporter.model import ThreadEvent, UsageEvent, event_from_cache

SCHEMA_VERSION = 3
#: The state database's file name in the state directory.
STATE_FILE = "state.db"

_SESSION_ROUTES = """
CREATE TABLE IF NOT EXISTS session_routes (
    tool TEXT NOT NULL,
    session_id TEXT NOT NULL,
    host TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (tool, session_id)
)
"""

_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    inode INTEGER NOT NULL,
    device INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    lines INTEGER NOT NULL DEFAULT 0,
    malformed_lines INTEGER NOT NULL DEFAULT 0,
    skipped_events INTEGER NOT NULL DEFAULT 0,
    events BLOB NOT NULL,
    parsed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS buckets (
    idempotency_key TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    model TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    session_id TEXT,
    payload_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    drop_explicit INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    entry_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_hints (
    session_id TEXT PRIMARY KEY,
    issue_ref TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hermes_counters (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL,
    billing_base_url TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    task TEXT NOT NULL,
    db_path TEXT NOT NULL,
    api_calls INTEGER NOT NULL,
    input INTEGER NOT NULL,
    output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_write INTEGER NOT NULL,
    reasoning INTEGER NOT NULL,
    actual_cost_usd TEXT NOT NULL,
    last_seen TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
CREATE TABLE IF NOT EXISTS hermes_usage (
    db_path TEXT NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL,
    billing_base_url TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    task TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    root_session TEXT NOT NULL,
    profile TEXT,
    unattended INTEGER NOT NULL DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    api_calls INTEGER NOT NULL DEFAULT 0,
    input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    reasoning INTEGER NOT NULL DEFAULT 0,
    cost TEXT NOT NULL DEFAULT '0',
    cost_complete INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    PRIMARY KEY (db_path, session_id, model, billing_provider, billing_base_url, billing_mode, task, usage_date)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sink TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}'
);
"""
    + _SESSION_ROUTES
    + ";\n"
)

#: Statuses of a bucket row.
SENT = "sent"
PENDING = "pending"
REJECTED = "rejected"
CONFLICT = "attribution_conflict"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _as_int64(value: int) -> int:
    """*value* folded into SQLite's signed 64-bit INTEGER range.

    Windows file IDs and volume serial numbers can use all 64 bits (ReFS file
    IDs even 128), which SQLite cannot store. The identity only has to change
    when the file does, so keeping the low 64 bits as a signed number is enough.
    """
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


@dataclass(frozen=True, slots=True)
class FileIdentity:
    inode: int
    device: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: Path, companions: tuple[Path, ...] = ()) -> FileIdentity:
        """The identity of *path*; a change in any companion file (that exists) changes it too."""
        st = path.stat()
        size, mtime_ns = st.st_size, st.st_mtime_ns
        for extra in companions:
            try:
                other = extra.stat()
            except OSError:
                continue
            # A write to the companion changes its size or its modification time.
            size += other.st_size
            mtime_ns = max(mtime_ns, other.st_mtime_ns)
        return cls(inode=_as_int64(st.st_ino), device=_as_int64(st.st_dev), size=size, mtime_ns=mtime_ns)


@dataclass(frozen=True, slots=True)
class BucketRow:
    idempotency_key: str
    tool: str
    model: str
    usage_date: str
    session_id: str | None
    payload_hash: str
    payload: dict[str, Any]
    status: str
    error_code: str | None
    error_message: str | None
    warnings: list[str]
    drop_explicit: bool
    attempts: int
    entry_id: int | None
    updated_at: str


class State:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None or int(row["value"]) < SCHEMA_VERSION:
            # Every change so far only added tables, which the script above creates.
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        self.db.commit()

    @classmethod
    def in_memory(cls) -> State:
        return cls(":memory:")

    @classmethod
    def scratch_copy(cls, path: Path) -> State:
        """An in-memory copy of the state database at *path* (empty when there is none).

        A dry run works on it, so it computes exactly what a push would send
        while the real state is never written.
        """
        state = cls.in_memory()
        if path.exists():
            source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                source.backup(state.db)
            finally:
                source.close()
            # A copy from an older reporter lacks the newer tables.
            state.db.executescript(_SCHEMA)
            state.db.commit()
        return state

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- files -----------------------------------------------------------------

    def cached_events(
        self, path: Path, identity: FileIdentity, parser_version: str
    ) -> list[UsageEvent | ThreadEvent] | None:
        row = self.db.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        if row is None or row["parser_version"] != parser_version:
            return None
        if (row["inode"], row["device"], row["size"], row["mtime_ns"]) != (
            identity.inode,
            identity.device,
            identity.size,
            identity.mtime_ns,
        ):
            return None
        data = json.loads(zlib.decompress(row["events"]))
        return [event_from_cache(item) for item in data]

    def store_events(
        self,
        path: Path,
        adapter: str,
        identity: FileIdentity,
        parser_version: str,
        events: Sequence[UsageEvent | ThreadEvent],
        *,
        lines: int,
        malformed_lines: int,
        skipped_events: int,
    ) -> None:
        blob = zlib.compress(json.dumps([e.to_cache() for e in events]).encode())
        self.db.execute(
            "INSERT OR REPLACE INTO files (path, adapter, inode, device, size, mtime_ns, parser_version, lines,"
            " malformed_lines, skipped_events, events, parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(path),
                adapter,
                identity.inode,
                identity.device,
                identity.size,
                identity.mtime_ns,
                parser_version,
                lines,
                malformed_lines,
                skipped_events,
                blob,
                _now(),
            ),
        )

    def forget_missing_files(self, adapter: str, present: set[str]) -> int:
        rows = self.db.execute("SELECT path FROM files WHERE adapter = ?", (adapter,)).fetchall()
        gone = [r["path"] for r in rows if r["path"] not in present]
        self.db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
        return len(gone)

    def file_stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for row in self.db.execute(
            "SELECT adapter, COUNT(*) AS files, SUM(lines) AS lines, SUM(malformed_lines) AS malformed,"
            " SUM(skipped_events) AS skipped FROM files GROUP BY adapter"
        ):
            out[row["adapter"]] = {
                "files": int(row["files"]),
                "lines": int(row["lines"] or 0),
                "malformed_lines": int(row["malformed"] or 0),
                "skipped_events": int(row["skipped"] or 0),
            }
        return out

    # -- buckets ---------------------------------------------------------------

    def _row(self, row: sqlite3.Row) -> BucketRow:
        return BucketRow(
            idempotency_key=row["idempotency_key"],
            tool=row["tool"],
            model=row["model"],
            usage_date=row["usage_date"],
            session_id=row["session_id"],
            payload_hash=row["payload_hash"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            warnings=json.loads(row["warnings"]),
            drop_explicit=bool(row["drop_explicit"]),
            attempts=int(row["attempts"]),
            entry_id=row["entry_id"],
            updated_at=row["updated_at"],
        )

    def bucket(self, key: str) -> BucketRow | None:
        row = self.db.execute("SELECT * FROM buckets WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row(row) if row is not None else None

    def buckets(self, status: str | None = None) -> Iterator[BucketRow]:
        if status is None:
            cursor = self.db.execute("SELECT * FROM buckets ORDER BY usage_date, idempotency_key")
        else:
            cursor = self.db.execute(
                "SELECT * FROM buckets WHERE status = ? ORDER BY usage_date, idempotency_key", (status,)
            )
        for row in cursor.fetchall():
            yield self._row(row)

    def status_counts(self) -> dict[str, int]:
        return {
            row["status"]: int(row["n"])
            for row in self.db.execute("SELECT status, COUNT(*) AS n FROM buckets GROUP BY status")
        }

    def error_counts(self) -> dict[str, int]:
        return {
            row["error_code"]: int(row["n"])
            for row in self.db.execute(
                "SELECT error_code, COUNT(*) AS n FROM buckets WHERE error_code IS NOT NULL GROUP BY error_code"
            )
        }

    def save_bucket(
        self,
        wire: dict[str, Any],
        payload_hash: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        warnings: list[str] | None = None,
        entry_id: int | None = None,
        drop_explicit: bool | None = None,
        count_attempt: bool = True,
    ) -> None:
        key = wire["idempotency_key"]
        current = self.bucket(key)
        attempts = (current.attempts if current else 0) + (1 if count_attempt else 0)
        flag = drop_explicit if drop_explicit is not None else (current.drop_explicit if current else False)
        self.db.execute(
            "INSERT OR REPLACE INTO buckets (idempotency_key, tool, model, usage_date, session_id, payload_hash,"
            " payload, status, error_code, error_message, warnings, drop_explicit, attempts, entry_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                wire["tool"],
                wire["model"],
                wire["usage_date"],
                wire.get("session_id"),
                payload_hash,
                json.dumps(wire, sort_keys=True),
                status,
                error_code,
                error_message,
                json.dumps(warnings or []),
                int(flag),
                attempts,
                entry_id if entry_id is not None else (current.entry_id if current else None),
                _now(),
            ),
        )

    # -- session hints ----------------------------------------------------------

    def set_session_issue(self, session_id: str, issue_ref: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO session_hints (session_id, issue_ref, captured_at) VALUES (?, ?, ?)",
            (session_id, issue_ref, _now()),
        )

    def session_issues(self) -> dict[str, str]:
        return {r["session_id"]: r["issue_ref"] for r in self.db.execute("SELECT * FROM session_hints")}

    # -- session routes ----------------------------------------------------------

    def session_routes(self, tool: str) -> dict[str, str]:
        """The route host each session of *tool* started with (``''``: none was set)."""
        rows = self.db.execute("SELECT session_id, host FROM session_routes WHERE tool = ?", (tool,))
        return {str(r["session_id"]): str(r["host"]) for r in rows}

    # -- meta --------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- runs --------------------------------------------------------------------

    def start_run(self, sink: str) -> int:
        cursor = self.db.execute("INSERT INTO runs (started_at, sink) VALUES (?, ?)", (_now(), sink))
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?", (_now(), json.dumps(summary), run_id)
        )

    def last_run(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "sink": row["sink"],
            "summary": json.loads(row["summary"]),
        }

    def commit(self) -> None:
        self.db.commit()


def record_session_route(path: Path, tool: str, session_id: str, host: str, *, replace: bool, timeout: float) -> None:
    """Keep the route *host* a hook saw for a session, without opening the whole state.

    Hooks call this, so it waits at most *timeout* seconds for a push that holds
    the database. Without *replace* a session already recorded is left as it is
    (and nothing is written).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=timeout)
    try:
        db.execute(_SESSION_ROUTES)
        if not replace:
            found = db.execute(
                "SELECT 1 FROM session_routes WHERE tool = ? AND session_id = ?", (tool, session_id)
            ).fetchone()
            if found is not None:
                return
        db.execute(
            "INSERT OR REPLACE INTO session_routes (tool, session_id, host, recorded_at) VALUES (?, ?, ?, ?)",
            (tool, session_id, host, _now()),
        )
        db.commit()
    finally:
        db.close()
