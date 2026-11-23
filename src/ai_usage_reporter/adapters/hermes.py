"""Hermes Agent: ``state.db`` in the Hermes home and in each of its profiles.

Where the data is:

- The Hermes home is ``$HERMES_HOME``, by default ``~/.hermes``
  (``%LOCALAPPDATA%\\hermes`` on Windows). When ``HERMES_HOME`` names a
  profile (``<root>/profiles/<name>``), the whole tree from ``<root>`` is read.
- The default profile's database is ``<home>/state.db``; every other profile
  has ``<home>/profiles/<name>/state.db``. The ``[sources.hermes] roots``
  setting replaces the default homes (a Docker bind mount such as
  ``/opt/data``, a copy from another host) and may also name a database file.

What the data looks like, and what this adapter does with it:

- Hermes keeps **no row per model request**. ``session_model_usage`` holds
  running totals per session, model, billing provider, base URL, billing mode
  and task (``''`` for the main loop, ``title_generation``, ``compression``
  and so on for auxiliary calls), with ``first_seen`` and ``last_seen``.
- **Snapshot deltas.** Each read compares the totals with the ones the
  reporter saw last (``hermes_counters`` in the state database) and adds the
  growth to the UTC day of the row's ``last_seen`` (no later than the run)
  in ``hermes_usage``. The events of a database are all the growth ever
  observed in it, so a record always holds its bucket's complete totals and
  reading again never counts anything twice. Totals that went down (a repair
  or a reset of Hermes's counters) count their current values as new usage;
  the run says how often that happened. The totals are keyed by session and
  route, not by file, so a session moved to another profile, or a copy of a
  database, adds nothing. Without a state database (``doctor``) every row's
  totals count on the day of its ``last_seen``.
- **Token semantics.** Hermes already stores ``input_tokens`` without cache
  reads and writes, whatever the provider's wire format. Its
  ``output_tokens`` include the reasoning it also reports apart, so output is
  sent as ``output - reasoning``. The one cache-write count has no lifetime
  and is sent as a 5-minute write.
- **Lineage.** A session's ``parent_session_id`` (a subagent, a branch, a
  continuation after compression or a reset) is followed to the root, and the
  usage is reported under the root session; ``push --session <child>``
  selects the root. Auxiliary tasks fold into the same session and model.
  ``parent_session_id`` is therefore never sent.
- **Provider, route and cost type.** ``route`` is Hermes's
  ``billing_provider`` as it is (``openrouter``, ``nous``, ``openai-codex``).
  ``provider`` is the model's maker, as the server's price lookup expects:
  through a gateway (``openrouter``, ``litellm``) the model id's own prefix
  (``anthropic/claude-sonnet-4.6`` gives ``anthropic``), ``openai-codex``
  gives ``openai``, and a local engine or a loopback or private base URL
  gives ``local``. ``billing_mode = subscription_included`` sends
  ``cost_type = subscription``, local engines ``local``; otherwise the server
  decides. ``reported_cost`` is the growth of ``actual_cost_usd`` when Hermes
  says the provider reported it (never its own estimate).
- **Profile and unattended.** ``profile`` is the root session's
  ``profile_name``, else the profile directory (``default`` for the home
  itself). A root session whose ``source`` or ``created_source`` is ``cron``
  is ``unattended``.
- **Issue marks.** Assistant messages' ``tool_calls`` (Hermes names MCP tools
  ``mcp__<server>__<tool>``) that mark an issue become markers at the
  message's time; ``specivo_create_issue`` reads the key from its tool result
  row. Only the tool name, the marking arguments and that result are read (see
  :mod:`ai_usage_reporter.issue_marks`).

**Schema.** Hermes changes its database often. Every read checks
``schema_version`` and probes the columns it uses: a database older than
:data:`MIN_SCHEMA_VERSION`, or one missing a required column, is skipped with
a message that names what is missing (the other sources go on); one newer
than :data:`TESTED_SCHEMA_VERSION` is read by its columns, with a note.
Optional columns degrade: no ``task`` folds everything into the main loop, no
``profile_name`` takes the profile from the directory, no usable ``messages``
gives no issue marks.

The database is opened read-only, like OpenCode's, with the same retry when
Hermes holds a lock.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext
from ai_usage_reporter.adapters._routes import resolve_provider
from ai_usage_reporter.adapters.opencode import _is_busy, _open_read_only
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call
from ai_usage_reporter.model import (
    SourceBusyError,
    SourceFile,
    SourceSchemaError,
    TokenCounts,
    UsageEvent,
    count,
    parse_timestamp,
)
from ai_usage_reporter.state import State

#: Hermes's ``schema_version`` from which ``session_model_usage`` exists.
MIN_SCHEMA_VERSION = 20
#: The newest ``schema_version`` the fixtures cover; newer ones are read by their columns.
TESTED_SCHEMA_VERSION = 33

#: Columns read from each table: without a required one the database is skipped.
USAGE_REQUIRED = frozenset(
    {"session_id", "model", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
     "reasoning_tokens", "last_seen"}
)  # fmt: skip
USAGE_OPTIONAL = frozenset(
    {"billing_provider", "billing_base_url", "billing_mode", "task", "api_call_count", "actual_cost_usd",
     "cost_source", "first_seen"}
)  # fmt: skip
SESSIONS_REQUIRED = frozenset({"id", "parent_session_id"})
SESSIONS_OPTIONAL = frozenset({"source", "created_source", "profile_name", "cwd", "git_branch"})
#: ``messages`` is optional as a whole: without these columns there are no issue marks.
MESSAGES_MARKS = frozenset({"session_id", "role", "tool_calls", "timestamp"})
MESSAGES_RESULTS = frozenset({"tool_call_id", "content"})

#: ``sessions.source`` values of runs with nobody in the loop.
UNATTENDED_SOURCES = frozenset({"cron"})
SUBSCRIPTION_MODES = frozenset({"subscription_included"})
#: ``cost_source`` values that mean the provider reported the charge (not a Hermes estimate).
REPORTED_COST_SOURCES = frozenset({"provider_cost_api", "provider_generation_api"})
#: Parent links are followed at most this deep (and never round a cycle).
MAX_DEPTH = 64
#: Tool-call ids looked up per query for specivo_create_issue results.
_CHUNK = 500
_CENT = Decimal("0.00000001")


# -- discovery ---------------------------------------------------------------------------


def hermes_homes(ctx: DiscoveryContext) -> list[Path]:
    """The Hermes homes (or database files) to read, existing ones only."""
    if ctx.roots:
        raw = list(ctx.roots)
    else:
        configured = ctx.env.get("HERMES_HOME", "").strip()
        if configured:
            home = ctx.expand(configured)
            # A profile's home: read the whole tree, the default profile included.
            raw = [home.parent.parent if home.parent.name == "profiles" else home]
        else:
            suffix = ctx.env.get("HERMES_DATA_DIR_SUFFIX", "")
            raw = [ctx.home / f".hermes{suffix}"]
            local_appdata = ctx.env.get("LOCALAPPDATA", "").strip()
            if local_appdata:
                raw.append(Path(local_appdata) / f"hermes{suffix}")
    found: list[Path] = []
    for path in raw:
        if path not in found and path.exists():
            found.append(path)
    return found


def _databases(home: Path) -> list[Path]:
    if home.is_file():
        return [home]
    found = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.is_dir():
        found.extend(sorted(p / "state.db" for p in profiles.iterdir() if not p.name.startswith(".")))
    return [p for p in found if p.is_file()]


def profile_of(db: Path) -> str:
    """The profile a database belongs to by its place: ``profiles/<name>/state.db`` or ``default``."""
    return db.parent.name if db.parent.parent.name == "profiles" else "default"


# -- what is read from a database ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRow:
    parent: str | None
    source: str | None
    created_source: str | None
    profile: str | None
    cwd: str | None
    git_branch: str | None


@dataclass(frozen=True, slots=True)
class UsageRow:
    session_id: str
    model: str
    billing_provider: str
    billing_base_url: str
    billing_mode: str
    task: str
    api_calls: int
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int
    actual_cost: Decimal
    cost_source: str | None
    first_seen: datetime | None
    last_seen: datetime | None

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (self.session_id, self.model, self.billing_provider, self.billing_base_url, self.billing_mode, self.task)

    @property
    def counters(self) -> tuple[int, int, int, int, int, int]:
        return (self.api_calls, self.input, self.output, self.cache_read, self.cache_write, self.reasoning)


@dataclass(slots=True)
class Snapshot:
    version: int | None
    sessions: dict[str, SessionRow]
    usage: list[UsageRow]
    #: (session, when, issue key) of each marking Specivo call.
    marks: list[tuple[str, datetime, str]]
    notes: list[str]


@dataclass(frozen=True, slots=True)
class Context:
    """What a session's usage is reported under (from its root session)."""

    root: str
    profile: str | None
    unattended: bool
    cwd: str | None
    git_branch: str | None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info('{table}')")}


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return Decimal(0)
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    if not amount.is_finite() or amount < 0:
        return Decimal(0)
    return amount.quantize(_CENT)


