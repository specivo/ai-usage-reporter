"""OpenCode: the SQLite database in its data directory, and the older JSON ``storage/`` layout.

Where the data is:

- The data directory is ``$XDG_DATA_HOME/opencode``, by default
  ``~/.local/share/opencode`` on every system. The ``[sources.opencode] roots``
  setting names other data directories (or database files).
- The database is ``opencode.db``. ``OPENCODE_DB`` names another file (a
  relative name is taken inside the data directory); test builds of OpenCode
  use ``opencode-<channel>.db``, which are read too.
- Releases before the SQLite database kept one JSON file per message under
  ``storage/message/<session id>/`` and one per session under
  ``storage/session/``. They are read when present.

What the data looks like, and what this adapter does with it:

- OpenCode 2 keeps sessions in ``session_v2`` (``id``, ``parent_id``,
  ``directory``, ``version``) and messages in ``session_message``, one row per
  message with a ``type`` and a JSON ``data`` column. OpenCode 1 used
  ``session`` and ``message`` with the role inside ``data``. Both are read;
  message ids are unique across sessions, so a message found twice (copied
  by the migration from one to the other, by a fork or into a backup) counts
  once.
- An assistant message carries the model (``model.id`` and
  ``model.providerID``, or ``modelID`` and ``providerID`` in OpenCode 1 and
  the JSON files), ``tokens`` (``input``, ``output``, ``reasoning``,
  ``cache.read``, ``cache.write``), ``cost`` and ``time.created`` in epoch
  milliseconds. One message is one model request.
- **Token semantics.** OpenCode stores its tokens already split into
  disjoint classes: ``input`` excludes cache reads and writes, and ``output``
  excludes ``reasoning``. That is the contract's semantics, so the counts are
  taken as they are. (In real databases cache reads are often many times
  larger than ``input``, and ``reasoning`` larger than ``output``, which
  could not happen if either were included.) OpenCode records one cache-write
  count without a lifetime; it is reported as a 5-minute write, the default
  cache lifetime of the providers that bill writes.
- ``cost`` is what OpenCode computed for the message, in US dollars. It is
  sent as ``reported_cost`` when every message of a bucket has a cost above
  zero in total; a zero cost (a subscription or an unpriced model) sends none.
- A session with a ``parent_id`` is a subagent's session. Its messages are
  reported under the root session, so a session's total includes the work it
  fanned out; ``push --session <child id>`` selects the root session.
- Messages without tokens (errors, aborted requests) are skipped.
- **Issue marks.** OpenCode 2 keeps a message's tool calls in its
  ``content`` as parts of type ``tool``: a Specivo MCP tool called directly
  (``name`` such as ``specivo_specivo_timer``, ``state.input``,
  ``state.content``), or calls made from code (``state.metadata.toolCalls``,
  each with ``tool`` such as ``specivo.specivo_timer`` and ``input``, but
  without its result). Marking Specivo calls (see
  :mod:`ai_usage_reporter.issue_marks`) become markers at the part's
  ``time.created``; ``specivo_create_issue`` marks only when its result is
  there. A child session's marks are its own (``sub_session_id``). OpenCode 1
  kept parts in a table of their own, which is not read: its sessions are left
  to the server's match of MCP sessions.

The database is opened read-only (``mode=ro``) and only for the time of two
``SELECT`` statements; the reporter never writes to it, never checkpoints its
write-ahead log and never holds a lock. When OpenCode holds a lock, the reader
retries for a moment and then leaves the database for the next run
(``ai-usage doctor`` counts it as busy).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_dict, get_str
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call
from ai_usage_reporter.model import SourceBusyError, SourceFile, TokenCounts, UsageEvent, count, parse_timestamp

#: How often and how long to wait for a database OpenCode holds locked.
BUSY_ATTEMPTS = 3
BUSY_WAIT_SECONDS = 0.25
#: Parent links are followed at most this deep (and never round a cycle).
MAX_DEPTH = 64

_DB_KIND = "db"
_JSON_KIND = "json"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    parent: str | None
    directory: str | None
    version: str | None


def data_dirs(ctx: DiscoveryContext) -> list[Path]:
    if ctx.roots:
        raw = list(ctx.roots)
    else:
        xdg = ctx.env.get("XDG_DATA_HOME", "").strip()
        raw = [(Path(xdg) if xdg else ctx.home / ".local" / "share") / "opencode"]
    found: list[Path] = []
    for path in raw:
        if path not in found and path.exists():
            found.append(path)
    return found


def _database_files(directory: Path, env: Mapping[str, str]) -> list[Path]:
    if directory.is_file():
        return [directory]
    names: list[Path] = []
    override = env.get("OPENCODE_DB", "").strip()
    if override and override != ":memory:":
        names.append(Path(override) if Path(override).is_absolute() else directory / override)
    names.append(directory / "opencode.db")
    names.extend(sorted(directory.glob("opencode-*.db")))
    unique: list[Path] = []
    for path in names:
        if path not in unique and path.is_file():
            unique.append(path)
    return unique


def root_of(session: str, sessions: Mapping[str, SessionInfo]) -> str:
    """The top of *session*'s parent chain."""
    current, seen = session, {session}
    for _ in range(MAX_DEPTH):
        info = sessions.get(current)
        if info is None or not info.parent or info.parent in seen:
            return current
        seen.add(info.parent)
        current = info.parent
    return current


