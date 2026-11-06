from __future__ import annotations

from typing import Any

import pytest

from ai_usage_reporter.sinks.specivo import HttpResponse, SinkError, SpecivoSink, endpoint, retry_after_seconds
from tests.conftest import INFO, FakeServer, make_record, ok, result_item


def _sink(server: FakeServer, sleeps: list[float] | None = None) -> SpecivoSink:
    return SpecivoSink(
        "https://specivo.example.com",
        "spv_secret",
        transport=server,
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_endpoint_accepts_base_or_full_url() -> None:
    assert endpoint("https://h.example") == "https://h.example/api/v1/ai-usage/ingest"
    assert endpoint("https://h.example/api/v1/") == "https://h.example/api/v1/ai-usage/ingest"
    assert endpoint("https://h.example/api/v1/ai-usage/ingest/") == "https://h.example/api/v1/ai-usage/ingest"


def test_sends_a_batch_with_the_key_and_reports_each_record(server: FakeServer) -> None:
    records = [make_record(i).to_wire() for i in range(3)]
    outcomes = _sink(server).send(records)
    assert [o.status for o in outcomes] == ["created"] * 3
    assert server.gets == 1 and len(server.posts) == 1
    assert server.headers[-1]["Authorization"] == "Bearer spv_secret"
    assert "spv_secret" not in repr(_sink(server))


def test_splits_by_max_records_and_bytes(server: FakeServer) -> None:
    server.info = {**INFO, "max_records": 2}
    _sink(server).send([make_record(i).to_wire() for i in range(5)])
    assert [len(b) for b in server.posts] == [2, 2, 1]
    other = FakeServer(info={**INFO, "max_bytes": 900})
    _sink(other).send([make_record(i).to_wire() for i in range(5)])
    assert len(other.posts) > 1 and sum(len(b) for b in other.posts) == 5


def test_waits_for_retry_after_on_429(server: FakeServer) -> None:
    server.responders = [lambda records: HttpResponse(429, {"Retry-After": "7"}, b"{}")]
    sleeps: list[float] = []
    outcomes = _sink(server, sleeps).send([make_record().to_wire()])
    assert sleeps == [7.0]
    assert outcomes[0].status == "created"


def test_a_long_retry_after_leaves_records_pending(server: FakeServer) -> None:
    server.responders = [lambda records: HttpResponse(429, {"retry-after": "3600"}, b"{}")]
    server.info = {**INFO, "max_records": 1}
    outcomes = _sink(server).send([make_record(i).to_wire() for i in range(2)])
    assert [o.status for o in outcomes] == ["pending", "pending"]
    assert len(server.posts) == 1  # the second batch was not tried


def test_retry_after_http_date() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("Fri, 06 Nov 2026 12:00:30 GMT", now) == 30.0
    assert retry_after_seconds("soon", now) is None


def test_server_errors_are_retried_then_left_pending(server: FakeServer) -> None:
    server.responders = [lambda r: HttpResponse(503, {}, b"")] * 3
    sleeps: list[float] = []
    outcomes = _sink(server, sleeps).send([make_record().to_wire()])
    assert outcomes[0].status == "pending"
    assert sleeps == [1.0, 2.0]


def test_a_413_splits_the_batch(server: FakeServer) -> None:
    server.responders = [lambda r: HttpResponse(413, {}, b'{"errors":[{"code":"batch_too_large"}]}')]
    outcomes = _sink(server).send([make_record(i).to_wire() for i in range(4)])
    assert [len(b) for b in server.posts] == [4, 2, 2]
    assert all(o.status == "created" for o in outcomes)


def test_bad_key_stops_the_push(server: FakeServer) -> None:
    server.responders = [lambda r: HttpResponse(401, {}, b'{"errors":[{"code":"api_key_invalid"}]}')]
    with pytest.raises(SinkError, match="api_key_invalid"):
        _sink(server).send([make_record().to_wire()])


def test_unsupported_contract_version_stops_the_push(server: FakeServer) -> None:
    server.info = {**INFO, "versions": ["specivo.ai-usage.v2"]}
    with pytest.raises(SinkError, match="does not accept"):
        _sink(server).send([make_record().to_wire()])


def test_per_record_errors_and_unknown_codes(server: FakeServer) -> None:
    def decide(i: int, r: dict[str, Any]) -> dict[str, Any]:
        if i == 0:
            return result_item(i, r["idempotency_key"], warnings=[{"code": "attribution_dropped", "message": "m"}])
        if i == 1:
            return result_item(i, r["idempotency_key"], "rejected", error={"code": "key_conflict", "message": "m"})
        return result_item(i, r["idempotency_key"], "something_new", error={"code": "brand_new", "message": "x"})

    server.responders = [lambda records: ok(records, decide)]
    outcomes = _sink(server).send([make_record(i).to_wire() for i in range(3)])
    assert outcomes[0].accepted and outcomes[0].warnings == ["attribution_dropped"]
    assert (outcomes[1].status, outcomes[1].error_code) == ("rejected", "key_conflict")
    assert (outcomes[2].status, outcomes[2].error_code) == ("rejected", "brand_new")
