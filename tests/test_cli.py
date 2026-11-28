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
from tests.conftest import FakeServer, ok, result_item, usage_batch

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
    batch = usage_batch(out)
    assert {r["tool"] for r in batch["records"]} == {"claude-code", "codex"}
    validate_records(batch["records"])
    _no_canaries(out + err)
    assert not env["state"].exists()


def test_push_status_and_second_push(
    env: dict[str, Path], server: FakeServer, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["push", "--no-git"]) == 0
    sent = sum(len(b) for b in server.posts)
    assert sent == 10
    assert cli.main(["push", "--no-git"]) == 0
    assert sum(len(b) for b in server.posts) == sent  # nothing changed, nothing sent
    assert cli.main(["status"]) == 0
    out, err = capsys.readouterr()
    assert "sent 10" in out
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
    # Only claude-code was pushed: its records went (one refused for good); codex's 5 are still to send.
    assert "records collected: 10; a push would send 5 (4 unchanged since the last push, 1 refused earlier" in out
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
    records = usage_batch(out)["records"]
    assert sorted(r["usage_date"] for r in records) == ["2026-10-05", "2026-10-05", "2026-10-06", "2026-10-06"]


def test_show_payload_and_dry_run_list_only_what_a_push_would_send(
    env: dict[str, Path], server: FakeServer, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert len(usage_batch(out)["records"]) == 10
    assert "10 records would be sent" in err

    assert cli.main(["push", "--no-git"]) == 0
    capsys.readouterr()
    # Everything went: nothing is left to send, though every record is still collected.
    assert cli.main(["show-payload", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert usage_batch(out)["records"] == []
    assert "0 records would be sent (0 from the queue); 10 unchanged since the last push" in err
    assert cli.main(["push", "--dry-run", "--no-git"]) == 0
    out, err = capsys.readouterr()
    assert usage_batch(out)["records"] == [] and "10 unchanged since the last push" in err

    assert cli.main(["show-payload", "--no-git", "--all"]) == 0
    out, err = capsys.readouterr()
    assert len(usage_batch(out)["records"]) == 10 and "--all" in err
    assert cli.main(["doctor", "--offline"]) == 0
    out, _ = capsys.readouterr()
    assert "records collected: 10; a push would send 0 (10 unchanged since the last push" in out


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


def test_init_refuses_what_is_not_an_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO("https://specivo.example.com/my/api-keys/\n"))
    code = cli.main(["init", "--url", "https://specivo.example.com", "--key-stdin", "--no-keyring", "--offline"])
    assert code != 0
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
    assert usage_batch(out)["records"] == []
    assert cli.main(["doctor", "--offline"]) in (0, 1)
    out, _ = capsys.readouterr()
    assert "instead of their logs: codex" in out


def test_init_reuses_the_configured_url_to_replace_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text('url = "https://specivo.example.com"\n', encoding="utf-8")
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO("spv_replacement\n"))
    assert cli.main(["init", "--key-stdin", "--no-keyring", "--offline"]) == 0
    assert (cfg / "api-key").read_text().strip() == "spv_replacement"
    assert (cfg / "config.toml").read_text() == 'url = "https://specivo.example.com"\n'


def test_init_without_url_or_config_does_not_prompt_when_piped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("sys.stdin", io.StringIO("spv_x\n"))
    assert cli.main(["init", "--key-stdin", "--no-keyring", "--offline"]) != 0
    assert "--url" in capsys.readouterr().err


def test_doctor_after_a_push_has_nothing_to_send(
    env: dict[str, Path], server: FakeServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A server that takes every optional field (active time among them), and a session issue captured by a
    # hook's push: doctor must build the records as push does (gated, with the session and git hints), or it
    # keeps counting records a push finds unchanged.
    from ai_usage_reporter.record import OPTIONAL_FIELDS

    server.info["optional_fields"] = list(OPTIONAL_FIELDS)
    monkeypatch.setenv("SPECIVO_ISSUE", "ACME-12")
    assert cli.main(["push", "--session", "11111111-2222-4111-8111-111111111111"]) == 0
    monkeypatch.delenv("SPECIVO_ISSUE")
    assert cli.main(["push"]) == 0
    assert any("active_seconds" in r for batch in server.posts for r in batch)
    capsys.readouterr()
    assert cli.main(["push"]) == 0
    _out, err = capsys.readouterr()
    assert "sent 0" in err or "sent 0" in _out
    cli.main(["doctor", "--offline"])
    out, _ = capsys.readouterr()
    assert "a push would send 0 (10 unchanged since the last push" in out
    assert cli.main(["show-payload"]) == 0
    out, err = capsys.readouterr()
    assert usage_batch(out)["records"] == [] and "10 unchanged since the last push" in err


# -- ai-usage time ----------------------------------------------------------------------------------

CANARY_BRANCH = "CANARY-BRANCH-3e8a"
SECOND_SESSION = "b8b8b8b8-0000-4000-8000-000000000002"
THIRD_SESSION = "c9c9c9c9-0000-4000-8000-000000000003"
TIME_RANGE = ["--since", "2026-10-03", "--until", "2026-10-06", "--no-git"]


def _mark(t: Any, when: str, done: str, issue: str, call: str) -> None:
    t.answer(when, tool="mcp__specivo__specivo_update_issue", call=call, arguments={"issue_ref": issue, "status_id": 2})
    t.result(done, call)


def _time_home(home: Path) -> Path:
    """Three Claude Code sessions on 2026-10-05 (seconds worked out by hand).

    S1 marks ACME-1 at 09:00:10 and ACME-2 at 09:05:00: ACME-1 09:00:00..09:01:00 (60 s), ACME-2
    09:01:00..09:06:00 (300 s; the minutes before the request that marked it are that request's).
    S2 marks ACME-1 at 09:02:30: 09:02:00..09:07:00 (30 + 1 + 269 = 300 s), all ACME-1.
    S3 marks ACME-2 at 09:03:20: 09:03:00..09:04:00 (60 s), inside S1's ACME-2 span.

    ACME-1: agent 360 s, wall clock 360 s. ACME-2: agent 360 s, wall clock 300 s (S3 overlaps S1).
    Total: agent 720 s; wall clock the union 09:00:00..09:07:00, 420 s (not 660, the sum of the rows).
    """
    from tests.fixtures.active_time_home import SESSION, Transcript

    s1 = Transcript(SESSION)
    s1.prompt("2026-10-05T09:00:00.000Z")
    _mark(s1, "2026-10-05T09:00:10.000Z", "2026-10-05T09:00:11.000Z", "ACME-1", "toolu_m1")
    s1.answer("2026-10-05T09:01:00.000Z")
    _mark(s1, "2026-10-05T09:05:00.000Z", "2026-10-05T09:05:01.000Z", "ACME-2", "toolu_m2")
    s1.answer("2026-10-05T09:06:00.000Z")
    s2 = Transcript(SECOND_SESSION)
    s2.prompt("2026-10-05T09:02:00.000Z")
    _mark(s2, "2026-10-05T09:02:30.000Z", "2026-10-05T09:02:31.000Z", "ACME-1", "toolu_m3")
    s2.answer("2026-10-05T09:07:00.000Z")
    s3 = Transcript(THIRD_SESSION)
    s3.prompt("2026-10-05T09:03:00.000Z")
    _mark(s3, "2026-10-05T09:03:20.000Z", "2026-10-05T09:03:21.000Z", "ACME-2", "toolu_m4")
    s3.answer("2026-10-05T09:04:00.000Z")
    for transcript in (s1, s2, s3):
        for line in transcript.lines:
            line["gitBranch"] = f"feat/{CANARY_BRANCH}"
        transcript.write(home)
    return home


@pytest.fixture
def time_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with the three Claude Code sessions and OpenCode's older JSON layout (no active time)."""
    from tests.fixtures.opencode_home import _legacy_storage

    home = _time_home(tmp_path / "home")
    opencode = tmp_path / "opencode"
    _legacy_storage(opencode / "storage")
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "config.toml").write_text(
        f'url = "https://specivo.example.com"\n\n[sources.opencode]\nroots = ["{opencode.as_posix()}"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("AI_USAGE_CONFIG_DIR", str(config))
    monkeypatch.setenv("AI_USAGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    for name in ("SPECIVO_ISSUE", "XDG_DATA_HOME", "OPENCODE_DB", "HERMES_HOME"):
        monkeypatch.delenv(name, raising=False)
    return config


def _time_json(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    assert cli.main(["time", "--json", *TIME_RANGE, *args]) == 0
    out, err = capsys.readouterr()
    _no_canaries(out + err)
    assert CANARY_BRANCH not in out + err
    result: dict[str, Any] = json.loads(out)
    return result


def test_time_by_issue_sums_agents_and_joins_the_wall_clock(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _time_json(capsys)
    groups = {g["key"]: g for g in report["groups"]}
    assert groups["ACME-1"] == {
        "key": "ACME-1",
        "wall_clock_seconds": 360,
        "agent_seconds": 360,
        "sessions": 2,
        "records": 2,
        "unmeasured_records": 0,
    }
    assert groups["ACME-2"]["agent_seconds"] == 360 and groups["ACME-2"]["wall_clock_seconds"] == 300
    # OpenCode's JSON layout yields no thread events: its records are "not measured", never zero.
    gemini = groups["(unassigned)"]
    assert gemini["agent_seconds"] is None and gemini["wall_clock_seconds"] is None
    assert gemini["unmeasured_records"] == gemini["records"] > 0
    total = report["total"]
    assert total["agent_seconds"] == 720 and total["wall_clock_seconds"] == 420
    assert total["unmeasured_records"] == gemini["records"]
    assert report["by"] == "issue" and report["would_send"] is True
    assert (report["since"], report["until"]) == ("2026-10-03", "2026-10-06")


def test_time_matches_the_records_a_dry_run_sends(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["push", "--dry-run", "--all", "--no-git"]) == 0
    records = usage_batch(capsys.readouterr()[0])["records"]
    sent = sum(r.get("active_seconds", 0) for r in records)
    assert _time_json(capsys)["total"]["agent_seconds"] == sent == 720


def test_time_by_day_session_and_tool(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.fixtures.active_time_home import SESSION

    days = _time_json(capsys, "--by", "day")["groups"]
    assert [g["key"] for g in days] == ["2026-10-03", "2026-10-05"]
    assert days[1]["agent_seconds"] == 720 and days[1]["wall_clock_seconds"] == 420
    assert days[0]["agent_seconds"] is None and days[0]["unmeasured_records"] > 0

    sessions = {(g["tool"], g["key"]): g for g in _time_json(capsys, "--by", "session")["groups"]}
    assert sessions[("claude-code", SESSION)]["agent_seconds"] == 360
    assert sessions[("claude-code", SESSION)]["wall_clock_seconds"] == 360
    assert sessions[("claude-code", SECOND_SESSION)]["agent_seconds"] == 300
    assert sessions[("claude-code", THIRD_SESSION)]["agent_seconds"] == 60
    assert {tool for tool, _ in sessions} == {"claude-code", "opencode"}

    tools = {g["key"]: g for g in _time_json(capsys, "--by", "tool")["groups"]}
    assert tools["claude-code"]["agent_seconds"] == 720 and tools["claude-code"]["sessions"] == 3
    assert tools["opencode"]["agent_seconds"] is None


def test_time_prints_a_table_with_a_total_row(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["time", *TIME_RANGE]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].split() == ["issue", "wall", "clock", "agent", "time", "sessions", "not", "measured"]
    rows = {line.split()[0]: line for line in lines[2:-2]}
    assert rows["ACME-1"].split()[1:] == ["6", "min", "6", "min", "2", "0"]
    assert rows["ACME-2"].split()[1:] == ["5", "min", "6", "min", "2", "0"]
    assert rows["(unassigned)"].split()[1:3] == ["-", "-"]
    assert lines[-1].split()[:5] == ["total", "7", "min", "12", "min"]
    assert "2026-10-03 to 2026-10-06" in err
    _no_canaries(out + err)
    assert CANARY_BRANCH not in out + err and "/work/" not in out + err

    assert cli.main(["time", "--by", "session", *TIME_RANGE]) == 0
    out, _ = capsys.readouterr()
    assert out.splitlines()[0].split()[:2] == ["session", "tool"]


def test_time_measures_with_active_time_off_and_says_so(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (time_env / "config.toml").write_text(
        (time_env / "config.toml")
        .read_text(encoding="utf-8")
        .replace('url = "https://specivo.example.com"\n', 'url = "https://specivo.example.com"\nactive_time = false\n'),
        encoding="utf-8",
    )
    assert cli.main(["time", "--json", *TIME_RANGE]) == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["total"]["agent_seconds"] == 720 and report["would_send"] is False
    assert "would not be sent" in err


def test_time_sends_nothing_and_writes_no_state(
    time_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ai-usage time must not contact a server")

    monkeypatch.setattr(cli, "SpecivoSink", refuse)
    monkeypatch.delenv("SPECIVO_API_KEY", raising=False)
    assert cli.main(["time", *TIME_RANGE]) == 0
    assert not (tmp_path / "state").exists()


def test_time_default_period_and_bad_dates(time_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["time", "--json", "--no-git"]) == 0
    report = json.loads(capsys.readouterr()[0])
    # The fixtures are older than the default seven days.
    assert report["groups"] == [] and report["total"]["records"] == 0
    assert cli.main(["time", "--json", "--until", "2026-10-06", "--no-git"]) == 0
    report = json.loads(capsys.readouterr()[0])
    assert report["since"] == "2026-09-30" and report["total"]["agent_seconds"] == 720
    assert cli.main(["time", "--since", "10/05/2026"]) == 2
    assert cli.main(["time", "--since", "2026-10-06", "--until", "2026-10-05"]) == 2
    assert "--until is before --since" in capsys.readouterr()[1]