def _tokens(value: object) -> TokenCounts:
    tokens: dict[str, Any] = value if isinstance(value, dict) else {}
    cache: dict[str, Any] = get_dict(tokens, "cache") or {}
    return TokenCounts(
        input=count(tokens.get("input")),
        output=count(tokens.get("output")),
        reasoning=count(tokens.get("reasoning")),
        cache_read=count(cache.get("read")),
        cache_write_5m=count(cache.get("write")),
    )


def _cost(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return str(amount)


def _open_read_only(path: Path, sleep: Callable[[float], None]) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    for attempt in range(BUSY_ATTEMPTS):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=BUSY_WAIT_SECONDS)
            conn.execute("PRAGMA query_only = 1")
            # Touch the schema now, so that a lock shows up here and not halfway through.
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            if attempt == BUSY_ATTEMPTS - 1:
                raise SourceBusyError(path.name) from None
            sleep(BUSY_WAIT_SECONDS)
    raise SourceBusyError(path.name)  # pragma: no cover - the loop always returns or raises


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info('{table}')")}


class OpenCodeAdapter:
    name = "opencode"
    provider = "unknown"
    parser_version = "opencode/2"

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    # -- discovery -------------------------------------------------------------------

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        for directory in data_dirs(ctx):
            for db in _database_files(directory, ctx.env):
                wal = db.with_name(db.name + "-wal")
                yield SourceFile(adapter=self.name, path=db, root=db.parent, session_hint=_DB_KIND, companions=(wal,))
            if directory.is_dir():
                yield from self._discover_json(directory / "storage")

    def _discover_json(self, storage: Path) -> Iterator[SourceFile]:
        message_dirs = [storage / "message", storage / "session" / "message"]
        if not any(d.is_dir() for d in message_dirs):
            return
        sessions = _json_sessions(storage)
        for messages in message_dirs:
            if not messages.is_dir():
                continue
            for path in sorted(messages.glob("*/*.json")):
                if path.is_file():
                    session = path.parent.name
                    yield SourceFile(
                        adapter=self.name,
                        path=path,
                        root=storage,
                        # "json:<root session>:<own session>" tells parse where the message belongs.
                        session_hint=f"{_JSON_KIND}:{root_of(session, sessions)}:{session}",
                    )

    # -- parsing -----------------------------------------------------------------------

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        hint = src.session_hint or ""
        if hint.startswith(f"{_JSON_KIND}:"):
            yield from self._parse_json_message(src, hint)
        else:
            yield from self._parse_database(src)

    def _parse_database(self, src: SourceFile) -> Iterator[UsageEvent]:
        conn = _open_read_only(src.path, self._sleep)
        try:
            sessions, rows = self._read_rows(conn)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise SourceBusyError(src.path.name) from None
            raise
        finally:
            conn.close()
        for message_id, session, created, raw in rows:
            src.stats.lines += 1
            try:
                data = json.loads(raw) if isinstance(raw, str | bytes) else None
            except ValueError:
                data = None
            if not isinstance(data, dict):
                src.stats.malformed_lines += 1
                continue
            event = self._event(src, data, message_id or get_str(data, "id"), session, created, sessions)
            if event is not None:
                yield event
            yield from self._marks(data, session, created, sessions)

    @staticmethod
    def _read_rows(
        conn: sqlite3.Connection,
    ) -> tuple[dict[str, SessionInfo], list[tuple[str | None, str, Any, Any]]]:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        sessions: dict[str, SessionInfo] = {}
        for table in ("session", "session_v2"):  # the newer table wins on a shared id
            if table not in tables:
                continue
            cols = _columns(conn, table)
            if "id" not in cols:
                continue
            picks = [c if c in cols else "NULL" for c in ("parent_id", "directory", "version")]
            for sid, parent, directory, version in conn.execute(f"SELECT id, {', '.join(picks)} FROM {table}"):
                sessions[str(sid)] = SessionInfo(
                    parent=str(parent) if parent else None,
                    directory=str(directory) if directory else None,
                    version=str(version) if version else None,
                )
        rows: list[tuple[str | None, str, Any, Any]] = []
        if "session_message" in tables and {"id", "session_id", "data"} <= _columns(conn, "session_message"):
            cols = _columns(conn, "session_message")
            where = "WHERE type = 'assistant'" if "type" in cols else ""
            created = "time_created" if "time_created" in cols else "NULL"
            query = f"SELECT id, session_id, {created}, data FROM session_message {where}"
            rows.extend((str(a), str(b), c, d) for a, b, c, d in conn.execute(query))
        if "message" in tables and {"id", "session_id", "data"} <= _columns(conn, "message"):
            created = "time_created" if "time_created" in _columns(conn, "message") else "NULL"
            query = f"SELECT id, session_id, {created}, data FROM message"
            rows.extend((str(a), str(b), c, d) for a, b, c, d in conn.execute(query))
        return sessions, rows

    def _parse_json_message(self, src: SourceFile, hint: str) -> Iterator[UsageEvent]:
        _, root, own = (hint.split(":", 2) + ["", ""])[:3]
        src.stats.lines += 1
        try:
            data = json.loads(src.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            src.stats.malformed_lines += 1
            return
        if not isinstance(data, dict):
            src.stats.malformed_lines += 1
            return
        own = get_str(data, "sessionID") or own or src.path.parent.name
        info = _json_session_info(src.root, own)
        sessions = {own: SessionInfo(parent=root if root and root != own else None, directory=None, version=None)}
        if info is not None:
            sessions[own] = SessionInfo(parent=sessions[own].parent, directory=info.directory, version=info.version)
        event = self._event(src, data, get_str(data, "id") or src.path.stem, own, None, sessions, root=root or own)
        if event is not None:
            yield event
        yield from self._marks(data, own, None, sessions, root=root or own)

    def _event(
        self,
        src: SourceFile,
        data: dict[str, Any],
        message_id: str | None,
        session: str,
        row_created: Any,
        sessions: Mapping[str, SessionInfo],
        *,
        root: str | None = None,
    ) -> UsageEvent | None:
        role = data.get("role")
        if role is not None and role != "assistant":
            return None
        tokens = _tokens(data.get("tokens"))
        if tokens.is_zero():
            return None
        when = parse_timestamp((get_dict(data, "time") or {}).get("created")) or parse_timestamp(row_created)
        if when is None:
            src.stats.skipped_events += 1
            return None
        model = get_str(data, "model", "id") or get_str(data, "modelID")
        provider = get_str(data, "model", "providerID") or get_str(data, "providerID")
        top = root or root_of(session, sessions)
        info = sessions.get(session)
        cost = _cost(data.get("cost"))
        src.stats.events += 1
        return UsageEvent(
            tool=self.name,
            session_id=top,
            sub_session_id=session if session != top else None,
            model=model or "unknown",
            timestamp=when,
            tokens=tokens,
            provider=provider,
            tool_version=info.version if info else None,
            message_id=message_id,
            cwd=get_str(data, "path", "cwd") or (info.directory if info else None),
            source_path=str(src.path),
            cost=cost,
            cost_currency="USD" if cost is not None else None,
        )

    def _marks(
        self,
        data: dict[str, Any],
        session: str,
        row_created: Any,
        sessions: Mapping[str, SessionInfo],
        *,
        root: str | None = None,
    ) -> Iterator[UsageEvent]:
        """Markers for the marking Specivo calls among a message's tool parts."""
        role = data.get("role")
        parts = data.get("content")
        if (role is not None and role != "assistant") or not isinstance(parts, list):
            return
        message_time = parse_timestamp((get_dict(data, "time") or {}).get("created")) or parse_timestamp(row_created)
        top = root or root_of(session, sessions)
        info = sessions.get(session)
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            when = parse_timestamp((get_dict(part, "time") or {}).get("created")) or message_time
            state = get_dict(part, "state") or {}
            if when is None or state.get("status") == "error":
                continue
            calls: list[tuple[object, object, object]] = [
                (part.get("name") or part.get("tool"), state.get("input"), state.get("content", state.get("output")))
            ]
            nested = get_dict(state, "metadata")
            if nested is not None and isinstance(nested.get("toolCalls"), list):
                calls.extend(
                    (call.get("tool"), call.get("input"), None)
                    for call in nested["toolCalls"]
                    if isinstance(call, dict) and call.get("status") != "error"
                )
            for name, arguments, result in calls:
                mark = marking_call(name, arguments)
                if mark == CREATE:
                    mark = created_issue(result) if result is not None else None
                if mark is None:
                    continue
                yield UsageEvent(
                    tool=self.name,
                    session_id=top,
                    sub_session_id=session if session != top else None,
                    model="",
                    timestamp=when,
                    tokens=TokenCounts(),
                    requests=0,
                    cwd=get_str(data, "path", "cwd") or (info.directory if info else None),
                    issue_mark=mark,
                )

    def dedup_key(self, event: UsageEvent) -> str | None:
        if event.message_id is None:
            return None
        # Message ids are unique across sessions, so a message copied by a fork,
        # a migration or a backup counts once.
        return event.message_id

    def hook(self) -> HookSpec:
        return HookSpec(
            kind="opencode-plugin",
            events=("session.idle",),
            notes="a plugin file in the OpenCode config's plugins directory",
        )


def _json_sessions(storage: Path) -> dict[str, SessionInfo]:
    """Sessions of the JSON layout, from ``storage/session/**.json`` (messages excluded)."""
    sessions: dict[str, SessionInfo] = {}
    folder = storage / "session"
    if not folder.is_dir():
        return sessions
    for path in folder.rglob("*.json"):
        if "message" in path.relative_to(folder).parts or "part" in path.relative_to(folder).parts:
            continue
        info = _read_session(path)
        if info is not None:
            sessions[info[0]] = info[1]
    return sessions


def _json_session_info(storage: Path, session: str) -> SessionInfo | None:
    folder = storage / "session"
    if not folder.is_dir():
        return None
    for path in folder.rglob(f"{session}.json"):
        info = _read_session(path)
        if info is not None and info[0] == session:
            return info[1]
    return None


def _read_session(path: Path) -> tuple[str, SessionInfo] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "role" in data:
        return None
    sid = get_str(data, "id")
    if sid is None:
        return None
    return sid, SessionInfo(
        parent=get_str(data, "parentID"), directory=get_str(data, "directory"), version=get_str(data, "version")
    )
