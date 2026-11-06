"""Schedule installers on a temporary home, with the system commands recorded instead of run."""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_usage_reporter import schedule
from ai_usage_reporter.hooks import BACKUP_INFIX

COMMAND = ("/opt/tools/bin/ai-usage",)


class Recorder:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.codes = codes or {}

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(tuple(argv))
        return self.codes.get(argv[1] if len(argv) > 1 else "", 0)


def _ctx(home: Path, run: Recorder, **env: str) -> schedule.ScheduleContext:
    return schedule.ScheduleContext(
        home=home, env=env, command=COMMAND, log=home / "state" / "schedule.log", run=run, uid=501
    )


def test_default_kind() -> None:
    assert schedule.default_kind("darwin") == "launchd"
    assert schedule.default_kind("win32") == "schtasks"
    assert schedule.default_kind("linux") == "systemd"


def test_launchd(tmp_path: Path) -> None:
    run = Recorder({"bootout": 3})  # nothing to unload the first time
    result = schedule.install(_ctx(tmp_path, run), "launchd", 30)
    plist = tmp_path / "Library" / "LaunchAgents" / "io.specivo.ai-usage-reporter.plist"
    assert result.files == (plist,) and result.failed == ()
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [*COMMAND, "push"]
    assert data["StartInterval"] == 1800 and data["Label"] == schedule.LABEL
    assert run.calls == [
        ("launchctl", "bootout", f"gui/501/{schedule.LABEL}"),
        ("launchctl", "bootstrap", "gui/501", str(plist)),
    ]
    assert schedule.install(_ctx(tmp_path, Recorder()), "launchd", 30).action == "unchanged"
    changed = schedule.install(_ctx(tmp_path, Recorder()), "launchd", 15)
    assert changed.action == "updated" and plistlib.loads(plist.read_bytes())["StartInterval"] == 900
    assert any(BACKUP_INFIX in p.name for p in plist.parent.iterdir())
    removed = schedule.remove(_ctx(tmp_path, Recorder()), "launchd")
    assert removed.action == "removed" and not plist.exists()
    assert schedule.remove(_ctx(tmp_path, Recorder()), "launchd").action == "absent"


def test_systemd(tmp_path: Path) -> None:
    run = Recorder()
    ctx = _ctx(tmp_path, run, XDG_CONFIG_HOME=str(tmp_path / "cfg"))
    result = schedule.install(ctx, "systemd", 30)
    folder = tmp_path / "cfg" / "systemd" / "user"
    service = (folder / "ai-usage-reporter.service").read_text()
    timer = (folder / "ai-usage-reporter.timer").read_text()
    assert "ExecStart=/opt/tools/bin/ai-usage push" in service and "Type=oneshot" in service
    assert "OnUnitActiveSec=30min" in timer and "WantedBy=timers.target" in timer
    assert run.calls == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "ai-usage-reporter.timer"),
    ]
    assert result.action == "updated"
    removed = schedule.remove(ctx, "systemd")
    assert removed.action == "removed" and not any(f.suffix in (".timer", ".service") for f in folder.iterdir())
    assert schedule.remove(ctx, "systemd").action == "absent"


def test_systemd_quotes_paths_with_spaces(tmp_path: Path) -> None:
    ctx = schedule.ScheduleContext(
        home=tmp_path, env={}, command=("/opt/my tools/ai-usage",), log=tmp_path / "l", run=Recorder()
    )
    assert 'ExecStart="/opt/my tools/ai-usage" push' in schedule.systemd_units(ctx, 30)["ai-usage-reporter.service"]


def test_schtasks(tmp_path: Path) -> None:
    run = Recorder()
    result = schedule.install(_ctx(tmp_path, run), "schtasks", 45)
    [call] = run.calls
    assert call[:9] == ("schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "45", "/TN", schedule.TASK)
    assert call[-1] == "/opt/tools/bin/ai-usage push"
    assert result.failed == ()
    schedule.remove(_ctx(tmp_path, run), "schtasks")
    assert run.calls[-1] == ("schtasks", "/Delete", "/F", "/TN", schedule.TASK)


def test_failures_and_bad_input_are_reported(tmp_path: Path) -> None:
    result = schedule.install(_ctx(tmp_path, Recorder({"--user": 1})), "systemd")
    assert len(result.failed) == 2
    with pytest.raises(schedule.ScheduleError):
        schedule.install(_ctx(tmp_path, Recorder()), "systemd", 1)
    with pytest.raises(schedule.ScheduleError):
        schedule.install(_ctx(tmp_path, Recorder()), "cron")
