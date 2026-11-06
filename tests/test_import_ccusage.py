"""``ai-usage import ccusage`` from synthetic ccusage reports."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter import cli
from ai_usage_reporter.config import Config, SourceSettings
from ai_usage_reporter.importers.ccusage import CcusageFormatError, import_report, tool_slug
from ai_usage_reporter.record import idempotency_key
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.testing import validate_records
from tests.conftest import FakeServer

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _breakdown(model: str, i: int, o: int, cw: int = 0, cr: int = 0) -> dict[str, Any]:
    return {"modelName": model, "inputTokens": i, "outputTokens": o, "cacheCreationTokens": cw,
            "cacheReadTokens": cr, "cost": 1.5}  # fmt: skip


REPORT: dict[str, Any] = {
    "daily": [
        {
            "date": "2026-11-01",
            "agent": "all",
            "inputTokens": 999,
            "agents": [
                {"agent": "claude", "modelBreakdowns": [_breakdown("claude-sonnet-4-5", 100, 20, 300, 4000)]},
                {"agent": "amp", "modelBreakdowns": [_breakdown("claude-opus-4-1", 50, 5), _breakdown("gpt-5", 7, 1)]},
                {"agent": "Cursor CLI", "modelBreakdowns": [], "inputTokens": 9, "outputTokens": 2},
                {"agent": "gemini", "modelBreakdowns": [_breakdown("gemini-2.5-pro", 30, 3)]},
            ],
        },
        {"date": "2026-11-02", "agents": [{"agent": "amp", "modelBreakdowns": [_breakdown("gpt-5", 1, 1)]}]},
        {"date": "2024-01-01", "agents": [{"agent": "amp", "modelBreakdowns": [_breakdown("gpt-5", 1, 1)]}]},
        {"date": "not a date"},
        "nonsense",
    ],
    "totals": {"inputTokens": 1},
}


def test_rows_become_records_and_native_tools_are_skipped() -> None:
    result = import_report(REPORT, Config(), ["claude-code", "codex", "gemini-cli", "opencode"], now=NOW)
    wires = [r.to_wire() for r in result.records]
    validate_records(wires)
    keys = {(w["tool"], w["model"], w["usage_date"]) for w in wires}
    assert keys == {
        ("amp", "claude-opus-4-1", "2026-11-01"),
        ("amp", "gpt-5", "2026-11-01"),
        ("cursor-cli", "unknown", "2026-11-01"),
        ("amp", "gpt-5", "2026-11-02"),
    }
    assert dict(result.skipped_tools) == {"claude-code": 1, "gemini-cli": 1}
    assert (result.rows, result.malformed_rows, result.too_old) == (5, 2, 1)
    amp = next(w for w in wires if w["model"] == "claude-opus-4-1")
    assert amp["tokens"] == {"input": 50, "output": 5}
    assert "session_id" not in amp and "reported_cost" not in amp and "request_count" not in amp
    assert amp["idempotency_key"] == idempotency_key(
        "amp", "import-ccusage", "claude-opus-4-1", NOW.date().replace(day=1)
    )


def test_via_ccusage_hands_a_tool_over_to_the_import() -> None:
    config = Config(sources={"claude-code": SourceSettings(via="ccusage")})
    result = import_report(REPORT, config, ["claude-code"], now=NOW)
    claude = next(r.to_wire() for r in result.records if r.tool == "claude-code")
    assert claude["tokens"] == {"input": 100, "output": 20, "cache_write_5m": 300, "cache_read": 4000}


def test_rows_without_agents_use_the_default_agent() -> None:
    report = {"daily": [{"date": "2026-11-03", "modelBreakdowns": [_breakdown("m", 1, 1)]}]}
    assert import_report(report, Config(), [], now=NOW).records[0].tool == "claude-code"
    assert import_report(report, Config(), [], now=NOW, default_agent="qwen").records[0].tool == "qwen"


def test_projects_layout_is_summed_per_day() -> None:
    row = {"date": "2026-11-03", "agents": [{"agent": "amp", "modelBreakdowns": [_breakdown("m", 2, 1)]}]}
    result = import_report({"projects": {"a": [row], "b": [row]}}, Config(), [], now=NOW)
    [record] = result.records
    assert record.tokens.input == 4


def test_not_a_report() -> None:
    with pytest.raises(CcusageFormatError):
        import_report({"weekly": []}, Config(), [], now=NOW)
    assert tool_slug("x") is None and tool_slug("Claude") == "claude-code"


def test_cli_import_sends_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_import_test")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.toml").write_text('url = "https://specivo.example.com"\n', encoding="utf-8")
    fake = FakeServer()
    monkeypatch.setattr(
        cli, "SpecivoSink", lambda url, key, **kw: SpecivoSink(url, key, transport=fake, sleep=lambda s: None, **kw)
    )
    report = tmp_path / "report.json"
    recent = json.loads(json.dumps(REPORT))
    today = datetime.now(UTC).date().isoformat()
    for row in recent["daily"][:2]:
        row["date"] = today
    report.write_text(json.dumps(recent), encoding="utf-8")
    assert cli.main(["import", "ccusage", str(report)]) == 0
    sent = sum(len(b) for b in fake.posts)
    assert sent == 3  # amp gpt-5 summed over both rows of today, amp opus, cursor-cli
    assert cli.main(["import", "ccusage", str(report)]) == 0
    assert sum(len(b) for b in fake.posts) == sent
    _, err = capsys.readouterr()
    assert "skipped rows of tools read from their own logs: claude-code 1, gemini-cli 1" in err

    monkeypatch.setattr("sys.stdin", io.StringIO(report.read_text(encoding="utf-8")))
    assert cli.main(["import", "ccusage", "-", "--dry-run"]) == 0
    out, _ = capsys.readouterr()
    assert len(json.loads(out)["records"]) == 3
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2"))
    assert cli.main(["import", "ccusage", "--dry-run"]) == 2
