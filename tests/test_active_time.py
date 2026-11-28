"""Active time end to end: Claude Code thread events, the owner rule in buckets, records, gating, the CLI.

The fixtures are synthetic (tests/fixtures/active_time_home.py); expected seconds are worked out by hand
in the comments.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter import cli
from ai_usage_reporter.adapters import DiscoveryContext, Registry, parse_all
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.config import Config, parse_config
from ai_usage_reporter.model import MODEL, PROMPT, TOOL_END, TOOL_START, TURN_END, ThreadEvent, UsageEvent
from ai_usage_reporter.pipeline import Collected, collect, deliver, superseded_records
from ai_usage_reporter.record import ACTIVE_TIME_FIELDS, OPTIONAL_FIELDS, gate_optional_fields
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.state import FileIdentity, State
from ai_usage_reporter.testing import validate_records
from tests.conftest import FakeServer, make_record, usage_batch
from tests.fixtures import active_time_home, agent_log_home
from tests.fixtures.active_time_home import HAIKU, SESSION, SONNET, Transcript

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def run(home: Path, config: Config | None = None, state: State | None = None) -> Collected:
    adapter = ClaudeCodeAdapter()
    return collect(
        config or Config(),
        Registry(adapters={adapter.name: adapter}),
        state=state,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
        now=NOW,
    )


def wires(home: Path, config: Config | None = None) -> list[dict[str, Any]]:
    return [r.to_wire() for r in run(home, config).records]


def by_model(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["model"]: r for r in records}


def thread_events(home: Path) -> list[ThreadEvent]:
    adapter = ClaudeCodeAdapter()
    out: list[ThreadEvent] = []
    for src in adapter.discover(DiscoveryContext(home=home, env={})):
        out.extend(e for e in parse_all(adapter, src) if isinstance(e, ThreadEvent))
    return out


# -- the adapter's events ----------------------------------------------------------------------------


def test_claude_code_yields_thread_events_as_the_definition_lists_them(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path)
    events = thread_events(home)
    main = [e for e in events if e.thread_id is None]
    assert [e.kind for e in main] == [
        PROMPT,
        MODEL,
        TOOL_START,
        TOOL_END,
        MODEL,
        TOOL_START,
        TOOL_END,
        MODEL,
        TURN_END,
        PROMPT,  # the peer hand-back: an isMeta line with an origin
        MODEL,
        TOOL_START,
        TOOL_END,
        MODEL,
        TOOL_START,
        TOOL_END,
        MODEL,
        TURN_END,
    ]
    calls = {(e.call_id, e.kind): e for e in main if e.call_id}
    assert calls[("toolu_ask", TOOL_START)].human_wait and calls[("toolu_ask", TOOL_END)].human_wait
    assert calls[("toolu_ask", TOOL_END)].user_decided
    assert calls[("toolu_approved", TOOL_END)].user_decided and not calls[("toolu_approved", TOOL_END)].human_wait
    assert not calls[("toolu_test", TOOL_END)].user_decided  # decided by the config
    # An async subagent launch is a plain (instant) tool run, not a subagent wait.
    assert not calls[("toolu_agent1", TOOL_END)].subagent_wait
    threads = {e.thread_id for e in events}
    assert threads == {None, "agent-a1b2c3d4", "agent-e5f6a7b8"}
    # The isMeta line without an origin in the second subagent is no event; the coordinator line is.
    second = [e.kind for e in events if e.thread_id == "agent-e5f6a7b8"]
    assert second == [PROMPT, MODEL, PROMPT, MODEL]
    assert all(e.session_id == SESSION and e.key for e in events)
    assert len({e.key for e in events}) == len(events)


@pytest.mark.parametrize("origin", ["human", "task-notification", "peer", "coordinator", "auto-continuation", None])
def test_a_prompt_of_every_origin_kind(tmp_path: Path, origin: str | None) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z", origin=origin, meta=origin not in (None, "human"))
    t.answer("2026-11-05T08:00:30.000Z")
    t.write(tmp_path)
    assert [e.kind for e in thread_events(tmp_path)] == [PROMPT, MODEL]


def test_meta_lines_without_an_origin_are_no_events(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z")
    t.prompt("2026-11-05T08:00:01.000Z", meta=True)
    t.write(tmp_path)
    assert [e.kind for e in thread_events(tmp_path)] == [PROMPT]


def test_foreground_subagent_and_user_rejections_are_classified(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z")
    t.answer("2026-11-05T08:00:05.000Z", tool="Task", call="toolu_fg")
    t.result("2026-11-05T08:30:05.000Z", "toolu_fg", launched={"status": "completed"})
    t.answer("2026-11-05T08:30:10.000Z", tool="Bash", call="toolu_no")
    t.result("2026-11-05T08:30:20.000Z", "toolu_no", source=None, denial="user-rejected")
    t.answer("2026-11-05T08:30:30.000Z", tool="Bash", call="toolu_rule")
    t.result("2026-11-05T08:30:31.000Z", "toolu_rule", decision="reject", denial="permission-rule")
    t.write(tmp_path)
    ends = {e.call_id: e for e in thread_events(tmp_path) if e.kind == TOOL_END}
    assert ends["toolu_fg"].subagent_wait and not ends["toolu_fg"].user_decided
    assert ends["toolu_no"].user_decided
    assert not ends["toolu_rule"].user_decided  # refused by a rule in the config, not by a person
    # prompt..08:00:05 (5); the foreground subagent's 30 minutes: nothing; 08:30:05 -> 08:30:10 (5);
    # the rejected call 10 s by the gap rule; 08:30:20 -> 08:30:30 (10); the refused call 1 s; total 31.
    assert by_model(wires(tmp_path))[SONNET]["active_seconds"] == 31


def test_lines_without_timestamp_or_uuid(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z")
    t.answer("2026-11-05T08:00:20.000Z")
    for line in t.lines:
        del line["uuid"]
    t.raw({"type": "user", "sessionId": SESSION, "message": {"role": "user", "content": "no time"}})
    t.write(tmp_path)
    events = thread_events(tmp_path)
    assert [e.kind for e in events] == [PROMPT, MODEL]
    assert all(e.key for e in events)  # named by session, thread and time instead
    assert by_model(wires(tmp_path))[SONNET]["active_seconds"] == 20


def test_parse_keeps_yielding_usage_only() -> None:
    # Third-party callers of parse() see what they always saw.
    home = Path(__file__).parent / "fixtures" / "claude_code" / "home"
    adapter = ClaudeCodeAdapter()
    for src in adapter.discover(DiscoveryContext(home=home, env={})):
        assert all(isinstance(e, UsageEvent) for e in adapter.parse(src))


# -- records -----------------------------------------------------------------------------------------


def test_a_session_with_two_subagents_sums_threads_and_joins_spans(tmp_path: Path) -> None:
    records = wires(active_time_home.build(tmp_path))
    validate_records(records)
    sonnet, haiku = by_model(records)[SONNET], by_model(records)[HAIKU]
    # Main thread 437 + second subagent 70 (both sonnet); the first subagent (haiku) 218.
    assert sonnet["active_seconds"] == 437 + 70 and haiku["active_seconds"] == 218
    assert sonnet["active_time_version"] == haiku["active_time_version"] == 1
    assert haiku["active_spans"] == [["2026-11-05T08:06:22Z", "2026-11-05T08:10:00Z"]]


def test_overlapping_threads_in_one_record_count_twice_in_seconds_once_in_spans(tmp_path: Path) -> None:
    main = Transcript(SESSION)
    main.prompt("2026-11-05T09:00:00.000Z").answer("2026-11-05T09:05:00.000Z")
    sub = Transcript(SESSION, agent="0f0f0f0f")
    sub.prompt("2026-11-05T09:01:00.000Z").answer("2026-11-05T09:06:00.000Z")
    main.write(tmp_path)
    sub.write(tmp_path)
    record = by_model(wires(tmp_path))[SONNET]
    assert record["active_seconds"] == 300 + 300
    assert record["active_spans"] == [["2026-11-05T09:00:00Z", "2026-11-05T09:06:00Z"]]
    validate_records([record])


def test_time_follows_its_request_across_midnight(tmp_path: Path) -> None:
    # prompt 23:58:00, request 23:58:30 (day 1) starts a tool that ends 00:03:00, request 00:03:10 (day 2).
    t = Transcript(SESSION)
    t.prompt("2026-11-05T23:58:00.000Z")
    t.answer("2026-11-05T23:58:30.000Z", tool="Bash", call="toolu_late")
    t.result("2026-11-06T00:03:00.000Z", "toolu_late")
    t.answer("2026-11-06T00:03:10.000Z")
    t.write(tmp_path)
    records = {r["usage_date"]: r for r in wires(tmp_path)}
    # Day 1 owns 23:58:00..23:58:30; the tool run and the last gap belong to the request after midnight.
    assert records["2026-11-05"]["active_seconds"] == 30
    assert records["2026-11-06"]["active_seconds"] == 270 + 10
    # Its span starts before its usage_date, which the contract allows.
    assert records["2026-11-06"]["active_spans"] == [["2026-11-05T23:58:30Z", "2026-11-06T00:03:10Z"]]
    validate_records(list(records.values()))


def test_two_models_in_one_session_split_the_time_by_request(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z")
    t.answer("2026-11-05T08:00:40.000Z", model=SONNET)
    t.answer("2026-11-05T08:01:00.000Z", model=HAIKU)
    t.answer("2026-11-05T08:01:30.000Z", model=SONNET)
    t.write(tmp_path)
    records = by_model(wires(tmp_path))
    # 0..40 -> sonnet; 40..60 -> haiku; 60..90 -> sonnet.
    assert records[SONNET]["active_seconds"] == 70 and records[HAIKU]["active_seconds"] == 20


def test_an_issue_mark_in_the_middle_moves_time_with_its_requests(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T09:00:00.000Z")
    t.answer(
        "2026-11-05T09:00:10.000Z",
        tool="mcp__specivo__specivo_update_issue",
        call="toolu_m1",
        arguments={"issue_ref": "ACME-1", "status_id": 2},
    )
    t.result("2026-11-05T09:00:11.000Z", "toolu_m1")
    t.answer("2026-11-05T09:01:00.000Z")
    t.answer(
        "2026-11-05T09:05:00.000Z",
        tool="mcp__specivo__specivo_update_issue",
        call="toolu_m2",
        arguments={"issue_ref": "ACME-2", "status_id": 2},
    )
    t.result("2026-11-05T09:05:01.000Z", "toolu_m2")
    t.answer("2026-11-05T09:06:00.000Z")
    t.write(tmp_path)
    records = {r["attribution"]["issue_ref"]: r for r in wires(tmp_path)}
    # ACME-1: 09:00:00..09:01:00 (60). ACME-2: the 4 minutes before the request at 09:05 that marked it
    # are that request's, then 09:05:00..09:06:00: 240 + 60.
    assert records["ACME-1"]["active_seconds"] == 60
    assert records["ACME-2"]["active_seconds"] == 300
    validate_records(list(records.values()))


def test_subagents_with_and_without_their_own_mark(tmp_path: Path) -> None:
    home = agent_log_home.build(tmp_path / "home")
    records = wires(home)
    issue = {r.get("attribution", {}).get("issue_ref"): r for r in records}
    # The worker marks ACME-20 itself: its prompt 10:00:00 .. its request 10:00:05 and the mark call at
    # 10:01:00 (55): 60 s. The helper has no mark and takes the main thread's ACME-12 at its start: 5 s.
    assert issue["ACME-20"]["active_seconds"] == 60
    assert ["2026-10-05T09:30:00Z", "2026-10-05T09:30:05Z"] in issue["ACME-12"]["active_spans"]


def test_a_resumed_file_repeating_earlier_lines_adds_no_time(tmp_path: Path) -> None:
    first = Transcript(SESSION)
    first.prompt("2026-11-05T08:00:00.000Z").answer("2026-11-05T08:01:00.000Z", message="m1")
    first.write(tmp_path)
    before = wires(tmp_path)
    # The resumed session's file starts with the same lines (same uuids), then works on its own.
    resumed = Transcript("b8b8b8b8-0000-4000-8000-000000000002")
    resumed.lines = [dict(line) for line in first.lines]
    resumed.prompt("2026-11-05T10:00:00.000Z").answer("2026-11-05T10:00:30.000Z")
    resumed.write(tmp_path)
    after = {r["session_id"]: r for r in wires(tmp_path)}
    assert after[SESSION]["active_seconds"] == before[0]["active_seconds"] == 60
    assert after["b8b8b8b8-0000-4000-8000-000000000002"]["active_seconds"] == 30


def test_a_duplicated_file_changes_nothing(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path / "home")
    before = wires(home)
    folder = active_time_home.projects_dir(home)
    shutil.copy2(folder / f"{SESSION}.jsonl", folder / "copy-of-session.jsonl")
    assert wires(home) == before


def test_a_bucket_with_requests_and_no_time_sends_zero(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.answer("2026-11-05T08:00:00.000Z")  # one request, no other event: nothing measurable
    t.write(tmp_path)
    (record,) = wires(tmp_path)
    assert record["active_seconds"] == 0 and record["active_spans"] == [] and record["active_time_version"] == 1
    validate_records([record])


def test_active_time_off_sends_none_of_the_three(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path)
    config = parse_config({"active_time": False})
    for record in wires(home, config):
        assert not set(ACTIVE_TIME_FIELDS) & set(record)


def test_sources_without_thread_events_never_carry_the_fields() -> None:
    home = Path(__file__).parent / "fixtures" / "gemini_cli" / "home"
    from ai_usage_reporter.adapters.gemini_cli import GeminiCliAdapter

    class UsageOnly:
        """An adapter without parse_events (as third-party adapters may be)."""

        name, provider, parser_version = "gemini-cli", "google", "usage-only/1"

        def __init__(self) -> None:
            self._inner = GeminiCliAdapter()

        def discover(self, ctx: DiscoveryContext) -> Any:
            return self._inner.discover(ctx)

        def parse(self, src: Any) -> Any:
            return self._inner.parse(src)

        def dedup_key(self, event: Any) -> Any:
            return self._inner.dedup_key(event)

    adapter = UsageOnly()
    collected = collect(
        Config(),
        Registry(adapters={adapter.name: adapter}),
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
        now=NOW,
    )
    assert collected.records
    assert all(not set(ACTIVE_TIME_FIELDS) & set(r.to_wire()) for r in collected.records)
    assert collected.reports["gemini-cli"].thread_events == 0


def test_the_parse_cache_keeps_thread_events(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path / "home")
    with State(tmp_path / "state.db") as state:
        first = [r.to_wire() for r in run(home, state=state).records]
        again = run(home, state=state)
        assert all(report.parsed == 0 for report in again.reports.values())
        assert [r.to_wire() for r in again.records] == first
        path = active_time_home.projects_dir(home) / f"{SESSION}.jsonl"
        cached = state.cached_events(path, FileIdentity.of(path), ClaudeCodeAdapter.parser_version)
        assert cached is not None and any(isinstance(e, ThreadEvent) for e in cached)


def test_unplaced_time_and_measured_hours_are_reported(tmp_path: Path) -> None:
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z")
    t.answer("2026-11-05T08:00:30.000Z")
    t.write(tmp_path)
    # A subagent whose only line with usage carries no tokens: its 40 s have no request.
    sub = Transcript(SESSION, agent="9a9a9a9a")
    sub.prompt("2026-11-05T08:00:00.000Z").answer("2026-11-05T08:00:40.000Z", tokens=0)
    sub.write(tmp_path)
    report = run(tmp_path).reports["claude-code"]
    assert report.unplaced_seconds == 40
    assert report.measured_seconds == 30  # 2026-11-05 lies within the last 7 days before NOW
    assert report.thread_events == 4


# -- the wire record and gating ----------------------------------------------------------------------


def test_to_wire_rounds_merges_and_clips_spans() -> None:
    day = date(2026, 10, 5)
    spans = (
        (datetime(2026, 10, 3, 22, 0, tzinfo=UTC), datetime(2026, 10, 4, 0, 0, 30, tzinfo=UTC)),
        (datetime(2026, 10, 5, 8, 0, 0, 400000, tzinfo=UTC), datetime(2026, 10, 5, 8, 0, 10, 600000, tzinfo=UTC)),
        (datetime(2026, 10, 5, 8, 0, 10, 700000, tzinfo=UTC), datetime(2026, 10, 5, 8, 0, 20, tzinfo=UTC)),
    )
    wire = make_record(day=day, active_time_version=1, active_seconds=60, active_spans=spans).to_wire()
    # The first span is clipped to 24 h before the day; the other two meet after rounding and are joined.
    assert wire["active_spans"] == [
        ["2026-10-04T00:00:00Z", "2026-10-04T00:00:30Z"],
        ["2026-10-05T08:00:00Z", "2026-10-05T08:00:20Z"],
    ]
    validate_records([wire])


def test_to_wire_leaves_out_all_three_when_one_does_not_fit() -> None:
    spans = ((datetime(2026, 10, 5, 8, tzinfo=UTC), datetime(2026, 10, 5, 9, tzinfo=UTC)),)
    for extra in (
        {"active_time_version": 0, "active_seconds": 3600, "active_spans": spans},
        {"active_time_version": 1, "active_seconds": 10_000_001, "active_spans": spans},
        {"active_time_version": 1, "active_seconds": 60, "active_spans": spans},  # union longer than the sum
        {"active_time_version": 1, "active_seconds": 60},  # not all three
    ):
        wire = make_record(**extra).to_wire()
        assert not set(ACTIVE_TIME_FIELDS) & set(wire), extra
    assert "active_spans" in make_record(active_time_version=1, active_seconds=3600, active_spans=spans).to_wire()


def test_more_than_1000_spans_make_a_valid_record() -> None:
    start = datetime(2026, 10, 5, 0, 0, tzinfo=UTC).timestamp()
    spans = tuple(
        (datetime.fromtimestamp(start + i * 50, tz=UTC), datetime.fromtimestamp(start + i * 50 + 10, tz=UTC))
        for i in range(1500)
    )
    wire = make_record(active_time_version=1, active_seconds=15000, active_spans=spans).to_wire()
    assert len(wire["active_spans"]) == 1000 and wire["active_seconds"] == 15000
    validate_records([wire])


def test_the_three_fields_are_gated_as_a_group() -> None:
    spans = ((datetime(2026, 10, 5, 8, tzinfo=UTC), datetime(2026, 10, 5, 8, 1, tzinfo=UTC)),)
    wire = make_record(route="gw", active_time_version=1, active_seconds=60, active_spans=spans).to_wire()
    assert gate_optional_fields(wire, set(OPTIONAL_FIELDS)) == wire
    for accepted in ({"route"}, {"route", "active_seconds"}, {"route", "active_time_version", "active_spans"}):
        gated = gate_optional_fields(wire, accepted)
        assert not set(ACTIVE_TIME_FIELDS) & set(gated) and gated["route"] == "gw"


@pytest.mark.parametrize(
    ("listed", "carried"),
    [((), False), (("active_seconds", "active_spans"), False), (ACTIVE_TIME_FIELDS, True)],
)
def test_push_sends_them_only_to_a_server_that_lists_all_three(
    tmp_path: Path, listed: tuple[str, ...], carried: bool
) -> None:
    home = active_time_home.build(tmp_path / "home")
    server = FakeServer()
    server.info["optional_fields"] = list(listed)
    sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        deliver(run(home, state=state).records, sink, state)
    sent = [r for batch in server.posts for r in batch]
    assert len(sent) == 2
    for record in sent:
        assert (set(ACTIVE_TIME_FIELDS) <= set(record)) is carried
        assert carried or not set(ACTIVE_TIME_FIELDS) & set(record)
    validate_records(sent)


def test_records_are_sent_again_when_the_server_starts_listing_them(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path / "home")
    server = FakeServer()
    sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        assert deliver(run(home, state=state).records, sink, state).sent == 2
        assert deliver(run(home, state=state).records, sink, state).sent == 0
        server.info["optional_fields"] = list(OPTIONAL_FIELDS)
        newer = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)
        assert deliver(run(home, state=state).records, newer, state).sent == 2
    assert all("active_seconds" in r for r in server.posts[-1])
    assert all("active_seconds" not in r for r in server.posts[0])


def test_a_superseded_segment_is_cleared_to_zero_time(tmp_path: Path) -> None:
    home = active_time_home.build(tmp_path / "home")
    server = FakeServer()
    server.info["optional_fields"] = list(OPTIONAL_FIELDS)
    sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        records = run(home, state=state).records
        deliver(records, sink, state)
        # Today's split gives the haiku requests another key (another segment): the old key is cleared.
        moved = [
            replace(r, idempotency_key="moved-" + r.idempotency_key[:20]) if r.model == HAIKU else r for r in records
        ]
        zeroed = superseded_records(moved, state)
    (wire,) = [z.to_wire() for z in zeroed]
    assert wire["model"] == HAIKU and wire["tokens"] == {}
    assert wire["active_time_version"] == 1 and wire["active_seconds"] == 0 and wire["active_spans"] == []
    validate_records([wire])


# -- the CLI -----------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = active_time_home.build(tmp_path / "home")
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.toml").write_text('url = "https://specivo.example.com"\n', encoding="utf-8")
    return home


def test_show_payload_and_dry_run_print_the_fields(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["show-payload", "--no-git"], ["push", "--dry-run", "--no-git"]):
        assert cli.main(argv) == 0
        out, err = capsys.readouterr()
        records = usage_batch(out)["records"]
        assert {r["active_seconds"] for r in records} == {218, 507}
        validate_records(records)
        assert "CANARY" not in out + err


def test_doctor_shows_measured_hours_and_unplaced_seconds(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["doctor", "--offline"])
    out, _ = capsys.readouterr()
    assert "active time: 0.0 h measured in the last 7 days, 0 s unplaced" in out
    assert "CANARY" not in out


def test_a_record_whose_active_time_is_refused_still_delivers_its_tokens(tmp_path: Path) -> None:
    from tests.conftest import ok, result_item

    home = active_time_home.build(tmp_path / "home")
    server = FakeServer()
    server.info["optional_fields"] = list(OPTIONAL_FIELDS)

    def refuse_spans(records: list[dict[str, Any]]) -> Any:
        def decide(i: int, r: dict[str, Any]) -> dict[str, Any]:
            error = {"code": "invalid_record", "message": "span in the future", "field": "active_spans"}
            return result_item(i, r["idempotency_key"], "rejected", error=error)

        return ok(records, decide)

    server.responders.append(refuse_spans)
    sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=server, sleep=lambda s: None)
    with State.in_memory() as state:
        summary = deliver(run(home, state=state).records, sink, state)
        assert summary.active_time_resent == 2 and summary.warnings["active_time_dropped"] == 2
        assert all(not set(ACTIVE_TIME_FIELDS) & set(r) and r["tokens"] for r in server.posts[1])
        # Unchanged buckets are not sent again on the next push.
        assert deliver(run(home, state=state).records, sink, state).sent == 0


def test_in_file_sidechain_lines_are_their_own_thread(tmp_path: Path) -> None:
    # Older Claude Code wrote Task sidechains into the main file; such a line is a thread of its own,
    # for its usage, its marks and its events alike.
    t = Transcript(SESSION)
    t.prompt("2026-11-05T08:00:00.000Z").answer("2026-11-05T08:00:10.000Z")
    side = Transcript(SESSION, agent="c0c0c0c0")
    side.prompt("2026-11-05T08:00:01.000Z").answer("2026-11-05T08:00:31.000Z")
    t.lines.extend(side.lines)
    for n, line in enumerate(t.lines):
        line["uuid"] = f"u-{n}"
    t.write(tmp_path)
    adapter = ClaudeCodeAdapter()
    (src,) = list(adapter.discover(DiscoveryContext(home=tmp_path, env={})))
    events = list(parse_all(adapter, src))
    assert {e.thread_id for e in events if isinstance(e, ThreadEvent)} == {None, "agent-c0c0c0c0"}
    assert {e.thread_id for e in events if isinstance(e, UsageEvent)} == {None, "agent-c0c0c0c0"}
    # Main 10 s + sidechain 30 s, overlapping: 40 s summed, 08:00:00..08:00:31 joined.
    record = by_model(wires(tmp_path))[SONNET]
    assert record["active_seconds"] == 40
    assert record["active_spans"] == [["2026-11-05T08:00:00Z", "2026-11-05T08:00:31Z"]]
