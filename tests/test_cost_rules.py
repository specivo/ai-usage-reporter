"""``[[cost_rules]]``: the route and cwd filters and the billing_account output."""

from __future__ import annotations

import os
import tomllib
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ai_usage_reporter.aggregate import Bucket
from ai_usage_reporter.config import Config, ConfigError, CostRule, cwd_matches, parse_config, render_template
from ai_usage_reporter.enrich import Enricher
from ai_usage_reporter.importers.ccusage import import_report
from ai_usage_reporter.model import TokenCounts
from ai_usage_reporter.record import OPTIONAL_FIELDS, gate_optional_fields

WHEN = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CASE_INSENSITIVE_PATHS = os.path.normcase("A") == os.path.normcase("a")


def _bucket(
    tool: str = "codex",
    *,
    route: str | None = None,
    cwds: dict[str, int] | None = None,
    cost_type: str | None = None,
) -> Bucket:
    return Bucket(
        tool=tool,
        session_id="00000000-0000-4000-8000-000000000001",
        model="gpt-5",
        usage_date=date(2026, 11, 2),
        started_at=WHEN,
        ended_at=WHEN,
        request_count=1,
        tokens=TokenCounts(input=10, output=2),
        cwds=Counter(cwds or {}),
        route=route,
        cost_type=cost_type,
    )


def _rules(text: str) -> Config:
    return parse_config(tomllib.loads(text))


# --- parsing ---------------------------------------------------------------------------------------


def test_route_cwd_and_billing_account_are_read() -> None:
    config = _rules(
        """
[[cost_rules]]
tool = "codex"
route = "corp-*"
cwd = "/srv/work/client-a"
billing_account = "corp-gateway"
"""
    )
    [rule] = config.cost_rules
    assert (rule.tool, rule.route, rule.billing_account) == ("codex", "corp-*", "corp-gateway")
    assert rule.cwd == str(Path("/srv/work/client-a"))
    assert rule.cost_type is None and rule.subscription_hint is None


def test_a_cwd_pattern_expands_the_home_folder() -> None:
    [rule] = _rules('[[cost_rules]]\ncwd = "~/work/*"\nbilling_account = "acme"').cost_rules
    assert rule.cwd == str(Path("~/work/*").expanduser())
    assert not rule.cwd.startswith("~")


@pytest.mark.parametrize(
    "account",
    ["Corp-Gateway", "-corp", ".corp", "corp gateway", "corp/gateway", "a" * 65, "ümlaut"],
)
def test_a_bad_billing_account_slug_is_refused_with_a_clear_error(account: str) -> None:
    with pytest.raises(ConfigError, match=r"cost_rules #1: billing_account .* is not a billing account slug"):
        _rules(f'[[cost_rules]]\ntool = "codex"\nbilling_account = "{account}"')


@pytest.mark.parametrize("account", ["corp-gateway", "a", "9", "acme.eu_2", "a" * 64])
def test_good_billing_account_slugs_are_accepted(account: str) -> None:
    [rule] = _rules(f'[[cost_rules]]\nbilling_account = "{account}"').cost_rules
    assert rule.billing_account == account


@pytest.mark.parametrize(
    "text",
    [
        '[[cost_rules]]\nroute = ""',
        "[[cost_rules]]\nroute = 1",
        '[[cost_rules]]\ncwd = " "',
        '[[cost_rules]]\nbilling_account = ""',
        '[[cost_rules]]\npayer = "corp"',
    ],
)
def test_bad_route_cwd_or_account_values_are_refused(text: str) -> None:
    with pytest.raises(ConfigError):
        _rules(text)


def test_the_template_shows_the_gateway_rule_and_still_parses() -> None:
    text = render_template("https://specivo.example.com")
    assert parse_config(tomllib.loads(text)).cost_rules == ()
    lines = text.splitlines()
    start = lines.index("# [[cost_rules]]")
    example = [line.removeprefix("# ") for line in lines[start : start + 4]]
    [rule] = parse_config(tomllib.loads("\n".join(example))).cost_rules
    assert (rule.tool, rule.route, rule.billing_account) == ("codex", "corp-*", "corp-gateway")


