from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from ai_usage_reporter import cli
from ai_usage_reporter import schedule as schedule_module
from ai_usage_reporter.sinks.specivo import SpecivoSink
from ai_usage_reporter.testing import validate_records
from tests.conftest import FakeServer, ok, result_item

CANARIES = ("CANARY-PROMPT-5c1e", "CANARY-TOOL-OUTPUT-9d07", "CANARY-PATH-7f3a", "CANARY-SECRET-b2e4")


@pytest.fixture
def env(tmp_path: Path, fixture_homes: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    dirs = {"config": tmp_path / "cfg", "state": tmp_path / "state"}
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(dirs["config"]))
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(dirs["state"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fixture_homes["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(fixture_homes["codex"]))
    monkeypatch.setenv("SPECIVO_API_KEY", "spv_cli_test")
    monkeypatch.delenv("SPECIVO_ISSUE", raising=False)
    dirs["config"].mkdir()
    (dirs["config"] / "config.toml").write_text('url = "https://specivo.example.com"\n', encoding="utf-8")
    return dirs


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()

    def make(url: str, key: str, **kwargs: Any) -> SpecivoSink:
        return SpecivoSink(url, key, transport=fake, sleep=lambda s: None, **kwargs)

    monkeypatch.setattr(cli, "SpecivoSink", make)
    return fake


def _no_canaries(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text


def test_dry_run_prints_valid_records_and_writes_nothing(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["push", "--dry-run", "--no-git"]) == 0
    out, err = capsys.readouterr()
    batch = json.loads(out)
    assert {r["tool"] for r in batch["records"]} == {"claude-code", "codex"}
    validate_records(batch["records"])
    _no_canaries(out + err)
    assert not env["state"].exists()


def test_push_status_and_second_push(
    env: dict[str, Path], server: FakeServer, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["push", "--no-git"]) == 0
    sent = sum(len(b) for b in server.posts)
    assert sent == 8
    assert cli.main(["push", "--no-git"]) == 0
    assert sum(len(b) for b in server.posts) == sent  # nothing changed, nothing sent
    assert cli.main(["status"]) == 0
    out, err = capsys.readouterr()
    assert "sent 8" in out
    _no_canaries(out + err)


def test_status_and_doctor_show_conflicts_and_rejections(
    env: dict[str, Path], server: FakeServer, capsys: pytest.CaptureFixture[str]
) -> None:
    def decide(i: int, r: dict[str, Any]) -> dict[str, Any]:
        if i == 0:
            return result_item(i, r["idempotency_key"], "rejected", error={"code": "key_conflict", "message": "taken"})
        if i == 1:
            return result_item(
                i, r["idempotency_key"], "rejected", error={"code": "attribution_conflict", "message": "elsewhere"}
            )
        return result_item(i, r["idempotency_key"])

    server.responders = [lambda records: ok(records, decide)]
    cli.main(["push", "--no-git", "--source", "claude-code"])
    capsys.readouterr()
    cli.main(["status"])
    out, _ = capsys.readouterr()
    assert "refused records (1)" in out and "key_conflict" in out
    assert "sent without issue_ref/project_key after an attribution_conflict" in out
    assert cli.main(["doctor"]) == 0
    out, err = capsys.readouterr()
    assert "key_conflict 1" in out and "API key: from the environment" in out
    assert "records that would be sent: 8" in out
    _no_canaries(out + err)


def test_session_issue_is_captured(env: dict[str, Path], server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    session = "11111111-2222-4111-8111-111111111111"
    monkeypatch.setenv("SPECIVO_ISSUE", "acme-12")
    assert cli.main(["push", "--no-git", "--session", session]) == 0
    assert {r["attribution"]["issue_ref"] for r in server.posts[0]} == {"ACME-12"}
    monkeypatch.delenv("SPECIVO_ISSUE")
    # Later pushes keep the captured issue for that session.
    assert cli.main(["show-payload", "--no-git", "--session", session]) == 0


def test_show_payload(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["show-payload", "--no-git", "--source", "codex", "--since", "2026-10-05"]) == 0
    out, _ = capsys.readouterr()
    records = json.loads(out)["records"]
    assert len(records) == 2 and all(r["usage_date"] == "2026-10-05" for r in records)


def test_init_stores_the_key_privately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO("spv_from_stdin\n"))
    code = cli.main(["init", "--url", "https://specivo.example.com", "--key-stdin", "--no-keyring", "--offline"])
    assert code == 0
    assert (tmp_path / "cfg" / "api-key").read_text().strip() == "spv_from_stdin"
    assert "spv_" not in (tmp_path / "cfg" / "config.toml").read_text()


def test_init_without_a_terminal_asks_for_key_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code = cli.main(["init", "--url", "https://specivo.example.com", "--no-keyring", "--offline"])
    _, err = capsys.readouterr()
    assert code != 0 and "--key-stdin" in err and "Traceback" not in err
    assert not (tmp_path / "cfg" / "api-key").exists()


def test_errors_are_short(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["push", "--source", "nope"]) == 2
    assert cli.main(["push", "--since", "yesterday"]) == 2
    _, err = capsys.readouterr()
    assert "unknown source" in err and "--since" in err


def test_hooks_install_and_uninstall_touch_only_the_temporary_home(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["hooks", "install", "--tool", "all"]) == 0
    assert (isolated_home / ".claude" / "settings.json").exists()
    assert (isolated_home / ".codex" / "config.toml").exists()
    assert (isolated_home / ".gemini" / "settings.json").exists()
    assert (isolated_home / "xdg_config_home" / "opencode" / "plugins" / "specivo-ai-usage-reporter.js").exists()
    assert cli.main(["hooks", "install", "--tool", "codex"]) == 0
    out, _ = capsys.readouterr()
    assert "codex: unchanged" in out
    assert cli.main(["hooks", "uninstall", "--tool", "all"]) == 0
    assert not (isolated_home / ".codex" / "config.toml").exists()


def test_hook_command_never_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "claude-code", "--bogus-option"]) == 0
    assert cli.main(["hook"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_schedule_runs_the_system_commands(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(schedule_module, "run_command", run)
    assert cli.main(["schedule", "install", "--kind", "systemd", "--every", "20"]) == 0
    assert calls[-1][-1] == "ai-usage-reporter.timer"
    timer = isolated_home / "xdg_config_home" / "systemd" / "user" / "ai-usage-reporter.timer"
    assert "OnUnitActiveSec=20min" in timer.read_text()
    assert cli.main(["schedule", "remove", "--kind", "systemd"]) == 0
    assert not timer.exists()
    assert cli.main(["schedule", "install", "--every", "2"]) == 2


def test_via_ccusage_keeps_push_away_from_the_tool(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    (env["config"] / "config.toml").write_text(
        'url = "https://specivo.example.com"\n[sources.codex]\nvia = "ccusage"\n', encoding="utf-8"
    )
    assert cli.main(["push", "--dry-run", "--no-git", "--source", "codex"]) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out)["records"] == []
    assert cli.main(["doctor", "--offline"]) in (0, 1)
    out, _ = capsys.readouterr()
    assert "instead of their logs: codex" in out