def schema_version(conn: sqlite3.Connection, tables: set[str]) -> int | None:
    if "schema_version" not in tables:
        return None
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        return None
    return int(row[0])


def probe(conn: sqlite3.Connection) -> tuple[int | None, dict[str, set[str]], list[str]]:
    """The schema version, the columns of the tables read, and notes; raises :class:`SourceSchemaError`."""
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    version = schema_version(conn, tables)
    shown = f"v{version}" if version is not None else "without a version"
    if version is not None and version < MIN_SCHEMA_VERSION:
        raise SourceSchemaError(
            f"Hermes state.db schema {shown} is older than the reporter reads (v{MIN_SCHEMA_VERSION}); update Hermes"
        )
    missing_tables = [t for t in ("session_model_usage", "sessions") if t not in tables]
    if missing_tables:
        raise SourceSchemaError(
            f"Hermes state.db schema {shown} has no {' or '.join(missing_tables)} table;"
            " Hermes schema changed, update the reporter"
        )
    columns: dict[str, set[str]] = {
        t: _columns(conn, t) for t in ("session_model_usage", "sessions", "messages") if t in tables
    }
    missing = sorted(USAGE_REQUIRED - columns["session_model_usage"]) + sorted(
        f"sessions.{c}" for c in SESSIONS_REQUIRED - columns["sessions"]
    )
    if missing:
        raise SourceSchemaError(
            f"Hermes state.db schema {shown} lacks {', '.join(missing)}; Hermes schema changed, update the reporter"
        )
    notes: list[str] = []
    if version is None:
        notes.append("Hermes state.db has no schema_version; read by its columns")
    elif version > TESTED_SCHEMA_VERSION:
        notes.append(
            f"Hermes state.db schema v{version} is newer than the reporter was tested with"
            f" (v{TESTED_SCHEMA_VERSION}); read by its columns, update the reporter when one is out"
        )
    if not MESSAGES_MARKS <= columns.get("messages", set()):
        notes.append("Hermes messages carry no readable tool calls; no issue marks")
    return version, columns, notes


