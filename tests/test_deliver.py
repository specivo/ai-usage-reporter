"""The ledger: what is sent again, what is final, the retry queue and attribution conflicts."""

from __future__ import annotations

from typing import Any

from ai_usage_reporter.model import TokenCounts
from ai_usage_reporter.pipeline import deliver, strip_explicit
from ai_usage_reporter.record import Attribution
from ai_usage_reporter.sinks.json_sink import JsonSink
from ai_usage_reporter.sinks.specivo import HttpResponse, SpecivoSink
from ai_usage_reporter.state import CONFLICT, PENDING, REJECTED, SENT, State
from tests.conftest import FakeServer, make_record, ok, result_item


def _sink(server: FakeServer) -> SpecivoSink:
    return SpecivoSink("https://specivo.example.com", "spv_x", transport=server, sleep=lambda s: None)


def test_only_changed_buckets_are_sent_again(server: FakeServer) -> None:
    with State.in_memory() as state:
        records = [make_record(i) for i in range(3)]
        assert deliver(records, _sink(server), state).sent == 3
        changed = [records[0], records[1], make_record(2, tokens=TokenCounts(input=999))]
        summary = deliver(changed, _sink(server), state)
        assert summary.sent == 1 and summary.skipped_unchanged == 2
        assert server.posts[-1][0]["tokens"]["input"] == 999


def test_final_rejections_are_not_sent_again(server: FakeServer) -> None:
    def reject(records: list[dict[str, Any]]) -> HttpResponse:
        return ok(
            records,
            lambda i, r: result_item(
                i, r["idempotency_key"], "rejected", error={"code": "key_conflict", "message": "taken"}
            ),
        )

    server.responders = [reject]
    with State.in_memory() as state:
        record = make_record()
        deliver([record], _sink(server), state)
        row = state.bucket(record.idempotency_key)
        assert row is not None and (row.status, row.error_code) == (REJECTED, "key_conflict")
        changed = make_record(tokens=TokenCounts(input=5))
        assert deliver([changed], _sink(server), state).skipped_final == 1
        assert len(server.posts) == 1
        assert deliver([changed], _sink(server), state, retry_rejected=True).sent == 1


def test_forbidden_is_final_and_counted(server: FakeServer) -> None:
    server.responders = [
        lambda records: ok(
            records,
            lambda i, r: result_item(i, r["idempotency_key"], "rejected", error={"code": "forbidden", "message": "no"}),
        )
    ]
    with State.in_memory() as state:
        summary = deliver([make_record(on_behalf_of="teammate")], _sink(server), state)
        assert summary.errors["forbidden"] == 1
        assert state.error_counts() == {"forbidden": 1}


def test_attribution_conflict_is_resent_once_without_explicit_hints(server: FakeServer) -> None:
    attribution = Attribution(issue_ref="ACME-12", issue_ref_source="env", project_key="ACME", git_branch="main")
    record = make_record(attribution=attribution)

    def conflict(records: list[dict[str, Any]]) -> HttpResponse:
        return ok(
            records,
            lambda i, r: result_item(
                i, r["idempotency_key"], "rejected", error={"code": "attribution_conflict", "message": "m"}
            ),
        )

    server.responders = [conflict]
    with State.in_memory() as state:
        summary = deliver([record], _sink(server), state)
        assert summary.conflicts_resent == 1
        assert len(server.posts) == 2
        assert server.posts[1][0]["attribution"] == {"git_branch": "main"}
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == SENT and row.drop_explicit
        assert "attribution_conflict" in row.warnings
        # The next push builds the stripped record and finds nothing changed: no loop.
        stripped = make_record(attribution=attribution.without_explicit())
        assert deliver([stripped], _sink(server), state).sent == 0


def test_a_second_conflict_is_recorded_not_retried(server: FakeServer) -> None:
    def conflict(records: list[dict[str, Any]]) -> HttpResponse:
        return ok(
            records,
            lambda i, r: result_item(
                i, r["idempotency_key"], "rejected", error={"code": "attribution_conflict", "message": "m"}
            ),
        )

    server.responders = [conflict, conflict]
    record = make_record(attribution=Attribution(project_key="ACME"))
    with State.in_memory() as state:
        deliver([record], _sink(server), state)
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == CONFLICT
        assert len(server.posts) == 2


def test_pending_records_are_retried_from_the_stored_payload(server: FakeServer) -> None:
    server.responders = [lambda records: HttpResponse(503, {}, b"")] * 3
    with State.in_memory() as state:
        record = make_record()
        deliver([record], _sink(server), state)
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == PENDING
        # The logs are gone: nothing is collected, the queue is still sent.
        summary = deliver([], _sink(server), state)
        assert summary.retried_from_queue == 1
        assert server.posts[-1][0] == record.to_wire()
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == SENT


def test_internal_error_is_retryable(server: FakeServer) -> None:
    server.responders = [
        lambda records: ok(
            records,
            lambda i, r: result_item(
                i, r["idempotency_key"], "rejected", error={"code": "internal_error", "message": "x"}
            ),
        )
    ]
    with State.in_memory() as state:
        record = make_record()
        deliver([record], _sink(server), state)
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == PENDING


