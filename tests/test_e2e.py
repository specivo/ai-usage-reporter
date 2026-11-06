"""End to end against a real Specivo server. Skipped unless configured.

Use a disposable test instance, never production: the test creates AI usage
entries with fixed, made-up sessions on the account of the key.

    SPECIVO_E2E_URL=https://specivo-test.example.com \\
    SPECIVO_E2E_API_KEY=spv_... \\
    uv run pytest -m e2e tests/test_e2e.py

The account needs no permission when the records carry no project.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ai_usage_reporter.model import TokenCounts
from ai_usage_reporter.pipeline import deliver
from ai_usage_reporter.record import Attribution, ContractRecord, idempotency_key
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.state import SENT, State

URL = os.environ.get("SPECIVO_E2E_URL")
KEY = os.environ.get("SPECIVO_E2E_API_KEY")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (URL and KEY), reason="set SPECIVO_E2E_URL and SPECIVO_E2E_API_KEY"),
]


def _record(session: str, output: int) -> ContractRecord:
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    if started.date() != (started + timedelta(minutes=20)).date():
        started -= timedelta(hours=1)
    return ContractRecord(
        idempotency_key=idempotency_key("claude-code", session, "claude-sonnet-4-5", started.date()),
        tool="claude-code",
        model="claude-sonnet-4-5",
        usage_date=started.date(),
        tokens=TokenCounts(input=100, output=output, cache_read=1000),
        provider="anthropic",
        session_id=session,
        started_at=started,
        ended_at=started + timedelta(minutes=20),
        request_count=3,
        attribution=Attribution(git_branch="e2e/ai-usage-reporter"),
    )


def test_push_update_and_unchanged() -> None:
    assert URL and KEY
    sink = SpecivoSink(URL, KEY)
    assert "specivo.ai-usage.v1" in sink.info().versions
    session = f"e2e-{uuid.uuid4()}"
    with State.in_memory() as state:
        first = deliver([_record(session, 50)], sink, state)
        assert first.statuses == {"created": 1}
        again = deliver([_record(session, 50)], sink, state, include_queue=False)
        assert again.sent == 0  # the ledger knows it is unchanged
        updated = deliver([_record(session, 80)], sink, state)
        assert updated.statuses == {"updated": 1}
        row = state.bucket(_record(session, 80).idempotency_key)
        assert row is not None and row.status == SENT and row.entry_id is not None
    # Without the ledger the server itself answers "unchanged".
    with State.in_memory() as fresh:
        assert deliver([_record(session, 80)], sink, fresh).statuses == {"unchanged": 1}