def _pick(columns: set[str], name: str, default: str) -> str:
    return name if name in columns else default


def read_snapshot(conn: sqlite3.Connection) -> Snapshot:
    version, columns, notes = probe(conn)
    usage_cols, session_cols = columns["session_model_usage"], columns["sessions"]
    picks = ", ".join(
        [
            _pick(usage_cols, "billing_provider", "''"),
            _pick(usage_cols, "billing_base_url", "''"),
            _pick(usage_cols, "billing_mode", "''"),
            _pick(usage_cols, "task", "''"),
            _pick(usage_cols, "api_call_count", "0"),
            _pick(usage_cols, "actual_cost_usd", "0"),
            _pick(usage_cols, "cost_source", "NULL"),
            _pick(usage_cols, "first_seen", "NULL"),
        ]
    )
    usage: list[UsageRow] = []
    query = (
        "SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        f" reasoning_tokens, last_seen, {picks} FROM session_model_usage"
    )
    for row in conn.execute(query):
        (sid, model, inp, out, c_read, c_write, reasoning, last_seen) = row[:8]
        (provider, base_url, mode, task, calls, actual, cost_source, first_seen) = row[8:]
        if not sid:
            continue
        usage.append(
            UsageRow(
                session_id=str(sid),
                model=_text(model) or "unknown",
                billing_provider=str(provider or ""),
                billing_base_url=str(base_url or ""),
                billing_mode=str(mode or ""),
                task=str(task or ""),
                api_calls=count(calls),
                input=count(inp),
                output=count(out),
                cache_read=count(c_read),
                cache_write=count(c_write),
                reasoning=count(reasoning),
                actual_cost=_money(actual),
                cost_source=_text(cost_source),
                first_seen=parse_timestamp(first_seen),
                last_seen=parse_timestamp(last_seen),
            )
        )
    sessions: dict[str, SessionRow] = {}
    names = ("source", "created_source", "profile_name", "cwd", "git_branch")
    extra = ", ".join(_pick(session_cols, c, "NULL") for c in names)
    for sid, parent, source, created, profile, cwd, branch in conn.execute(
        f"SELECT id, parent_session_id, {extra} FROM sessions"
    ):
        sessions[str(sid)] = SessionRow(
            parent=_text(parent),
            source=_text(source),
            created_source=_text(created),
            profile=_text(profile),
            cwd=_text(cwd),
            git_branch=_text(branch),
        )
    marks = _read_marks(conn, columns.get("messages", set()))
    return Snapshot(version=version, sessions=sessions, usage=usage, marks=marks, notes=notes)


