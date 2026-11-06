from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter.model import TokenCounts
from ai_usage_reporter.record import ContractRecord, idempotency_key
from ai_usage_reporter.sinks.specivo import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"

INFO = {
    "versions": ["specivo.ai-usage.v1"],
    "max_records": 500,
    "max_bytes": 1048576,
    "max_age_days": 400,
    "rate_limit": {"requests": 60, "window_seconds": 60},
    "server_time": "2026-11-06T12:00:00Z",
    "tools": ["claude-code", "codex"],
    "currencies": ["EUR", "THB", "USD"],
}


def result_item(index: int, key: str, status: str = "created", **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "index": index,
        "idempotency_key": key,
        "status": status,
        "entry_id": None if status == "rejected" else 100 + index,
        "revision": None if status == "rejected" else 1,
        "attribution": None if status == "rejected" else {"method": "none", "project_key": None, "issue_ref": None},
        "warnings": [],
        "error": None,
    }
    item.update(extra)
    return item


Responder = Callable[[list[dict[str, Any]]], HttpResponse]


def ok(
    records: list[dict[str, Any]], decide: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None
) -> HttpResponse:
    results = [decide(i, r) if decide else result_item(i, r["idempotency_key"]) for i, r in enumerate(records)]
    body = {
        "schema": "specivo.ai-usage.v1",
        "server_time": "2026-11-06T12:00:00Z",
        "summary": {"received": len(records), "created": 0, "updated": 0, "unchanged": 0, "rejected": 0},
        "results": results,
        "warnings": [],
    }
    return HttpResponse(200, {}, json.dumps(body).encode())


@dataclass
class FakeServer:
    """A scripted Specivo ingest endpoint for the sink tests."""

    responders: list[Responder] = field(default_factory=list)
    default: Responder = ok
    info: dict[str, Any] = field(
        default_factory=lambda: {**INFO, "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
    )
    posts: list[list[dict[str, Any]]] = field(default_factory=list)
    headers: list[Mapping[str, str]] = field(default_factory=list)
    gets: int = 0

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        self.headers.append(headers)
        if method == "GET":
            self.gets += 1
            return HttpResponse(200, {}, json.dumps(self.info).encode())
        assert body is not None
        batch = json.loads(body)
        self.posts.append(batch["records"])
        responder = self.responders.pop(0) if self.responders else self.default
        return responder(batch["records"])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test ever reads the real home directory: the tools' logs, configs and data live there."""
    home = tmp_path_factory.mktemp("home")
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(home / name.lower()))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DB", "SPECIVO_ISSUE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def make_record(n: int = 0, *, day: date = date(2026, 10, 5), **extra: Any) -> ContractRecord:
    session = f"session-{n}"
    values: dict[str, Any] = {
        "idempotency_key": idempotency_key("claude-code", session, "claude-sonnet-4-5", day),
        "tool": "claude-code",
        "model": "claude-sonnet-4-5",
        "usage_date": day,
        "tokens": TokenCounts(input=100 + n, output=10),
        "session_id": session,
        "started_at": datetime(day.year, day.month, day.day, 1, 0, tzinfo=UTC),
        "ended_at": datetime(day.year, day.month, day.day, 2, 0, tzinfo=UTC),
        "request_count": 1,
    }
    values.update(extra)
    return ContractRecord(**values)


@pytest.fixture
def fixture_homes(tmp_path: Path) -> Iterator[dict[str, Path]]:
    claude = tmp_path / "claude-home"
    codex = tmp_path / "codex-home"
    shutil.copytree(FIXTURES / "claude_code" / "home", claude)
    shutil.copytree(FIXTURES / "codex" / "home", codex)
    yield {"claude": claude / ".claude", "codex": codex / ".codex"}
