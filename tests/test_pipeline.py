"""Buckets, long context, enrichment, exclusion and the parse cache."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext, Registry
from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.aggregate import aggregate
from ai_usage_reporter.config import Config, CostRule, SourceSettings
from ai_usage_reporter.enrich import GitInfo
from ai_usage_reporter.longcontext import Thresholds
from ai_usage_reporter.model import TokenCounts, UsageEvent
from ai_usage_reporter.pipeline import Selection, collect
from ai_usage_reporter.state import State

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _event(ts: str, tokens: TokenCounts, model: str = "m-1", session: str = "s1") -> UsageEvent:
    return UsageEvent(
        tool="claude-code",
        session_id=session,
        model=model,
        timestamp=datetime.fromisoformat(ts),
        tokens=tokens,
    )


def test_buckets_split_at_utc_midnight_and_by_model() -> None:
    events = [
        _event("2026-10-05T23:59:59+00:00", TokenCounts(input=1)),
        _event("2026-10-06T06:30:00+07:00", TokenCounts(input=2)),  # 23:30 UTC on the 5th
        _event("2026-10-06T07:00:00+07:00", TokenCounts(input=4)),  # 00:00 UTC on the 6th
        _event("2026-10-06T00:00:01+00:00", TokenCounts(input=8), model="m-2"),
    ]
    buckets = aggregate(events, Thresholds())
    assert [(str(b.usage_date), b.model, b.tokens.input, b.request_count) for b in buckets] == [
        ("2026-10-05", "m-1", 3, 2),
        ("2026-10-06", "m-1", 4, 1),
        ("2026-10-06", "m-2", 8, 1),
    ]
    assert buckets[0].started_at.isoformat() == "2026-10-05T23:30:00+00:00"


def test_long_context_requests_go_to_their_own_bucket_whole() -> None:
    thresholds = Thresholds({"m-*": 1000})
    events = [
        _event("2026-10-05T01:00:00+00:00", TokenCounts(input=10, cache_read=990, output=5)),  # exactly 1000: base
        _event("2026-10-05T01:01:00+00:00", TokenCounts(input=10, cache_read=991, output=7)),  # above: long
    ]
    [bucket] = aggregate(events, thresholds)
    assert bucket.tokens == TokenCounts(input=10, cache_read=990, output=5)
    assert bucket.long_tokens == TokenCounts(input=10, cache_read=991, output=7)
    assert Thresholds({"exact-model": 5})("Exact-Model") == 5
    assert Thresholds({"exact-model": 5})("other") is None


class FakeGit:
    def __init__(self, remote: str | None, email: str | None, commits: Sequence[str]) -> None:
        self.remote, self.email, self.commits = remote, email, commits
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], cwd: str) -> str | None:
        self.calls.append(list(args))
        if args[0] == "remote":
            return self.remote
        if args[0] == "config":
            return self.email
        return "\n".join(self.commits)


def _collect(home: Path, config: Config, **kwargs: object) -> list[dict[str, object]]:
    registry = Registry(adapters={"claude-code": ClaudeCodeAdapter()})
    collected = collect(
        config,
        registry,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )
    return [r.to_wire() for r in collected.records]


def test_enrichment_adds_hints_and_cost_rules(fixture_homes: dict[str, Path]) -> None:
    sha = "32fe3796ff2245cb43d05a41280745a53a12bd54"
    git = FakeGit("git@github.com:acme/app.git\n", "dev@example.com\n", [sha])
    config = Config(
        projects={"github.com/acme/*": "ACME"},
        cost_rules=(
            CostRule(tool="codex", cost_type="api"),
            CostRule(tool="claude-code", model="claude-haiku-*", cost_type="api"),
            CostRule(tool="claude-code", cost_type="subscription", subscription_hint="claude-max-20x"),
        ),
        on_behalf_of="teammate",
    )
    session = "11111111-2222-4111-8111-111111111111"
    records = _collect(
        fixture_homes["claude"].parent,
        config,
        git=GitInfo(runner=git),
        session_issues={session: "ACME-12"},
    )
    by_model = {(r["session_id"], r["model"], r["usage_date"]): r for r in records}
    resumed = by_model[(session, "claude-sonnet-4-5", "2026-10-07")]
    assert resumed["attribution"] == {
        "issue_ref": "ACME-12",
        "issue_ref_source": "env",
        "project_key": "ACME",
        "git_branch": "feat/acme-12-login",
        "repo": "github.com/acme/app",
        "commits": [sha],
    }
    assert resumed["cost_type"] == "subscription" and resumed["subscription_hint"] == "claude-max-20x"
    assert resumed["on_behalf_of"] == "teammate"
    haiku = next(r for r in records if r["model"] == "claude-haiku-4-5")
    assert haiku["cost_type"] == "api" and "subscription_hint" not in haiku
    log_call = next(c for c in git.calls if c[0] == "log")
    assert "--author=<dev@example.com>" in log_call


def test_no_commits_without_a_git_user(fixture_homes: dict[str, Path]) -> None:
    git = FakeGit("https://github.com/acme/app", None, ["32fe3796ff2245cb43d05a41280745a53a12bd54"])
    records = _collect(fixture_homes["claude"].parent, Config(), git=GitInfo(runner=git))
    assert all("commits" not in r.get("attribution", {}) for r in records)  # type: ignore[operator]


def test_excluded_paths_are_never_reported(fixture_homes: dict[str, Path]) -> None:
    everything = _collect(fixture_homes["claude"].parent, Config())
    assert everything
    excluded = _collect(fixture_homes["claude"].parent, Config(exclude_paths=("/work",)))
    assert excluded == []


def test_selection_by_session_and_since(fixture_homes: dict[str, Path]) -> None:
    home = fixture_homes["claude"].parent
    session = "11111111-2222-4111-8111-111111111111"
    only = _collect(home, Config(), selection=Selection(session=session))
    assert {r["session_id"] for r in only} == {session}
    from datetime import date

    recent = _collect(home, Config(), selection=Selection(since=date(2026, 10, 6)))
    assert min(str(r["usage_date"]) for r in recent) == "2026-10-06"


def test_old_buckets_are_left_out(fixture_homes: dict[str, Path]) -> None:
    registry = Registry(adapters={"claude-code": ClaudeCodeAdapter()})
    home = fixture_homes["claude"].parent
    collected = collect(
        Config(),
        registry,
        context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        now=datetime(2027, 12, 1, tzinfo=UTC),
    )
    assert collected.records == [] and collected.too_old > 0


def test_unchanged_files_are_not_parsed_again(fixture_homes: dict[str, Path]) -> None:
    home = fixture_homes["claude"].parent
    registry = Registry(adapters={"claude-code": ClaudeCodeAdapter()})

    def run(state: State) -> tuple[int, int]:
        c = collect(
            Config(),
            registry,
            state=state,
            now=NOW,
            context_for=lambda a, roots: DiscoveryContext(home=home, env={}, roots=roots),
        )
        report = c.reports["claude-code"]
        return report.parsed, report.reused

    with State.in_memory() as state:
        assert run(state) == (3, 0)
        assert run(state) == (0, 3)
        # A changed file is parsed again.
        transcript = next(fixture_homes["claude"].rglob("11111111-2222-*.jsonl"))
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "summary", "summary": "x"}) + "\n")
        assert run(state) == (1, 2)
        # A new parser version parses everything again.
        ClaudeCodeAdapter.parser_version = "claude-code/test"
        try:
            assert run(state) == (3, 0)
        finally:
            ClaudeCodeAdapter.parser_version = "claude-code/1"


def test_disabled_sources_are_skipped(fixture_homes: dict[str, Path]) -> None:
    registry = Registry(adapters={"claude-code": ClaudeCodeAdapter(), "codex": CodexAdapter()})
    config = Config(sources={"claude-code": SourceSettings(enabled=False)})
    collected = collect(
        config,
        registry,
        now=NOW,
        context_for=lambda a, roots: DiscoveryContext(home=fixture_homes["codex"].parent, env={}, roots=roots),
    )
    assert set(collected.reports) == {"codex"}
