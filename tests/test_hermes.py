"""The Hermes adapter beyond the conformance kit: lineage, profiles, routes, schema checks and snapshot deltas."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters._routes import resolve_provider
from ai_usage_reporter.adapters.hermes import TESTED_SCHEMA_VERSION, HermesAdapter
from ai_usage_reporter.adapters.opencode import OpenCodeAdapter
from ai_usage_reporter.config import Config, CostRule
from ai_usage_reporter.model import SourceBusyError, UsageEvent
from ai_usage_reporter.pipeline import Collected, Selection, collect, deliver
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.state import State
from tests.conftest import FakeServer
from tests.fixtures import hermes_home
from tests.fixtures.hermes_home import CHILD, CRON, DEFAULT_USAGE, LOCAL, RESEARCH, ROOT, build, update_usage

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
SONNET = DEFAULT_USAGE[0]
CHILD_SONNET = DEFAULT_USAGE[3]


def _collect(
    home: Path,
    state: State | None = None,
    *,
    now: datetime = NOW,
    env: dict[str, str] | None = None,
    roots: tuple[Path, ...] = (),
    selection: Selection | None = None,
    config: Config | None = None,
) -> Collected:
    registry = Registry(adapters={"hermes": HermesAdapter(sleep=lambda s: None)})
    return collect(
        config or Config(),
        registry,
        state=state,
        selection=selection,
        context_for=lambda a, r: DiscoveryContext(home=home, env=env or {}, roots=roots or r),
        git=None,
        now=now,
    )


def _wires(collected: Collected) -> list[dict[str, Any]]:
    return [r.to_wire() for r in collected.records]


def _record(wires: list[dict[str, Any]], model: str, day: str | None = None) -> dict[str, Any]:
    found = [w for w in wires if w["model"] == model and (day is None or w["usage_date"] == day)]
    assert len(found) == 1, found
    return found[0]


def _events(home: Path) -> list[UsageEvent]:
    adapter = HermesAdapter(sleep=lambda s: None)
    ctx = DiscoveryContext(home=home, env={})
    return [e for src in adapter.discover(ctx) for e in adapter.parse(src)]


def _sink(server: FakeServer) -> SpecivoSink:
    return SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)


# -- token mapping, lineage, profiles, unattended ------------------------------------------------


def test_tokens_take_reasoning_out_of_output(tmp_path: Path) -> None:
    build(tmp_path)
    event = next(e for e in _events(tmp_path) if e.model == "claude-sonnet-4-5" and e.sub_session_id is None)
    assert event.tokens.as_dict() == {
        "input": 3000,
        "output": 1200,  # 1500 including 300 of reasoning
        "cache_write_5m": 4000,
        "cache_write_1h": 0,
        "cache_read": 20000,
        "reasoning": 300,
    }
    assert event.requests == 6
    assert event.span_start == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert event.timestamp == datetime(2026, 11, 5, 9, 20, tzinfo=UTC)


def test_child_sessions_roll_up_into_the_root(tmp_path: Path) -> None:
    build(tmp_path)
    child = next(e for e in _events(tmp_path) if e.sub_session_id == CHILD)
    assert child.session_id == ROOT
    wires = _wires(_collect(tmp_path))
    assert CHILD not in {w["session_id"] for w in wires}
    sonnet = _record(wires, "claude-sonnet-4-5")
    assert sonnet["session_id"] == ROOT
    assert sonnet["request_count"] == SONNET.calls + CHILD_SONNET.calls
    assert sonnet["tokens"]["input"] == 3000 + 800
    assert sonnet["started_at"] == "2026-11-05T09:00:00Z" and sonnet["ended_at"] == "2026-11-05T09:20:00Z"
    # The roll-up is the lineage: no record says which session it came from.
    assert all("parent_session_id" not in w for w in wires)


def test_push_session_of_a_child_selects_the_root(tmp_path: Path) -> None:
    build(tmp_path)
    wires = _wires(_collect(tmp_path, selection=Selection(session=CHILD)))
    assert {w["session_id"] for w in wires} == {ROOT}


def test_lineage_is_followed_to_the_top_and_survives_a_cycle(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    conn = sqlite3.connect(hermes / "state.db")
    with conn:
        hermes_home.session(conn, "grandchild-1", "subagent", "2026-11-05T09:12:00", parent=CHILD)
        hermes_home.put_usage(conn, replace(CHILD_SONNET, session="grandchild-1", model="claude-opus-4-1"))
        hermes_home.session(conn, "loop-a", "cli", "2026-11-05T11:00:00", parent="loop-b")
        hermes_home.session(conn, "loop-b", "cli", "2026-11-05T11:00:00", parent="loop-a")
        hermes_home.put_usage(conn, replace(CHILD_SONNET, session="loop-a", model="gpt-5"))
    conn.close()
    wires = _wires(_collect(tmp_path))
    assert _record(wires, "claude-opus-4-1")["session_id"] == ROOT
    assert _record(wires, "gpt-5")["session_id"] in {"loop-a", "loop-b"}


def test_profiles_from_the_directory_and_the_session(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    wires = _wires(_collect(tmp_path))
    assert _record(wires, "claude-sonnet-4-5")["profile"] == "default"
    assert _record(wires, "Hermes-4-405B")["profile"] == "research"
    # The root session's profile_name wins over the directory.
    conn = sqlite3.connect(hermes / "state.db")
    with conn:
        conn.execute("UPDATE sessions SET profile_name = 'ops' WHERE id = ?", (ROOT,))
    conn.close()
    wires = _wires(_collect(tmp_path))
    assert _record(wires, "claude-sonnet-4-5")["profile"] == "ops"
    assert _record(wires, "gpt-5.1")["profile"] == "default"


def test_hermes_home_variable_and_profile_homes(tmp_path: Path) -> None:
    hermes = build(tmp_path / "elsewhere")
    assert _events(tmp_path) == []
    every = {"claude-sonnet-4-5", "Hermes-4-405B"}
    by_home = _wires(_collect(tmp_path, env={"HERMES_HOME": str(hermes)}))
    assert every <= {w["model"] for w in by_home}
    # HERMES_HOME naming a profile reads the whole tree, the default profile included.
    by_profile = _wires(_collect(tmp_path, env={"HERMES_HOME": str(hermes / "profiles" / "research")}))
    assert {w["model"] for w in by_profile} == {w["model"] for w in by_home}
    # roots replace the defaults and may name a database file.
    only_research = _wires(_collect(tmp_path, roots=(hermes / "profiles" / "research" / "state.db",)))
    assert [w["model"] for w in only_research] == ["Hermes-4-405B"]
    assert only_research[0]["profile"] == "research"


def test_cron_sessions_are_unattended_with_their_children(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    conn = sqlite3.connect(hermes / "state.db")
    with conn:
        hermes_home.session(conn, "cron-child", "subagent", "2026-11-05T02:01:00", parent=CRON)
        hermes_home.put_usage(conn, replace(CHILD_SONNET, session="cron-child", model="claude-haiku-4-5"))
    conn.close()
    wires = _wires(_collect(tmp_path))
    unattended = {(w["session_id"], w["model"]) for w in wires if w.get("unattended")}
    assert unattended == {(CRON, "gpt-5.1"), (CRON, "claude-haiku-4-5")}
    assert "unattended" not in _record([w for w in wires if w["session_id"] == ROOT], "claude-haiku-4-5")


# -- provider, route and cost --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("billing_provider", "base_url", "model", "expected"),
    [
        ("openrouter", "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.6", ("anthropic", None)),
        ("openrouter", "https://openrouter.ai/api/v1", "google/gemini-2.5-pro", ("gemini", None)),
        ("litellm", "https://llm.example.com", "openai/gpt-5", ("openai", None)),
        ("openai-codex", "https://chatgpt.com/backend-api/codex", "gpt-5.1", ("openai", None)),
        ("nous", "https://inference-api.nousresearch.com/v1", "Hermes-4-405B", ("nous", None)),
        ("anthropic", "https://api.anthropic.com", "claude-sonnet-4-5", ("anthropic", None)),
        ("Anthropic", "", "claude-sonnet-4-5", ("anthropic", None)),
        ("custom", "http://localhost:11434/v1", "qwen3-coder:30b", ("local", "local")),
        ("custom", "http://127.0.0.1:8080/v1", "any-model", ("local", "local")),
        ("custom", "http://10.0.0.7:8000/v1", "any-model", ("local", "local")),
        ("ollama", "", "llama3", ("local", "local")),
        ("custom", "https://llm.example.com/v1", "in-house-model", (None, None)),
        ("", "", "deepseek/deepseek-r1", ("deepseek", None)),
    ],
)
def test_provider_and_cost_type_from_the_route(
    billing_provider: str, base_url: str, model: str, expected: tuple[str | None, str | None]
) -> None:
    assert resolve_provider(billing_provider, base_url, model) == expected


def test_route_split_on_the_records(tmp_path: Path) -> None:
    build(tmp_path)
    wires = _wires(_collect(tmp_path))
    gateway = _record(wires, "anthropic/claude-sonnet-4.6")
    assert (gateway["provider"], gateway["route"]) == ("anthropic", "openrouter")
    assert gateway["reported_cost"] == {"amount": "0.0421", "currency": "USD"}
    assert "cost_type" not in gateway
    codex = _record(wires, "gpt-5.1")
    assert (codex["provider"], codex["route"], codex["cost_type"]) == ("openai", "openai-codex", "subscription")
    local = _record(wires, "qwen3-coder:30b")
    assert (local["provider"], local["route"], local["cost_type"]) == ("local", "custom", "local")
    nous = _record(wires, "Hermes-4-405B")
    assert (nous["provider"], nous["route"]) == ("nous", "nous")
    # A Hermes estimate is never sent as a reported cost.
    assert "reported_cost" not in _record(wires, "claude-sonnet-4-5")


def test_a_cost_rule_wins_over_the_route(tmp_path: Path) -> None:
    build(tmp_path)
    config = Config(cost_rules=(CostRule(cost_type="api", tool="hermes", model="gpt-5*"),))
    wires = _wires(_collect(tmp_path, config=config))
    assert _record(wires, "gpt-5.1")["cost_type"] == "api"
    assert _record(wires, "qwen3-coder:30b")["cost_type"] == "local"


# -- issue marks ---------------------------------------------------------------------------------


def test_specivo_tool_calls_mark_issues(tmp_path: Path) -> None:
    build(tmp_path)
    marks = sorted((e.timestamp.isoformat(), e.issue_mark) for e in _events(tmp_path) if e.is_marker)
    assert marks == [("2026-11-05T09:05:00+00:00", "ACME-12"), ("2026-11-05T09:25:00+00:00", "ACME-13")]
    wires = _wires(_collect(tmp_path))
    assert _record(wires, "claude-sonnet-4-5")["attribution"]["issue_ref"] == "ACME-12"
    assert _record(wires, "anthropic/claude-sonnet-4.6")["attribution"]["issue_ref"] == "ACME-13"


# -- schema versions and missing data -------------------------------------------------------------


def _replace_default(home: Path, **kwargs: Any) -> Path:
    db = home / ".hermes" / "state.db"
    db.unlink()
    return hermes_home.build_default(db, **kwargs)


def test_a_newer_schema_is_read_by_its_columns_with_a_note(tmp_path: Path) -> None:
    build(tmp_path)
    _replace_default(tmp_path, version=TESTED_SCHEMA_VERSION + 1)
    collected = _collect(tmp_path)
    report = collected.reports["hermes"]
    assert report.schema_errors == []
    assert any(f"v{TESTED_SCHEMA_VERSION + 1} is newer" in note for note in report.notes)
    assert _record(_wires(collected), "claude-sonnet-4-5")["request_count"] == 9


def test_a_missing_required_column_skips_the_database_with_a_message(tmp_path: Path) -> None:
    build(tmp_path)
    schema = hermes_home.SCHEMA_V33.replace(
        "    reasoning_tokens INTEGER NOT NULL DEFAULT 0,\n    estimated", "    estimated"
    )
    _replace_default(tmp_path, schema=schema, data=False)
    collected = _collect(tmp_path)
    report = collected.reports["hermes"]
    assert len(report.schema_errors) == 1
    assert "reasoning_tokens" in report.schema_errors[0]
    assert "update the reporter" in report.schema_errors[0]
    assert report.failed_files == 1
    # The other profile is read all the same.
    assert [w["model"] for w in _wires(collected)] == ["Hermes-4-405B"]


def test_an_older_schema_or_no_usage_table_is_refused(tmp_path: Path) -> None:
    build(tmp_path)
    _replace_default(tmp_path, version=19)
    report = _collect(tmp_path).reports["hermes"]
    assert report.schema_errors and "older than the reporter reads" in report.schema_errors[0]
    _replace_default(tmp_path, schema=hermes_home.SCHEMA_V33.split("CREATE TABLE session_model_usage")[0], data=False)
    report = _collect(tmp_path).reports["hermes"]
    assert report.schema_errors and "no session_model_usage table" in report.schema_errors[0]


def test_optional_columns_degrade(tmp_path: Path) -> None:
    build(tmp_path)
    schema = (
        hermes_home.SCHEMA_V33.replace("    profile_name TEXT,\n", "")
        .replace("    tool_calls TEXT,\n", "")
        .replace("    task TEXT NOT NULL DEFAULT '',\n", "")
        .replace("billing_mode, task)", "billing_mode)")
    )
    db = tmp_path / "plain" / "state.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version VALUES (25)")
        conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s-1', 'cli', 0)")
        conn.execute(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens, last_seen)"
            " VALUES ('s-1', 'claude-sonnet-4-5', 10, 5, ?)",
            (hermes_home.ts("2026-11-05T10:00:00"),),
        )
    conn.close()
    collected = _collect(tmp_path, roots=(tmp_path / "plain",))
    assert collected.reports["hermes"].schema_errors == []
    assert any("no issue marks" in note for note in collected.reports["hermes"].notes)
    (wire,) = _wires(collected)
    assert (wire["profile"], wire["tokens"]) == ("default", {"input": 10, "output": 5})


def test_no_hermes_home_or_database_reads_nothing(tmp_path: Path) -> None:
    collected = _collect(tmp_path)
    assert collected.reports["hermes"].files == 0 and collected.records == []
    assert _collect(tmp_path, env={"HERMES_HOME": str(tmp_path / "missing")}).records == []
    (tmp_path / ".hermes" / "profiles" / "empty").mkdir(parents=True)
    collected = _collect(tmp_path)
    assert collected.reports["hermes"].files == 0 and collected.records == []


def test_a_locked_database_is_left_for_the_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build(tmp_path)

    def locked(path: Path, sleep: Any) -> sqlite3.Connection:
        raise SourceBusyError(path.name)

    monkeypatch.setattr("ai_usage_reporter.adapters.hermes._open_read_only", locked)
    with State.in_memory() as state:
        collected = _collect(tmp_path, state)
        assert collected.reports["hermes"].busy_files == 2 and collected.records == []
        assert state.db.execute("SELECT COUNT(*) FROM hermes_counters").fetchone()[0] == 0


# -- snapshot deltas and idempotency -----------------------------------------------------------------


def test_reading_again_never_counts_twice(tmp_path: Path, server: FakeServer) -> None:
    hermes = build(tmp_path)
    with State.in_memory() as state:
        first = _wires(_collect(tmp_path, state))
        summary = deliver(_collect(tmp_path, state).records, _sink(server), state)
        assert summary.sent == len(first)
        # Unchanged files are not parsed again; the same records are built and nothing is sent.
        again = _collect(tmp_path, state)
        assert again.reports["hermes"].parsed == 0
        assert _wires(again) == first
        # A write that changes no totals (Hermes touched the file) parses again with the same result.
        update_usage(hermes / "state.db", SONNET)
        touched = _collect(tmp_path, state)
        assert touched.reports["hermes"].parsed >= 1
        assert _wires(touched) == first
        assert deliver(touched.records, _sink(server), state).sent == 0


def test_growth_of_the_totals_is_counted_once(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    with State.in_memory() as state:
        _collect(tmp_path, state)
        # Still before the agent created its next issue at 09:25, so in one segment.
        grown = replace(SONNET, calls=8, tokens=(3500, 1700, 26000, 4000, 300), last="2026-11-05T09:24:00")
        update_usage(hermes / "state.db", grown)
        sonnet = _record(_wires(_collect(tmp_path, state)), "claude-sonnet-4-5")
        # The record holds the session's complete totals: the root's new totals plus the child's.
        assert sonnet["request_count"] == 8 + CHILD_SONNET.calls
        assert sonnet["tokens"]["input"] == 3500 + 800
        assert sonnet["tokens"]["output"] == (1700 - 300) + (350 - 50)
        assert sonnet["ended_at"] == "2026-11-05T09:24:00Z"
        # A third read with nothing new changes nothing.
        update_usage(hermes / "state.db", grown)
        assert _record(_wires(_collect(tmp_path, state)), "claude-sonnet-4-5") == sonnet


def test_growth_on_a_later_day_lands_on_that_day(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    with State.in_memory() as state:
        before = _record(_wires(_collect(tmp_path, state)), "claude-sonnet-4-5")
        next_day = replace(SONNET, calls=10, tokens=(4000, 2000, 30000, 4000, 400), last="2026-11-06T08:15:00")
        update_usage(hermes / "state.db", next_day)
        wires = _wires(_collect(tmp_path, state))
        assert _record(wires, "claude-sonnet-4-5", "2026-11-05") == before
        later = _record(wires, "claude-sonnet-4-5", "2026-11-06")
        assert later["request_count"] == 4
        assert later["tokens"] == {"input": 1000, "output": 400, "cache_read": 10000, "reasoning": 100}
        # The growth began after the last reading, which was on the day before.
        assert later["started_at"] == "2026-11-06T00:00:00Z" and later["ended_at"] == "2026-11-06T08:15:00Z"


def test_a_counter_reset_counts_the_current_values(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    with State.in_memory() as state:
        _collect(tmp_path, state)
        reset = replace(SONNET, calls=2, tokens=(400, 100, 0, 0, 0), last="2026-11-06T09:00:00")
        update_usage(hermes / "state.db", reset)
        collected = _collect(tmp_path, state)
        assert any("went down" in note for note in collected.reports["hermes"].notes)
        later = _record(_wires(collected), "claude-sonnet-4-5", "2026-11-06")
        assert later["request_count"] == 2 and later["tokens"] == {"input": 400, "output": 100}


def test_usage_seen_after_the_run_is_clamped_to_the_run(tmp_path: Path) -> None:
    build(tmp_path)
    early = datetime(2026, 11, 5, 9, 10, tzinfo=UTC)
    wires = _wires(_collect(tmp_path, now=early))
    assert _record(wires, "claude-sonnet-4-5")["ended_at"] == "2026-11-05T09:10:00Z"


def test_a_copied_or_moved_database_adds_nothing(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    expected = _wires(_collect(tmp_path))
    copy = hermes / "profiles" / "backup" / "state.db"
    copy.parent.mkdir(parents=True)
    shutil.copy2(hermes / "state.db", copy)
    assert _wires(_collect(tmp_path)) == expected
    with State.in_memory() as state:
        assert _wires(_collect(tmp_path, state)) == expected


def test_usage_survives_hermes_pruning_the_session(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    with State.in_memory() as state:
        before = _record(_wires(_collect(tmp_path, state)), "claude-sonnet-4-5")
        conn = sqlite3.connect(hermes / "state.db")
        with conn:
            conn.execute("DELETE FROM session_model_usage WHERE session_id = ?", (CHILD,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (CHILD,))
        conn.close()
        after = _record(_wires(_collect(tmp_path, state)), "claude-sonnet-4-5")
        assert after == before


def test_a_dry_run_on_a_scratch_copy_matches_the_push_and_writes_nothing(tmp_path: Path) -> None:
    hermes = build(tmp_path)
    path = tmp_path / "reporter" / "state.db"
    with State(path) as state:
        _collect(tmp_path, state)
    update_usage(hermes / "state.db", replace(SONNET, calls=10, last="2026-11-06T08:00:00"))
    stamp = path.stat().st_mtime_ns, path.stat().st_size
    with State.scratch_copy(path) as scratch:
        preview = _wires(_collect(tmp_path, scratch))
    assert (path.stat().st_mtime_ns, path.stat().st_size) == stamp
    with State(path) as state:
        assert _wires(_collect(tmp_path, state)) == preview
    assert {w["usage_date"] for w in preview if w["model"] == "claude-sonnet-4-5"} == {"2026-11-05", "2026-11-06"}


def test_other_sources_are_unaffected(tmp_path: Path) -> None:
    build(tmp_path)
    registry = Registry(adapters={"opencode": OpenCodeAdapter(), "hermes": HermesAdapter()})
    collected = collect(
        Config(), registry, context_for=lambda a, r: DiscoveryContext(home=tmp_path, env={}, roots=r), now=NOW
    )
    assert collected.reports["opencode"].files == 0
    assert {r.tool for r in collected.records} == {"hermes"}
    assert {r.session_id for r in collected.records} == {ROOT, CRON, LOCAL, RESEARCH}


# -- the schema check against a Hermes checkout ------------------------------------------------------


def _schema_check() -> Any:
    import importlib.util

    path = Path(__file__).parent.parent / "scripts" / "hermes_schema_check.py"
    spec = importlib.util.spec_from_file_location("hermes_schema_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ddl(schema: str, version: int = TESTED_SCHEMA_VERSION) -> str:
    """The fixture DDL in the shape of Hermes's _schema_ddl.py."""
    body = schema.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ").replace("\n);", "\n);")
    return f'SCHEMA_VERSION = {version}\n\nSCHEMA_SQL = """\n{body}\n"""\n'


def test_schema_check_accepts_the_fixture_schema() -> None:
    check = _schema_check()
    assert check.check(_ddl(hermes_home.SCHEMA_V33)) == []


def test_schema_check_names_missing_columns_and_newer_versions() -> None:
    check = _schema_check()
    schema = hermes_home.SCHEMA_V33.replace("    last_seen REAL,\n", "").replace("    profile_name TEXT,\n", "")
    problems = check.check(_ddl(schema, TESTED_SCHEMA_VERSION + 1))
    assert any("newer than the fixtures" in p for p in problems)
    assert any(p.startswith("session_model_usage.last_seen is gone (required") for p in problems)
    assert any(p.startswith("sessions.profile_name is gone (optional") for p in problems)