# --- matching --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "route", "expected"),
    [
        ("corp-*", "corp-gateway", True),
        ("corp-*", "CORP-Gateway", True),  # case-insensitive
        ("CORP-*", "corp-gateway", True),
        ("corp-*", "openai", False),
        ("corp-*", None, False),  # no route never matches a rule that names one
        ("llm.example.com", "llm.example.com", True),
        ("llm.example.com", "llm.example.com.evil", False),  # a glob, not a prefix
        ("*.example.com", "llm.example.com", True),
        ("claude.ai:org-*", "claude.ai:org-1a2b3c4d", True),
    ],
)
def test_route_is_a_case_insensitive_glob(pattern: str, route: str | None, expected: bool) -> None:
    assert CostRule(route=pattern).matches("codex", "gpt-5", None, route=route) is expected


@pytest.mark.parametrize(
    ("pattern", "cwd", "expected"),
    [
        ("/srv/work/client-a", "/srv/work/client-a", True),
        ("/srv/work/client-a", "/srv/work/client-a/app/src", True),  # everything under the folder
        ("/srv/work/client-a/", "/srv/work/client-a/app", True),  # a trailing separator is ignored
        ("/srv/work/client-a", "/srv/work/client-ab", False),  # whole folder names, not a text prefix
        ("/srv/work/client-a", "/srv/work", False),
        ("/srv/work/client-*", "/srv/work/client-b/app", True),
        ("/srv/work/*/app", "/srv/work/client-b/app/src", True),
        ("*/client-a", "/srv/work/client-a/app", True),
        ("/srv/other", "/srv/work/client-a", False),
    ],
)
def test_cwd_is_a_glob_on_the_folder_or_a_folder_above_it(pattern: str, cwd: str, expected: bool) -> None:
    pattern, cwd = str(Path(pattern)) + ("/" if pattern.endswith("/") else ""), str(Path(cwd))
    assert cwd_matches(cwd, pattern) is expected
    assert CostRule(cwd=pattern).matches("codex", "gpt-5", None, cwd=cwd) is expected


def test_cwd_case_follows_the_platform() -> None:
    matched = cwd_matches(str(Path("/srv/Work/App")), str(Path("/srv/work")))
    assert matched is CASE_INSENSITIVE_PATHS


def test_a_rule_naming_a_cwd_never_matches_without_one() -> None:
    assert CostRule(cwd="/srv/work").matches("codex", "gpt-5", None, cwd=None) is False


def test_all_filters_must_match_and_the_first_rule_wins() -> None:
    config = Config(
        cost_rules=(
            CostRule(tool="codex", route="corp-*", billing_account="corp-gateway", cost_type="api"),
            CostRule(cwd=str(Path("/srv/work/client-a")), billing_account="client-a"),
            CostRule(tool="codex", cost_type="subscription"),
        )
    )
    corp = config.cost_for("codex", "gpt-5", None, route="corp-gateway", cwd=str(Path("/srv/work/client-a")))
    assert corp is not None and corp.billing_account == "corp-gateway"
    other_tool = config.cost_for("opencode", "m", None, route="corp-gateway", cwd=str(Path("/srv/work/client-a")))
    assert other_tool is not None and other_tool.billing_account == "client-a"
    plain = config.cost_for("codex", "gpt-5", None, route="openai")
    assert plain is not None and plain.cost_type == "subscription" and plain.billing_account is None


def test_existing_calls_without_route_or_cwd_still_work() -> None:
    config = Config(cost_rules=(CostRule(tool="codex", cost_type="api"),))
    rule = config.cost_for("codex", "gpt-5", None)
    assert rule is not None and rule.cost_type == "api"


