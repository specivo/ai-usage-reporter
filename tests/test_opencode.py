"""The OpenCode adapter beyond the conformance kit: layouts, locations, locking and child sessions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters.opencode import OpenCodeAdapter
from ai_usage_reporter.config import Config, SourceSettings
from ai_usage_reporter.model import SourceBusyError, UsageEvent
from ai_usage_reporter.pipeline import Collected, Selection, collect
from ai_usage_reporter.state import State
from tests.fixtures.opencode_home import CHILD, ROOT, assistant, build, ms


def _events(home: Path, env: dict[str, str] | None = None, roots: tuple[Path, ...] = ()) -> list[UsageEvent]:
    adapter = OpenCodeAdapter(sleep=lambda s: None)
    ctx = DiscoveryContext(home=home, env=env or {}, roots=roots)
    return [e for src in adapter.discover(ctx) for e in adapter.parse(src)]


def _collect(home: Path, state: State | None = None, selection: Selection | None = None) -> Collected:
    registry = Registry(adapters={"opencode": OpenCodeAdapter(sleep=lambda s: None)})
    return collect(
        Config(),
        registry,
        state=state,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        selection=selection,
    )


def test_tokens_are_taken_as_disjoint_classes(tmp_path: Path) -> None:
    build(tmp_path)
    by_id = {e.message_id: e for e in _events(tmp_path)}
    first = by_id["msg_0002"]
    assert first.tokens.as_dict() == {
        "input": 120,
        "output": 300,
        "cache_write_5m": 2000,
        "cache_write_1h": 0,
        "cache_read": 9000,
        "reasoning": 50,
    }
    assert (first.model, first.provider, first.tool_version) == ("claude-sonnet-4-5", "anthropic", "2.2.5")
    assert (first.cost, first.cost_currency) == ("0.0123", "USD")
    assert first.cwd and first.cwd.endswith("/app")


def test_child_sessions_fold_into_the_root(tmp_path: Path) -> None:
    build(tmp_path)
    child = next(e for e in _events(tmp_path) if e.message_id == "msg_0004")
    assert (child.session_id, child.sub_session_id) == (ROOT, CHILD)


def test_push_session_of_a_child_selects_the_whole_root_session(tmp_path: Path) -> None:
    build(tmp_path)
    collected = _collect(tmp_path, selection=Selection(session=CHILD))
    records = [r.to_wire() for r in collected.records]
    assert {r["session_id"] for r in records} == {ROOT}
    assert sum(r["request_count"] for r in records) == 3


def test_data_directory_follows_xdg_and_roots(tmp_path: Path) -> None:
    build(tmp_path / "elsewhere")
    data = tmp_path / "elsewhere" / ".local" / "share"
    assert _events(tmp_path) == []
    assert len(_events(tmp_path, {"XDG_DATA_HOME": str(data)})) == 6
    assert len(_events(tmp_path, roots=(data / "opencode",))) == 6
    assert len(_events(tmp_path, roots=(data / "opencode" / "opencode.db",))) == 5


def test_opencode_db_variable_and_channel_databases(tmp_path: Path) -> None:
    data = build(tmp_path)
    (data / "opencode.db").rename(data / "custom.db")
    assert len(_events(tmp_path, {"OPENCODE_DB": "custom.db"})) == 6
    (data / "custom.db").rename(data / "opencode-beta.db")
    assert len(_events(tmp_path)) == 6


def _v1_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE session (id text PRIMARY KEY, project_id text, parent_id text, slug text, directory text,"
        " title text, version text, time_created integer, time_updated integer);"
        "CREATE TABLE message (id text PRIMARY KEY, session_id text, time_created integer, time_updated integer,"
        " data text);"
    )
    db.execute("INSERT INTO session VALUES ('ses_v1root', 'p', NULL, 's', '/w', 't', '1.0.0', 0, 0)")
    db.execute("INSERT INTO session VALUES ('ses_v1kid', 'p', 'ses_v1root', 's', '/w', 't', '1.0.0', 0, 0)")
    data = assistant("2026-10-02T08:00:00", "unused", "unused", (10, 5, 0, 100, 0), 0.001)
    data.pop("model")
    data.update({"role": "assistant", "modelID": "claude-haiku-4-5", "providerID": "anthropic"})
    db.execute("INSERT INTO message VALUES ('msg_v1', 'ses_v1kid', 0, 0, ?)", (json.dumps(data),))
    user = {"role": "user", "time": {"created": ms("2026-10-02T07:59:00")}}
    db.execute("INSERT INTO message VALUES ('msg_v1u', 'ses_v1kid', 0, 0, ?)", (json.dumps(user),))
    db.commit()
    db.close()


def test_opencode_1_tables_are_read(tmp_path: Path) -> None:
    data = tmp_path / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    _v1_database(data / "opencode.db")
    [event] = _events(tmp_path)
    assert (event.session_id, event.sub_session_id, event.model, event.tool_version) == (
        "ses_v1root",
        "ses_v1kid",
        "claude-haiku-4-5",
        "1.0.0",
    )


def test_a_message_in_both_layouts_counts_once(tmp_path: Path) -> None:
    data = build(tmp_path)
    db = sqlite3.connect(data / "opencode.db")
    row = db.execute("SELECT data FROM session_message WHERE id = 'msg_0002'").fetchone()
    db.executescript("CREATE TABLE message (id text, session_id text, time_created integer, data text);")
    copy = {**json.loads(row[0]), "role": "assistant"}
    db.execute("INSERT INTO message VALUES ('msg_0002', ?, 0, ?)", (ROOT, json.dumps(copy)))
    db.commit()
    db.close()
    collected = _collect(tmp_path)
    root = [r for r in collected.records if r.session_id == ROOT]
    assert sum(r.request_count for r in root) == 3


def test_the_database_is_never_written(tmp_path: Path) -> None:
    data = build(tmp_path)
    db_file = data / "opencode.db"
    before = (db_file.read_bytes(), db_file.stat().st_mtime_ns)
    _events(tmp_path)
    assert (db_file.read_bytes(), db_file.stat().st_mtime_ns) == before
    assert not (data / "opencode.db-journal").exists()


def test_a_locked_database_is_skipped_and_read_next_time(tmp_path: Path) -> None:
    data = build(tmp_path)
    writer = sqlite3.connect(data / "opencode.db", isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    adapter = OpenCodeAdapter(sleep=lambda s: None)
    src = next(iter(adapter.discover(DiscoveryContext(home=tmp_path, env={}))))
    with pytest.raises(SourceBusyError):
        list(adapter.parse(src))
    with State.in_memory() as state:
        busy = _collect(tmp_path, state)
        assert busy.reports["opencode"].busy_files == 1
        writer.execute("ROLLBACK")
        writer.close()
        again = _collect(tmp_path, state)
        assert again.reports["opencode"].parsed >= 1
        assert again.records


def test_changes_in_the_write_ahead_log_are_seen(tmp_path: Path) -> None:
    data = build(tmp_path)
    writer = sqlite3.connect(data / "opencode.db", isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    with State.in_memory() as state:
        first = _collect(tmp_path, state)
        more = assistant("2026-10-05T10:05:00", "claude-sonnet-4-5", "anthropic", (1, 1, 0, 0, 0), 0.0001)
        writer.execute(
            "INSERT INTO session_message VALUES ('msg_0007', ?, 'assistant', 50, 0, 0, ?)", (ROOT, json.dumps(more))
        )
        assert (data / "opencode.db-wal").exists()
        second = _collect(tmp_path, state)
        writer.close()
    assert sum(r.request_count for r in second.records) == sum(r.request_count for r in first.records) + 1


def test_disabled_or_imported_tool_is_not_read(tmp_path: Path) -> None:
    build(tmp_path)
    registry = Registry(adapters={"opencode": OpenCodeAdapter()})
    for settings in (SourceSettings(enabled=False), SourceSettings(via="ccusage")):
        config = Config(sources={"opencode": settings})
        collected = collect(
            config, registry, context_for=lambda a, roots: DiscoveryContext(home=tmp_path, env={}, roots=roots)
        )
        assert collected.records == []