def _tool_calls(raw: object) -> list[tuple[str | None, object, object]]:
    """``(id, name, arguments)`` of each call in a message's ``tool_calls`` JSON."""
    try:
        data = json.loads(raw) if isinstance(raw, str | bytes) else raw
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    calls: list[tuple[str | None, object, object]] = []
    for call in data:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            name, arguments = function.get("name"), function.get("arguments")
        else:
            name, arguments = call.get("name"), call.get("arguments", call.get("input"))
        call_id = call.get("id") or call.get("call_id")
        calls.append((str(call_id) if call_id else None, name, arguments))
    return calls


def _read_marks(conn: sqlite3.Connection, columns: set[str]) -> list[tuple[str, datetime, str]]:
    if not MESSAGES_MARKS <= columns:
        return []
    marks: list[tuple[str, datetime, str]] = []
    pending: dict[str, tuple[str, datetime]] = {}
    # The filter keeps the read to the few messages that can call a Specivo tool.
    query = (
        "SELECT session_id, timestamp, tool_calls FROM messages WHERE role = 'assistant'"
        " AND tool_calls IS NOT NULL AND tool_calls LIKE '%specivo%'"
    )
    for sid, stamp, raw in conn.execute(query):
        when = parse_timestamp(stamp)
        if not sid or when is None:
            continue
        for call_id, name, arguments in _tool_calls(raw):
            mark = marking_call(name, arguments)
            if mark == CREATE:
                if call_id:
                    pending[call_id] = (str(sid), when)
            elif mark is not None:
                marks.append((str(sid), when, mark))
    if pending and MESSAGES_RESULTS <= columns:
        ids = sorted(pending)
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start : start + _CHUNK]
            places = ", ".join("?" for _ in chunk)
            for call_id, content in conn.execute(
                f"SELECT tool_call_id, content FROM messages WHERE role = 'tool' AND tool_call_id IN ({places})", chunk
            ):
                key = created_issue(content)
                if key is not None and str(call_id) in pending:
                    sid, when = pending[str(call_id)]
                    marks.append((sid, when, key))
    return marks


# -- lineage -----------------------------------------------------------------------------------


def root_of(session: str, sessions: Mapping[str, SessionRow]) -> str:
    """The top of *session*'s parent chain."""
    current, seen = session, {session}
    for _ in range(MAX_DEPTH):
        info = sessions.get(current)
        if info is None or not info.parent or info.parent in seen:
            return current
        seen.add(info.parent)
        current = info.parent
    return current


def context_of(session: str, sessions: Mapping[str, SessionRow], directory_profile: str) -> Context:
    root = root_of(session, sessions)
    top = sessions.get(root)
    own = sessions.get(session)
    unattended = bool(top and (top.source in UNATTENDED_SOURCES or top.created_source in UNATTENDED_SOURCES))
    return Context(
        root=root,
        profile=(top.profile if top else None) or (own.profile if own else None) or directory_profile,
        unattended=unattended,
        cwd=(own.cwd if own else None) or (top.cwd if top else None),
        git_branch=(own.git_branch if own else None) or (top.git_branch if top else None),
    )


# -- the counter ledger --------------------------------------------------------------------------


def _day_start(when: datetime) -> datetime:
    return datetime.combine(when.date(), datetime.min.time(), tzinfo=UTC)


