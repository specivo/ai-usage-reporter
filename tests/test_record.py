"""The privacy serializer and the vendored contract."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, date, datetime
from importlib.resources import files

import pytest

from ai_usage_reporter.model import TokenCounts, UsageEvent
from ai_usage_reporter.record import (
    Attribution,
    ContractRecord,
    RecordError,
    idempotency_key,
    normalise_repo,
    opaque_session_id,
)
from ai_usage_reporter.testing import contract_file, schema_validator, validate_records
from tests.conftest import make_record

EXAMPLES = files("ai_usage_reporter.contract").joinpath("v1", "examples")


def test_the_record_has_exactly_the_contract_fields() -> None:
    schema = contract_file("record.schema.json")
    assert {f.name for f in fields(ContractRecord)} == set(schema["properties"])
    assert set(Attribution.__dataclass_fields__) == set(schema["$defs"]["attribution"]["properties"])


def test_usage_events_cannot_reach_the_wire() -> None:
    event_fields = {f.name for f in fields(UsageEvent)}
    assert {"cwd", "source_path"} <= event_fields
    assert not {"cwd", "source_path"} & {f.name for f in fields(ContractRecord)}


def test_recommended_idempotency_key() -> None:
    # The contract's own example: SHA-256 of v1|tool|session|model|date.
    import hashlib

    expected = hashlib.sha256(b"v1|codex|s-1|gpt-5|2026-11-05").hexdigest()
    assert idempotency_key("codex", "s-1", "gpt-5", date(2026, 11, 5)) == expected


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("git@github.com:acme/app.git", "github.com/acme/app"),
        ("https://github.com/acme/app.git", "github.com/acme/app"),
        ("https://user:token@GitLab.example.com/group/sub/app", "gitlab.example.com/group/sub/app"),
        ("ssh://git@git.example.com:2222/acme/app.git", "git.example.com/acme/app"),
        ("/srv/git/app.git", None),
        ("file:///srv/git/app.git", None),
        ("C:\\repos\\app", None),
        ("../app", None),
        ("", None),
        (None, None),
    ],
)
def test_repository_hints_never_carry_paths_or_credentials(url: str | None, hint: str | None) -> None:
    assert normalise_repo(url) == hint


def test_opaque_session_ids() -> None:
    assert opaque_session_id("0f6c2a1e-1234-4abc-9def-000000000000") == "0f6c2a1e-1234-4abc-9def-000000000000"
    hashed = opaque_session_id("/home/someone/project/session")
    assert hashed.startswith("h-") and "/" not in hashed


def test_wire_leaves_out_values_that_do_not_fit() -> None:
    record = make_record(
        tool_version="x" * 51,
        cost_type="bogus",
        attribution=Attribution(
            issue_ref="not an issue",
            project_key="ACME",
            git_branch="bad\nbranch",
            repo="/local/path",
            commits=("32FE3796FF2245CB43D05A41280745A53A12BD54", "abc"),
        ),
    )
    wire = record.to_wire()
    assert "tool_version" not in wire and "cost_type" not in wire
    assert wire["attribution"] == {"project_key": "ACME", "commits": ["32fe3796ff2245cb43d05a41280745a53a12bd54"]}
    validate_records([wire])


def test_required_values_must_fit() -> None:
    with pytest.raises(RecordError):
        make_record(tool="Claude Code").to_wire()
    with pytest.raises(RecordError):
        make_record(tokens=TokenCounts(input=-1)).to_wire()


def test_timestamps_are_utc_with_an_offset() -> None:
    from zoneinfo import ZoneInfo

    bangkok = datetime(2026, 10, 5, 9, 30, 0, 250000, tzinfo=ZoneInfo("Asia/Bangkok"))
    wire = make_record(started_at=bangkok, ended_at=bangkok).to_wire()
    assert wire["started_at"] == "2026-10-05T02:30:00.25Z"
    validate_records([wire])


def test_full_record_validates() -> None:
    record = make_record(
        long_context_tokens=TokenCounts(input=300_000, output=10),
        cost_type="subscription",
        subscription_hint="claude-max-20x",
        on_behalf_of="teammate",
        provider="anthropic",
        timezone="Asia/Bangkok",
        attribution=Attribution(
            issue_ref="ACME-12", issue_ref_source="env", git_branch="feat/acme-12-login", repo="github.com/acme/app"
        ),
    )
    validate_records([record.to_wire()])


def test_vendored_examples_match_the_schema() -> None:
    expected = json.loads(EXAMPLES.joinpath("expected.json").read_text("utf-8"))
    batch = schema_validator("batch.schema.json")
    for name in expected["valid"]:
        batch.validate(json.loads(EXAMPLES.joinpath("valid", name).read_text("utf-8")))
    for name, answer in expected["invalid"].items():
        body = json.loads(EXAMPLES.joinpath("invalid", name).read_text("utf-8"))
        assert batch.is_valid(body) == answer["schema_valid"]


def test_record_dates_are_utc_days() -> None:
    record = make_record(day=date(2026, 10, 5), started_at=datetime(2026, 10, 5, 23, 59, tzinfo=UTC))
    assert record.to_wire()["usage_date"] == "2026-10-05"


def test_long_context_and_base_tokens_are_bounded_together() -> None:
    from ai_usage_reporter.record import MAX_COUNT, RecordError

    near = TokenCounts(input=MAX_COUNT - 10)
    ok_record = make_record(tokens=near, long_context_tokens=TokenCounts(input=10))
    assert ok_record.to_wire()["long_context_tokens"] == {"input": 10}
    with pytest.raises(RecordError):
        make_record(tokens=near, long_context_tokens=TokenCounts(input=11)).to_wire()
