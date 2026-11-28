"""Work activity summaries: classification, the summary, the record, levels, delivery and privacy."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter import cli
from ai_usage_reporter.activity_record import (
    ACTIVITY_SCHEMA,
    ActivityRecord,
    ActivityRecordError,
    activity_key,
    thread_hash,
)
from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.config import Config, ConfigError, parse_config, render_template
from ai_usage_reporter.pipeline import Collected, collect, deliver, deliver_activity
from ai_usage_reporter.record import idempotency_key
from ai_usage_reporter.sinks.json_sink import JsonSink
from ai_usage_reporter.sinks.specivo import HttpResponse, SpecivoSink
from ai_usage_reporter.state import State
from ai_usage_reporter.testing import (
    ACTIVITY_INFO,
    activity_contract_file,
    activity_rule_errors,
    activity_validator,
    validate_activity_records,
)
from ai_usage_reporter.work_activity import (
    MAX_TIMELINE,
    GroupWork,
    ToolRun,
    build_timeline,
    classify_argv,
    classify_command,
    label_spans,
    patch_lines,
    summarise,
    text_lines,
    unified_diff_lines,
)
from tests.conftest import INFO, activity_records, usage_batch
from tests.fixtures import work_activity_home as fx

EXAMPLES = files("ai_usage_reporter.contract").joinpath("agent-activity", "v1", "examples")
SONNET = "claude-sonnet-4-5"
KEYS = ("idempotency_key", "usage_key", "session_id")


# -- classifying commands -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "category"),
    [
        ("cd x && FOO=1 uv run pytest -k y | tee log", "test"),
        ("echo start; git commit -m 'msg; with separator'", "git"),
        ("some-unknown-program --flag", "shell"),
        ("ls -la", "shell"),
        ("python3 -m pytest -q", "test"),
        ("sudo -u builder make test-unit", "test"),
        ("make -C sub test", "test"),
        ("make lint", "shell"),
        ("npm test", "test"),
        ("npm run test:unit", "test"),
        ("go test ./...", "test"),
        ("cargo build", "shell"),
        ("timeout 60 pytest", "test"),
        ("env A=1 B=2 /usr/bin/git diff", "git"),
        ('bash -lc "npm test"', "test"),
        ("npx vitest run", "test"),
        ("uv run --with x pytest", "test"),
        ("playwright test", "test"),
        ("git status && pytest", "test"),
        ("echo 'unbalanced", "shell"),
        ("# a comment", "shell"),
        ("", "shell"),
    ],
)
def test_classify_command(command: str, category: str) -> None:
    assert classify_command(command) == category


def test_classify_argv_looks_into_a_shell_script() -> None:
    assert classify_argv(["/bin/zsh", "-lc", "cd /x && cargo test"]) == "test"
    assert classify_argv(["/bin/zsh", "-lc", "git log -1"]) == "git"
    assert classify_argv(["/bin/zsh", "-lc", "rg pattern"]) == "shell"
    assert classify_argv(["pytest"]) == "test"


def test_line_counts() -> None:
    assert patch_lines(["+a", "-b", " c", "+d", 5]) == (2, 1)
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+more\n context\n"
    assert unified_diff_lines(diff) == (2, 1)
    assert text_lines("") == 0 and text_lines("a") == 1 and text_lines("a\nb\n") == 2 and text_lines("a\nb") == 2


# -- the timeline -------------------------------------------------------------------------------------


def test_overlapping_runs_take_the_most_specific_label() -> None:
    runs = [ToolRun(0, 100, "read"), ToolRun(10, 20, "test"), ToolRun(50, 60, "edit")]
    assert label_spans(runs) == [
        (0, 10, "read"),
        (10, 20, "test"),
        (20, 50, "read"),
        (50, 60, "edit"),
        (60, 100, "read"),
    ]


def test_timeline_folds_short_segments_but_keeps_isolated_ones() -> None:
    pieces: list[tuple[float, float, str | None]] = [
        (0, 40, "edit"),
        (40, 50, "git"),  # 10 s between edit and test: into the longer neighbour (test, 60 s)
        (50, 110, "test"),
        (500, 505, None),  # alone: kept, as other
    ]
    assert build_timeline(pieces, (0, 10_000)) == [(0, 40, "edit"), (40, 110, "test"), (500, 505, "other")]


def test_timeline_unlabelled_time_takes_the_label_before_else_after() -> None:
    pieces: list[tuple[float, float, str | None]] = [(0, 40, None), (40, 100, "read"), (100, 200, None)]
    assert build_timeline(pieces, (0, 10_000)) == [(0, 200, "read")]


def test_timeline_is_clipped_rounded_and_limited() -> None:
    assert build_timeline([(9.4, 50.6, "edit")], (20, 40)) == [(20, 40, "edit")]
    many: list[tuple[float, float, str | None]] = [(i * 100.0, i * 100.0 + 40, "read") for i in range(400)]
    timeline = build_timeline(many, (0, 1e9))
    assert len(timeline) == MAX_TIMELINE
    assert all(a[1] <= b[0] for a, b in zip(timeline, timeline[1:], strict=False))
    assert timeline[0][0] == 0 and timeline[-1][1] == 399 * 100 + 40


def test_summary_counts() -> None:
    group = GroupWork(
        runs=[
            ToolRun(0, 10, "edit", lines_added=3, files=("a",), seconds=10),
            ToolRun(20, 30, "edit", lines_added=1, lines_removed=2, files=("a",), seconds=10),
            ToolRun(40, 50, "edit", failed=True, files=("b",), seconds=4.6),
            ToolRun(60, 70, "test", failed=True, seconds=10),
            ToolRun(80, 90, "shell", failed=True, seconds=10),
            ToolRun(95, 96, "subagent", spawns=True, seconds=1),
        ],
        spawns=2,
    )
    summary = summarise(group, (0, 1e9))
    assert summary.categories == (("edit", 3, 25), ("shell", 1, 10), ("test", 1, 10), ("subagent", 1, 1))
    # A failed edit changed no file; a failed shell run is no failed test.
    assert (summary.files_changed, summary.lines_added, summary.lines_removed) == (1, 4, 2)
    assert (summary.test_runs, summary.test_failed, summary.subagents_spawned) == (1, 1, 3)


# -- the record and the contract ----------------------------------------------------------------------


def test_the_record_has_exactly_the_fields_of_the_levels_it_builds() -> None:
    schema = activity_contract_file("record.schema.json")
    counts_level = {
        name for name, prop in schema["properties"].items() if "Level counts" in prop.get("description", "")
    }
    any_level = set(schema["required"]) | {"on_behalf_of"}
    assert {f.name for f in fields(ActivityRecord)} == counts_level | any_level
    higher = {name for name, prop in schema["properties"].items() if "Level names" in prop.get("description", "")}
    assert higher == {"mcp_servers", "commands", "files", "files_outside", "agent_type"}


def test_recommended_activity_key() -> None:
    expected = hashlib.sha256(b"v1|activity|codex|s-1|-|2026-11-05|ACME-1").hexdigest()
    assert activity_key("codex", "s-1", None, date(2026, 11, 5), "acme-1") == expected
    expected = hashlib.sha256(b"v1|activity|codex|s-1|t1|2026-11-05|-").hexdigest()
    assert activity_key("codex", "s-1", "t1", date(2026, 11, 5), None) == expected


@pytest.mark.parametrize("name", ["minimal.json", "counts.json", "names.json", "commits.json", "on-behalf.json"])
def test_valid_examples_pass_the_schema(name: str) -> None:
    batch = json.loads(EXAMPLES.joinpath("valid", name).read_text("utf-8"))
    activity_validator("batch.schema.json").validate(batch)
    for record in batch["records"]:
        assert not activity_rule_errors(record)


def test_invalid_examples_fail_as_the_server_says() -> None:
    expected = json.loads(EXAMPLES.joinpath("expected.json").read_text("utf-8"))["invalid"]
    validator = activity_validator("record.schema.json")
    batch_validator = activity_validator("batch.schema.json")
    for name, answer in expected.items():
        batch = json.loads(EXAMPLES.joinpath("invalid", name).read_text("utf-8"))
        assert batch_validator.is_valid(batch) == answer["schema_valid"], name
        if "records" not in answer:
            continue  # a whole-batch error
        for record, outcome in zip(batch["records"], answer["records"], strict=True):
            assert validator.is_valid(record) == outcome["schema_valid"], (name, outcome)
            # The rules beyond the schema that a counts reporter must keep (the level, test runs, the timeline).
            if outcome["schema_valid"] and not outcome["field"].startswith("files."):
                assert outcome["field"] in activity_rule_errors(record), (name, outcome)


def test_to_wire_refuses_what_does_not_fit() -> None:
    base = ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5))
    with pytest.raises(ActivityRecordError):
        ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5), categories=(("model", 1, 1),)).to_wire()
    with pytest.raises(ActivityRecordError):
        ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5), test_runs=(1, 2)).to_wire()
    with pytest.raises(ActivityRecordError):
        ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5), thread="a/path").to_wire()
    with pytest.raises(ActivityRecordError):
        ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5), level="names").to_wire()
    # Segments that break the contract's rules are left out; the rest go.
    day = datetime(2026, 11, 5, tzinfo=UTC).timestamp()
    timeline = (
        (int(day) + 10, int(day) + 20, "read"),
        (int(day) + 15, int(day) + 30, "edit"),  # overlaps the one before
        (int(day) + 40, int(day) + 40, "edit"),  # empty
        (int(day) - 3 * 86400, int(day) - 3 * 86400 + 5, "edit"),  # not within 24 h of the day
        (int(day) + 50, int(day) + 60, "model"),  # not a category
        (int(day) + 60, int(day) + 70, "git"),
    )
    wire = ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5), timeline=timeline).to_wire()
    assert wire["timeline"] == [
        ["2026-11-05T00:00:10Z", "2026-11-05T00:00:20Z", "read"],
        ["2026-11-05T00:01:00Z", "2026-11-05T00:01:10Z", "git"],
    ]
    assert base.to_wire() == {
        "idempotency_key": "k",
        "usage_key": "u",
        "tool": "codex",
        "session_id": "s-1",
        "usage_date": "2026-11-05",
        "level": "counts",
        "activity_version": 1,
        "categories": {},
    }


# -- the synthetic homes ------------------------------------------------------------------------------


def _collect(home: Path, adapter: Any, config: Config | None = None, state: State | None = None) -> Collected:
    return collect(
        config or Config(),
        Registry(adapters={adapter.name: adapter}),
        state=state,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        git=None,
    )


def _strip(wire: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in wire.items() if k not in KEYS}


def _stat(count: int, seconds: int) -> dict[str, int]:
    return {"count": count, "seconds": seconds}


def test_claude_code_home_gives_the_numbers_worked_out_by_hand(tmp_path: Path) -> None:
    collected = _collect(fx.build_claude(tmp_path / "home"), ClaudeCodeAdapter())
    wires = [r.to_wire() for r in collected.activity]
    validate_activity_records(wires)
    main_day1, main_day2, sub = wires
    assert _strip(main_day1) == {
        "tool": "claude-code",
        "usage_date": "2026-11-05",
        "level": "counts",
        "activity_version": 1,
        "categories": {
            "search": _stat(1, 1),
            "edit": _stat(3, 4),
            "shell": _stat(1, 1),
            "test": _stat(1, 60),
            "git": _stat(1, 2),
            "subagent": _stat(1, 10),
            "mcp": _stat(1, 3),
        },
        "thread_kind": "main",
        "files_changed": 2,
        "lines_added": 7,
        "lines_removed": 1,
        "subagents_spawned": 1,
        "test_runs": {"count": 1, "failed": 1},
        "timeline": [
            ["2026-11-05T10:00:00Z", "2026-11-05T10:01:10Z", "test"],
            ["2026-11-05T10:01:10Z", "2026-11-05T10:02:21Z", "edit"],
            ["2026-11-05T23:59:50Z", "2026-11-05T23:59:55Z", "other"],
        ],
    }
    assert main_day2["usage_date"] == "2026-11-06" and main_day2["categories"] == {}
    assert main_day2["timeline"] == [["2026-11-05T23:59:55Z", "2026-11-06T00:00:11Z", "subagent"]]
    assert _strip(sub) == {
        "tool": "claude-code",
        "usage_date": "2026-11-06",
        "level": "counts",
        "activity_version": 1,
        "categories": {"read": _stat(1, 1)},
        "thread": thread_hash("claude-code", fx.SESSION, "agent-w0rker01"),
        "thread_kind": "subagent",
        "files_changed": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "subagents_spawned": 0,
        "test_runs": {"count": 0, "failed": 0},
        "timeline": [["2026-11-06T00:00:06Z", "2026-11-06T00:00:09Z", "read"]],
    }
    # Each day's records are anchored to that day's usage record, and keyed as the contract recommends.
    for wire in wires:
        day = date.fromisoformat(wire["usage_date"])
        assert wire["usage_key"] == idempotency_key("claude-code", fx.SESSION, SONNET, day)
        assert wire["idempotency_key"] == activity_key("claude-code", fx.SESSION, wire.get("thread"), day, None)
    assert {w["usage_key"] for w in wires} <= {r.idempotency_key for r in collected.records}


def test_codex_home_gives_the_numbers_worked_out_by_hand(tmp_path: Path) -> None:
    collected = _collect(fx.build_codex(tmp_path / "home"), CodexAdapter())
    (wire,) = [r.to_wire() for r in collected.activity]
    validate_activity_records([wire])
    assert wire["usage_key"] == collected.records[0].idempotency_key
    assert _strip(wire) == {
        "tool": "codex",
        "usage_date": "2026-11-05",
        "level": "counts",
        "activity_version": 1,
        "categories": {
            "read": _stat(1, 1),
            "search": _stat(1, 2),
            "edit": _stat(1, 1),
            "test": _stat(1, 60),
            "web": _stat(1, 5),
            "subagent": _stat(1, 0),
            "mcp": _stat(1, 4),
        },
        "thread_kind": "main",
        "files_changed": 3,
        "lines_added": 5,
        "lines_removed": 1,
        "subagents_spawned": 1,
        "test_runs": {"count": 1, "failed": 1},
        "timeline": [
            ["2026-11-05T14:00:00Z", "2026-11-05T14:01:30Z", "test"],
            ["2026-11-05T14:01:30Z", "2026-11-05T14:02:10Z", "search"],
            ["2026-11-05T14:02:10Z", "2026-11-05T14:05:00Z", "subagent"],
        ],
    }


def test_an_issue_mark_splits_the_summary_like_usage(tmp_path: Path) -> None:
    home = tmp_path / "home"
    main = fx.Session(fx.SESSION)
    main.prompt("2026-11-05T09:00:00.000Z", origin="human")
    mark = {"issue_ref": "ACME-1", "status_id": 2}
    main.answer("2026-11-05T09:00:05.000Z", tool="mcp__specivo__specivo_update_issue", call="m1", arguments=mark)
    main.tool_result("2026-11-05T09:00:06.000Z", "m1")
    main.answer("2026-11-05T09:00:10.000Z", tool="Bash", call="b1", arguments={"command": "pytest"})
    main.tool_result("2026-11-05T09:00:40.000Z", "b1")
    main.answer("2026-11-05T09:00:45.000Z", tool="mcp__specivo__specivo_update_issue", call="m2", arguments={
        "issue_ref": "ACME-2", "status_id": 2})  # fmt: skip
    main.tool_result("2026-11-05T09:00:46.000Z", "m2")
    main.answer("2026-11-05T09:00:50.000Z", tool="Bash", call="b2", arguments={"command": "git diff"})
    main.tool_result("2026-11-05T09:00:52.000Z", "b2")
    main.answer("2026-11-05T09:00:55.000Z")
    main.turn_end("2026-11-05T09:00:56.000Z")
    main.write(home)
    collected = _collect(home, ClaudeCodeAdapter())
    by_issue = {r.attribution.issue_ref if r.attribution else None: r for r in collected.records}
    assert set(by_issue) == {"ACME-1", "ACME-2"}
    wires = {w["usage_key"]: w for w in (r.to_wire() for r in collected.activity)}
    first, second = wires[by_issue["ACME-1"].idempotency_key], wires[by_issue["ACME-2"].idempotency_key]
    assert first["categories"]["test"] == _stat(1, 30) and "git" not in first["categories"]
    assert second["categories"]["git"] == _stat(1, 2) and "test" not in second["categories"]
    day = date(2026, 11, 5)
    assert first["idempotency_key"] == activity_key("claude-code", fx.SESSION, None, day, "ACME-1")
    assert second["idempotency_key"] == activity_key("claude-code", fx.SESSION, None, day, "ACME-2")

    # Without issue segments the session is one group again; the two records sent before are cleared.
    with State.in_memory() as state:
        server = Server()
        sink = server.sink()
        collected = _collect(home, ClaudeCodeAdapter(), state=state)
        deliver(collected.records, sink, state)
        deliver_activity(collected.activity, sink, state)
        unsplit = _collect(home, ClaudeCodeAdapter(), Config(agent_log_issues=False), state)
        deliver(unsplit.records, sink, state)
        deliver_activity(unsplit.activity, sink, state)
        last = server.activity_posts[-1]
        cleared = [r for r in last if r["idempotency_key"] in (first["idempotency_key"], second["idempotency_key"])]
        assert len(cleared) == 2 and all(r["categories"] == {} and r["timeline"] == [] for r in cleared)
        assert len(last) == 3
        validate_activity_records(last)


# -- levels -------------------------------------------------------------------------------------------


def test_config_levels() -> None:
    config = parse_config(
        {"activity": {"level": "names", "repos": {"github.com/client/*": "off", "github.com/acme/*": "counts"}}}
    )
    assert config.activity_level_for("github.com/client/app") == "off"
    assert config.activity_level_for("github.com/acme/app") == "counts"
    assert config.activity_level_for("github.com/other/app") == "names"
    # Unknown repository: at most counts. Higher configured levels send counts (not built yet).
    assert config.activity_level_for(None) == "counts"
    assert config.activity_sent_level("github.com/other/app") == "counts"
    assert parse_config({"activity": {"level": "off"}}).activity_sent_level(None) == "off"
    assert Config().activity_sent_level(None) == "counts"
    for bad in ({"activity": {"level": "all"}}, {"activity": {"repos": {"x": "loud"}}}, {"activity": {"x": 1}}):
        with pytest.raises(ConfigError):
            parse_config(bad)
    template = render_template("https://specivo.example.com")
    import tomllib

    assert parse_config(tomllib.loads(template)).activity_level == "counts"


def test_level_off_globally_or_for_the_repository(tmp_path: Path) -> None:
    home = fx.build_codex(tmp_path / "home")  # the rollout names github.com/acme/svc
    assert _collect(home, CodexAdapter()).activity
    off = parse_config({"activity": {"level": "off"}})
    collected = _collect(home, CodexAdapter(), off)
    assert collected.records and not collected.activity
    assert collected.reports["codex"].activity_off == 1
    repo_off = parse_config({"activity": {"repos": {"github.com/acme/*": "off"}}})
    assert not _collect(home, CodexAdapter(), repo_off).activity
    other_off = parse_config({"activity": {"repos": {"github.com/client/*": "off"}}})
    assert _collect(home, CodexAdapter(), other_off).activity
    names = parse_config({"activity": {"level": "commits"}})
    assert {r.level for r in _collect(home, CodexAdapter(), names).activity} == {"counts"}


# -- delivery -----------------------------------------------------------------------------------------

Decide = Callable[[int, dict[str, Any]], dict[str, Any]]


@dataclass
class Server:
    """A scripted Specivo server with the usage and the work activity endpoints."""

    activity: bool = True
    decide: Decide | None = None
    activity_info: dict[str, Any] = field(default_factory=lambda: dict(ACTIVITY_INFO))
    usage_posts: list[list[dict[str, Any]]] = field(default_factory=list)
    activity_posts: list[list[dict[str, Any]]] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    headers: list[Mapping[str, str]] = field(default_factory=list)
    stored: set[str] = field(default_factory=set)
    activity_status: list[int] = field(default_factory=list)

    def sink(self) -> SpecivoSink:
        return SpecivoSink("https://specivo.example.com", "spv_test", transport=self, sleep=lambda s: None)

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        self.headers.append(headers)
        if url.endswith("/agent-activity/ingest"):
            if not self.activity:
                return HttpResponse(404, {}, b'{"detail": "Not Found"}')
            if method == "GET":
                return HttpResponse(200, {}, json.dumps(self.activity_info).encode())
            if self.activity_status:
                status = self.activity_status.pop(0)
                if status != 200:
                    return HttpResponse(status, {"Retry-After": "1"}, b"{}")
            assert body is not None
            records = json.loads(body)["records"]
            self.order.append("activity")
            self.activity_posts.append(records)
            results = []
            for i, r in enumerate(records):
                item = self._activity_result(i, r)
                if self.decide is not None:
                    item = self.decide(i, r) or item
                results.append(item)
            answer = {"schema": ACTIVITY_SCHEMA, "server_time": "2026-11-06T12:00:00Z", "results": results}
            return HttpResponse(200, {}, json.dumps(answer).encode())
        if method == "GET":
            return HttpResponse(200, {}, json.dumps(INFO).encode())
        assert body is not None
        records = json.loads(body)["records"]
        self.order.append("usage")
        self.usage_posts.append(records)
        self.stored.update(r["idempotency_key"] for r in records)
        results = [
            {"index": i, "idempotency_key": r["idempotency_key"], "status": "created", "entry_id": i + 1,
             "revision": 1, "attribution": {"method": "none"}, "warnings": [], "error": None}
            for i, r in enumerate(records)
        ]  # fmt: skip
        return HttpResponse(200, {}, json.dumps({"schema": "specivo.ai-usage.v1", "results": results}).encode())

    def _activity_result(self, index: int, record: dict[str, Any]) -> dict[str, Any]:
        if record["usage_key"] not in self.stored:
            return rejected(index, record, "usage_not_found")
        key = record["idempotency_key"]
        return {"index": index, "idempotency_key": key, "status": "created", "activity_id": index + 1,
                "revision": 1, "usage_entry_id": 1, "warnings": [], "error": None}  # fmt: skip


def rejected(index: int, record: dict[str, Any], code: str, field_name: str | None = None) -> dict[str, Any]:
    error = {"code": code, "message": "refused", "field": field_name}
    return {"index": index, "idempotency_key": record["idempotency_key"], "status": "rejected", "activity_id": None,
            "revision": None, "usage_entry_id": None, "warnings": [], "error": error}  # fmt: skip


def _pushed(home: Path, server: Server, state: State, config: Config | None = None) -> Any:
    collected = _collect(home, ClaudeCodeAdapter(), config, state)
    sink = server.sink()
    deliver(collected.records, sink, state)
    return deliver_activity(collected.activity, sink, state)


def test_activity_goes_after_the_usage_with_the_same_key_and_only_once(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")
    server = Server()
    with State.in_memory() as state:
        summary = _pushed(home, server, state)
        assert summary.supported and summary.sent == 3 and summary.statuses == {"created": 3}
        assert server.order == ["usage", "activity"]
        assert {h["Authorization"] for h in server.headers} == {"Bearer spv_test"}
        validate_activity_records(server.activity_posts[0])
        again = _pushed(home, server, state)
        assert again.sent == 0 and again.skipped_unchanged == 3 and len(server.activity_posts) == 1


def test_a_server_without_the_endpoint_gets_no_activity(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")
    server = Server(activity=False)
    with State.in_memory() as state:
        summary = _pushed(home, server, state)
        assert summary.supported is False and summary.sent == 0 and summary.error is None
        assert server.usage_posts and not server.activity_posts
        assert not list(state.activities())


def test_a_server_that_does_not_list_the_version_gets_no_activity(tmp_path: Path) -> None:
    server = Server(activity_info={**ACTIVITY_INFO, "versions": ["specivo.agent-activity.v9"]})
    with State.in_memory() as state:
        assert _pushed(fx.build_claude(tmp_path / "home"), server, state).supported is False
        assert not server.activity_posts


def test_retries_usage_not_found_and_internal_error_but_not_invalid_records(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")

    def decide(index: int, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("thread_kind") == "subagent":
            return rejected(index, record, "usage_not_found")
        if record["usage_date"] == "2026-11-06":
            return rejected(index, record, "internal_error")
        return rejected(index, record, "invalid_record", "timeline.0")

    server = Server(decide=decide)
    with State.in_memory() as state:
        first = _pushed(home, server, state)
        assert first.errors == {"usage_not_found": 1, "internal_error": 1, "invalid_record": 1}
        assert state.activity_status_counts() == {"pending": 2, "rejected": 1}
        server.decide = None
        second = _pushed(home, server, state)
        # The two waiting records go again; the refused one is not sent again while it is unchanged.
        assert second.sent == 2 and second.skipped_final == 1 and second.statuses == {"created": 2}
        assert state.activity_status_counts() == {"sent": 2, "rejected": 1}


def test_activity_waits_for_its_usage_record(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")
    server = Server()
    with State.in_memory() as state:
        collected = _collect(home, ClaudeCodeAdapter(), state=state)
        summary = deliver_activity(collected.activity, server.sink(), state)  # no usage push before it
        assert summary.sent == 0 and summary.waiting == 3 and not server.activity_posts


def test_batches_respect_the_endpoint_limits_and_retry(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")
    server = Server(activity_info={**ACTIVITY_INFO, "max_records": 2}, activity_status=[429, 503])
    with State.in_memory() as state:
        summary = _pushed(home, server, state)
    assert summary.statuses == {"created": 3}
    assert [len(batch) for batch in server.activity_posts] == [2, 1]


def test_a_failing_activity_endpoint_never_fails_the_push(tmp_path: Path) -> None:
    home = fx.build_claude(tmp_path / "home")
    server = Server(activity_status=[503, 503, 503])
    with State.in_memory() as state:
        summary = _pushed(home, server, state)
        assert summary.statuses == {"pending": 3} and state.activity_status_counts() == {"pending": 3}
        assert _pushed(home, server, state).statuses == {"created": 3}


def test_json_sink_writes_activity_beside_the_usage(tmp_path: Path) -> None:
    output = tmp_path / "out" / "usage.json"
    sink = JsonSink(output=output)
    sink.send([])
    sink.send_activity([ActivityRecord("k", "u", "codex", "s-1", date(2026, 11, 5)).to_wire()])
    assert json.loads(output.read_text())["records"] == []
    assert json.loads((tmp_path / "out" / "usage.activity.json").read_text())["schema"] == ACTIVITY_SCHEMA


# -- the command line and privacy ---------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    fx.build_claude(home)
    fx.build_codex(home)
    dirs = {"config": tmp_path / "cfg", "state": tmp_path / "state", "home": home}
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(dirs["config"]))
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(dirs["state"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_cli_test")
    dirs["config"].mkdir()
    (dirs["config"] / "config.toml").write_text('url = "https://specivo.example.com"\n', encoding="utf-8")
    return dirs


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    fake = Server()

    def make(url: str, key: str, **kwargs: Any) -> SpecivoSink:
        return SpecivoSink(url, key, transport=fake, sleep=lambda s: None, **kwargs)

    monkeypatch.setattr(cli, "SpecivoSink", make)
    return fake


def test_show_payload_prints_exactly_what_a_push_sends(
    env: dict[str, Path], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    shown = activity_records(out)
    assert len(shown) == 4 and "# work activity records" in err and "4 work activity records would be sent" in err
    assert len(usage_batch(out)["records"]) == 3
    assert cli.main(["push", "--dry-run", "--no-git"]) == 0
    out, _ = capsys.readouterr()
    assert activity_records(out) == shown
    assert cli.main(["push", "--no-git"]) == 0
    _, err = capsys.readouterr()
    assert "work activity: sent 4 (created 4)" in err
    assert [r for batch in server.activity_posts for r in batch] == shown
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert activity_records(out) == [] and "0 work activity records would be sent" in err and "4 unchanged" in err


def test_level_off_prints_and_sends_no_activity(
    env: dict[str, Path], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    config = env["config"] / "config.toml"
    config.write_text('url = "https://specivo.example.com"\n[activity]\nlevel = "off"\n', encoding="utf-8")
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert activity_records(out) == [] and "# work activity" not in err
    assert cli.main(["push", "--no-git"]) == 0
    assert server.usage_posts and not server.activity_posts
    assert cli.main(["doctor", "--offline"]) == 0
    out, _ = capsys.readouterr()
    assert "work activity: level off" in out and "codex: 0 records at counts, 1 left out (level off)" in out


def test_doctor_shows_levels_and_the_server(
    env: dict[str, Path], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    (env["config"] / "config.toml").write_text(
        'url = "https://specivo.example.com"\ngit_enrichment = false\n'
        '[activity]\nlevel = "names"\n[activity.repos]\n"github.com/acme/*" = "off"\n',
        encoding="utf-8",
    )
    assert cli.main(["push", "--no-git"]) == 0
    capsys.readouterr()
    cli.main(["doctor"])
    out, _ = capsys.readouterr()
    assert "work activity: level names" in out and "repositories github.com/acme/*: off" in out
    assert "level names is not built yet" in out and "git enrichment is off" in out
    # Codex logs its repository (held off by the pattern); Claude Code's is unknown here (at most counts).
    assert "claude-code: 3 records at counts, 0 left out" in out and "codex: 0 records at counts, 1 left out" in out
    assert f"work activity: takes {ACTIVITY_SCHEMA}" in out and "work activity sent: sent 3" in out
    server.activity = False
    cli.main(["doctor"])
    out, _ = capsys.readouterr()
    assert "the server takes none" in out


def test_a_server_without_activity_is_remembered_by_the_preview(
    env: dict[str, Path], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    server.activity = False
    assert cli.main(["push", "--no-git"]) == 0
    _, err = capsys.readouterr()
    assert "work activity: the server takes none" in err
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert activity_records(out) == [] and "the server took none at the last push" in err


def _state_text(path: Path) -> str:
    """Everything in the state database, the compressed parse cache included."""
    db = sqlite3.connect(path)
    try:
        parts: list[str] = []
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in db.execute(f"SELECT * FROM {table}").fetchall():  # noqa: S608 - names from sqlite_master
                for value in row:
                    if isinstance(value, bytes):
                        try:
                            value = zlib.decompress(value)
                        except zlib.error:
                            pass
                        parts.append(value.decode("utf-8", errors="replace"))
                    else:
                        parts.append(str(value))
        return "\n".join(parts)
    finally:
        db.close()


def test_canaries_never_leave_at_counts(
    env: dict[str, Path], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    home = env["home"]
    raw = "".join(p.read_text("utf-8", errors="replace") for p in home.rglob("*") if p.is_file())
    for canary in fx.CANARIES:
        assert canary in raw, f"canary {canary!r} is not planted"
    console = ""
    for argv in (["show-payload", "--no-git"], ["push", "--dry-run", "--no-git"], ["push", "--no-git"], ["doctor"]):
        cli.main(argv)
        out, err = capsys.readouterr()
        console += out + err
    payloads = json.dumps(server.activity_posts) + json.dumps(server.usage_posts)
    assert server.activity_posts
    state = _state_text(env["state"] / "state.db")
    for canary in fx.CANARIES:
        assert canary not in payloads, f"{canary} in a payload"
        assert canary not in console, f"{canary} on the console"
        assert canary not in state, f"{canary} in the state database"