# --- records ---------------------------------------------------------------------------------------


def test_a_route_rule_sets_the_billing_account_of_the_record() -> None:
    config = _rules(
        """
[[cost_rules]]
tool = "codex"
route = "corp-*"
billing_account = "corp-gateway"
cost_type = "api"

[[cost_rules]]
tool = "codex"
cost_type = "subscription"
"""
    )
    enricher = Enricher(config)
    gateway = enricher.record(_bucket(route="corp-gateway")).to_wire()
    assert gateway["billing_account"] == "corp-gateway" and gateway["cost_type"] == "api"
    assert gateway["route"] == "corp-gateway"
    own = enricher.record(_bucket(route="openai")).to_wire()
    assert "billing_account" not in own and own["cost_type"] == "subscription"


def test_a_rule_with_only_a_billing_account_keeps_the_tools_cost_type() -> None:
    config = Config(cost_rules=(CostRule(route="corp-*", billing_account="corp-gateway"),))
    record = Enricher(config).record(_bucket(route="corp-gateway", cost_type="subscription"))
    assert record.billing_account == "corp-gateway" and record.cost_type == "subscription"


def test_a_bucket_from_several_folders_is_matched_by_its_main_folder() -> None:
    client_a, client_b = str(Path("/srv/work/client-a/app")), str(Path("/srv/work/client-b"))
    config = Config(
        cost_rules=(
            CostRule(cwd=str(Path("/srv/work/client-b")), billing_account="client-b"),
            CostRule(cwd=str(Path("/srv/work/client-a")), billing_account="client-a"),
        )
    )
    enricher = Enricher(config)
    # Most requests ran in client-a, so the bucket is client-a's although client-b's rule comes first.
    assert enricher.record(_bucket(cwds={client_a: 3, client_b: 1})).billing_account == "client-a"
    assert enricher.record(_bucket(cwds={client_a: 1, client_b: 3})).billing_account == "client-b"
    # A tie goes to the first folder in sort order, the same one the repository hint comes from.
    assert enricher.record(_bucket(cwds={client_a: 2, client_b: 2})).billing_account == "client-a"


def test_the_working_folder_never_reaches_the_wire() -> None:
    folder = str(Path("/srv/work/client-a"))
    config = Config(cost_rules=(CostRule(cwd=folder, billing_account="client-a"),))
    wire = Enricher(config).record(_bucket(cwds={folder: 1})).to_wire()
    assert wire["billing_account"] == "client-a"
    assert "client-a" not in repr({k: v for k, v in wire.items() if k != "billing_account"})
    assert "/srv/work" not in repr(wire)


def test_the_billing_account_is_gated_by_the_servers_optional_fields() -> None:
    config = Config(cost_rules=(CostRule(route="corp-*", billing_account="corp-gateway"),))
    wire = Enricher(config).record(_bucket(route="corp-gateway")).to_wire()
    assert "billing_account" not in gate_optional_fields(wire, set())
    assert gate_optional_fields(wire, set(OPTIONAL_FIELDS))["billing_account"] == "corp-gateway"


def test_ccusage_imports_get_a_billing_account_but_never_match_route_or_cwd() -> None:
    report = {"daily": [{"date": "2026-11-01", "agents": [{"agent": "amp", "modelBreakdowns": [
        {"modelName": "m", "inputTokens": 1, "outputTokens": 1}]}]}]}  # fmt: skip
    now = datetime(2026, 11, 6, tzinfo=UTC)
    by_route = Config(cost_rules=(CostRule(route="*", billing_account="never"), CostRule(cwd="*", billing_account="x")))
    [record] = import_report(report, by_route, [], now=now).records
    assert record.billing_account is None
    by_tool = Config(cost_rules=(CostRule(tool="amp", billing_account="team-amp"),))
    [record] = import_report(report, by_tool, [], now=now).records
    assert record.billing_account == "team-amp"