@dataclass(slots=True)
class Ledger:
    """The last totals seen and the growth observed, in the reporter's state database."""

    db: sqlite3.Connection

    def observe(self, db_path: str, row: UsageRow, ctx: Context, now: datetime) -> bool:
        """Record the growth of *row* since the last read; ``True`` when its totals went down."""
        previous = self.db.execute(
            "SELECT api_calls, input, output, cache_read, cache_write, reasoning, actual_cost_usd, last_seen"
            " FROM hermes_counters WHERE session_id = ? AND model = ? AND billing_provider = ?"
            " AND billing_base_url = ? AND billing_mode = ? AND task = ?",
            row.key,
        ).fetchone()
        when = min(row.last_seen or now, now)
        seen = when.isoformat() if row.last_seen else None
        reset = False
        deltas: tuple[int, ...]
        if previous is None:
            deltas = row.counters
            cost = row.actual_cost
            start = row.first_seen or when
        else:
            old = tuple(int(v) for v in previous[:6])
            old_cost = _money(previous[6])
            deltas = tuple(new - prior for new, prior in zip(row.counters, old, strict=True))
            cost = row.actual_cost - old_cost
            if any(d < 0 for d in deltas) or cost < 0:
                reset = True
                deltas, cost = row.counters, row.actual_cost
            elif not any(deltas) and cost == 0 and previous[7] == seen:
                return False  # nothing new
            start = (parse_timestamp(previous[7]) if previous[7] else None) or row.first_seen or when
        self.db.execute(
            "INSERT OR REPLACE INTO hermes_counters (session_id, model, billing_provider, billing_base_url,"
            " billing_mode, task, db_path, api_calls, input, output, cache_read, cache_write, reasoning,"
            " actual_cost_usd, last_seen, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *row.key,
                db_path,
                *row.counters,
                str(row.actual_cost),
                seen,
                now.isoformat(),
            ),
        )
        if not any(deltas) and cost == 0:
            return reset
        start = min(max(start, _day_start(when)), when)
        cost_known = row.cost_source in REPORTED_COST_SOURCES
        existing = self.db.execute(
            "SELECT api_calls, input, output, cache_read, cache_write, reasoning, cost, cost_complete, started_at,"
            " ended_at FROM hermes_usage WHERE db_path = ? AND session_id = ? AND model = ? AND billing_provider = ?"
            " AND billing_base_url = ? AND billing_mode = ? AND task = ? AND usage_date = ?",
            (db_path, *row.key, when.date().isoformat()),
        ).fetchone()
        if existing is not None:
            sums = tuple(int(a) + b for a, b in zip(existing[:6], deltas, strict=True))
            total_cost = _money(existing[6]) + (cost if cost_known else Decimal(0))
            complete = bool(existing[7]) and cost_known
            started = min(datetime.fromisoformat(existing[8]), start)
            ended = max(datetime.fromisoformat(existing[9]), when)
        else:
            sums, complete, started, ended = deltas, cost_known, start, when
            total_cost = cost if cost_known else Decimal(0)
        self.db.execute(
            "INSERT OR REPLACE INTO hermes_usage (db_path, session_id, model, billing_provider, billing_base_url,"
            " billing_mode, task, usage_date, root_session, profile, unattended, cwd, git_branch, api_calls, input,"
            " output, cache_read, cache_write, reasoning, cost, cost_complete, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                db_path,
                *row.key,
                when.date().isoformat(),
                ctx.root,
                ctx.profile,
                int(ctx.unattended),
                ctx.cwd,
                ctx.git_branch,
                *sums,
                str(total_cost),
                int(complete),
                started.isoformat(),
                ended.isoformat(),
            ),
        )
        return reset

    def usage(self, db_path: str) -> Iterator[sqlite3.Row]:
        cursor = self.db.execute(
            "SELECT * FROM hermes_usage WHERE db_path = ? ORDER BY usage_date, session_id, model, billing_provider,"
            " billing_base_url, billing_mode, task",
            (db_path,),
        )
        cursor.row_factory = sqlite3.Row
        yield from cursor.fetchall()


# -- the adapter ------------------------------------------------------------------------------------