def test_json_sink_does_not_touch_the_ledger() -> None:
    import io

    with State.in_memory() as state:
        out = io.StringIO()
        deliver([make_record()], JsonSink(stream=out), state)
        assert state.status_counts() == {}
        assert '"specivo.ai-usage.v1"' in out.getvalue()


def test_strip_explicit_keeps_inferred_hints() -> None:
    wire = {"attribution": {"issue_ref": "A-1", "issue_ref_source": "env", "project_key": "A", "repo": "h/o/r"}}
    assert strip_explicit(wire) == {"attribution": {"repo": "h/o/r"}}
    assert strip_explicit({"attribution": {"project_key": "A"}}) == {}


AGENT_LOG = Attribution(issue_ref="ACME-12", issue_ref_source="agent_log", project_key="ACME", git_branch="main")


def _refuse_agent_log(records: list[dict[str, Any]]) -> HttpResponse:
    """An older server: issue_ref_source "agent_log" breaks its schema."""

    def decide(i: int, r: dict[str, Any]) -> dict[str, Any]:
        if (r.get("attribution") or {}).get("issue_ref_source") == "agent_log":
            error = {"code": "invalid_record", "message": "not a valid value", "field": "attribution.issue_ref_source"}
            return result_item(i, r["idempotency_key"], "rejected", error=error)
        return result_item(i, r["idempotency_key"])

    return ok(records, decide)


def test_an_older_server_gets_the_record_again_without_the_agent_log_issue(server: FakeServer) -> None:
    server.default = _refuse_agent_log
    records = [make_record(0, attribution=AGENT_LOG), make_record(1, attribution=AGENT_LOG.without_explicit())]
    with State.in_memory() as state:
        summary = deliver(records, _sink(server), state)
        assert summary.agent_log_resent == 1 and summary.warnings["agent_log_dropped"] == 1
        assert not summary.errors
        assert len(server.posts) == 2
        stripped = {**records[0].to_wire(), "attribution": {"project_key": "ACME", "git_branch": "main"}}
        assert server.posts[1] == [stripped]
        assert all((row := state.bucket(r.idempotency_key)) and row.status == SENT for r in records)
        # Unchanged buckets are not sent again, and while the refusal is remembered, a changed one
        # goes without the issue at once: one request, no rejection.
        assert deliver(records, _sink(server), state).sent == 0
        changed = make_record(0, attribution=AGENT_LOG, tokens=TokenCounts(input=5000))
        summary = deliver([changed], _sink(server), state)
        assert summary.sent == 1 and summary.agent_log_left_out == 1 and summary.agent_log_resent == 0
        assert "issue_ref" not in server.posts[-1][0]["attribution"]
        assert len(server.posts) == 3


def test_an_unusable_agent_log_issue_rejected_by_an_older_server_is_dropped(server: FakeServer) -> None:
    def not_found(records: list[dict[str, Any]]) -> HttpResponse:
        error = {"code": "not_found", "message": "no such issue", "field": "attribution.issue_ref"}
        return ok(records, lambda i, r: result_item(i, r["idempotency_key"], "rejected", error=error))

    server.responders = [not_found]
    record = make_record(attribution=AGENT_LOG)
    with State.in_memory() as state:
        summary = deliver([record], _sink(server), state)
        assert summary.agent_log_resent == 1 and summary.statuses["created"] == 1
        row = state.bucket(record.idempotency_key)
        assert row is not None and row.status == SENT and "issue_ref" not in row.payload["attribution"]
        # Not a refusal of the value itself: the next changed bucket still tries its issue.
        changed = make_record(attribution=AGENT_LOG, tokens=TokenCounts(input=5000))
        deliver([changed], _sink(server), state)
        assert server.posts[-1][0]["attribution"]["issue_ref_source"] == "agent_log"


def test_a_server_that_lists_its_issue_ref_sources_decides_up_front(server: FakeServer) -> None:
    record = make_record(attribution=AGENT_LOG)
    server.info["issue_ref_sources"] = ["explicit", "env"]
    with State.in_memory() as state:
        summary = deliver([record], _sink(server), state)
        assert summary.agent_log_left_out == 1 and len(server.posts) == 1
        assert "issue_ref" not in server.posts[0][0]["attribution"]
    server.info["issue_ref_sources"] = ["explicit", "env", "agent_log"]
    with State.in_memory() as state:
        deliver([record], _sink(server), state)
        assert server.posts[-1][0]["attribution"]["issue_ref_source"] == "agent_log"


def test_a_refusal_is_forgotten_after_a_while(server: FakeServer) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from ai_usage_reporter.pipeline import AGENT_LOG_META

    record = make_record(attribution=AGENT_LOG)
    with State.in_memory() as state:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        state.set_meta(AGENT_LOG_META, json.dumps({"url": _sink(server).url, "until": past}))
        deliver([record], _sink(server), state)
        assert server.posts[-1][0]["attribution"]["issue_ref_source"] == "agent_log"