class HermesAdapter:
    name = "hermes"
    provider = "unknown"
    parser_version = "hermes/1"

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self._state: State | None = None
        self._scratch: State | None = None
        self._now: datetime | None = None

    def bind_run(self, state: State | None, now: datetime | None) -> None:
        """The pipeline's state database and run instant, for the parses of one run (``None`` after it).

        Without a state database the run uses a scratch one, so its databases
        still share one ledger (a session found in two of them counts once).
        """
        if self._scratch is not None:
            self._scratch.close()
            self._scratch = None
        self._state, self._now = state, now
        if state is None and now is not None:
            self._scratch = State.in_memory()

    # -- discovery -------------------------------------------------------------------------

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        seen: set[Path] = set()
        for home in hermes_homes(ctx):
            for db in _databases(home):
                if db in seen:
                    continue
                seen.add(db)
                wal = db.with_name(db.name + "-wal")
                yield SourceFile(
                    adapter=self.name,
                    path=db,
                    root=home if home.is_dir() else home.parent,
                    session_hint=profile_of(db),
                    companions=(wal,),
                )

    # -- parsing -----------------------------------------------------------------------------

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        conn = _open_read_only(src.path, self._sleep)
        try:
            snapshot = read_snapshot(conn)
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise SourceBusyError(src.path.name) from None
            raise
        finally:
            conn.close()
        src.stats.notes.extend(snapshot.notes)
        now = self._now or datetime.now(UTC)
        own: State | None = None
        state = self._state or self._scratch
        if state is None:
            own = state = State.in_memory()
        try:
            yield from self._events(src, snapshot, Ledger(state.db), now)
        finally:
            if own is not None:
                own.close()

    def _events(self, src: SourceFile, snapshot: Snapshot, ledger: Ledger, now: datetime) -> Iterator[UsageEvent]:
        db_path = str(src.path)
        directory_profile = src.session_hint or profile_of(src.path)
        contexts: dict[str, Context] = {}

        def context(session: str) -> Context:
            if session not in contexts:
                contexts[session] = context_of(session, snapshot.sessions, directory_profile)
            return contexts[session]

        resets = 0
        for row in snapshot.usage:
            src.stats.lines += 1
            resets += ledger.observe(db_path, row, context(row.session_id), now)
        if resets:
            src.stats.notes.append(
                f"{resets} Hermes usage counters went down (a repair or a reset); their current values were"
                " counted as new usage"
            )
        for stored in ledger.usage(db_path):
            event = self._event(src, stored, snapshot.sessions, context)
            if event is not None:
                src.stats.events += 1
                yield event
        for session, when, issue in snapshot.marks:
            ctx = context(session)
            yield UsageEvent(
                tool=self.name,
                session_id=ctx.root,
                sub_session_id=session if session != ctx.root else None,
                model="",
                timestamp=when,
                tokens=TokenCounts(),
                requests=0,
                cwd=ctx.cwd,
                issue_mark=issue,
            )

    def _event(
        self,
        src: SourceFile,
        stored: sqlite3.Row,
        sessions: Mapping[str, SessionRow],
        context: Callable[[str], Context],
    ) -> UsageEvent | None:
        session = str(stored["session_id"])
        if session in sessions:
            ctx = context(session)
        else:  # Hermes pruned the session: what was known when its usage was seen
            ctx = Context(
                root=str(stored["root_session"]),
                profile=stored["profile"],
                unattended=bool(stored["unattended"]),
                cwd=stored["cwd"],
                git_branch=stored["git_branch"],
            )
        try:
            ended = datetime.fromisoformat(stored["ended_at"])
            started = datetime.fromisoformat(stored["started_at"])
        except (TypeError, ValueError):
            src.stats.skipped_events += 1
            return None
        reasoning = int(stored["reasoning"])
        tokens = TokenCounts(
            input=int(stored["input"]),
            output=max(int(stored["output"]) - reasoning, 0),
            cache_write_5m=int(stored["cache_write"]),
            cache_read=int(stored["cache_read"]),
            reasoning=reasoning,
        )
        requests = int(stored["api_calls"])
        if tokens.is_zero() and not requests:
            return None
        model = str(stored["model"])
        route = _text(stored["billing_provider"])
        provider, cost_type = resolve_provider(route, stored["billing_base_url"], model)
        if str(stored["billing_mode"]) in SUBSCRIPTION_MODES:
            cost_type = "subscription"
        cost = str(_money(stored["cost"])) if stored["cost_complete"] else None
        parts = (session, model, route or "", str(stored["billing_base_url"]), str(stored["billing_mode"]))
        return UsageEvent(
            tool=self.name,
            session_id=ctx.root,
            sub_session_id=session if session != ctx.root else None,
            model=model,
            timestamp=ended,
            span_start=started,
            tokens=tokens,
            provider=provider,
            route=route,
            cost_type=cost_type,
            requests=requests,
            message_id="|".join((*parts, str(stored["task"]), str(stored["usage_date"]))),
            git_branch=ctx.git_branch,
            cwd=ctx.cwd,
            source_path=str(src.path),
            cost=cost,
            cost_currency="USD" if cost is not None else None,
            profile=ctx.profile,
            unattended=ctx.unattended,
        )

    def dedup_key(self, event: UsageEvent) -> str | None:
        if event.message_id is None:
            return None
        # The same growth read from two copies of a database (a backup among the roots, without a
        # state database) counts once; different growth of one row in two databases does not merge.
        return f"{event.message_id}|{event.tokens.total}|{event.requests}"
