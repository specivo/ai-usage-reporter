"""``ai-usage schedule install|remove``: run ``ai-usage push`` on a timer (default every 30 minutes).

=========  ====================================================  ===========================
Kind       What is written                                        Then
=========  ====================================================  ===========================
launchd    ``~/Library/LaunchAgents/io.specivo.ai-usage-          ``launchctl bootstrap
           reporter.plist`` (``StartInterval``)                   gui/<uid>``
systemd    ``ai-usage-reporter.service`` and ``.timer`` in        ``systemctl --user enable
           ``$XDG_CONFIG_HOME/systemd/user``                      --now``
schtasks   nothing; a Task Scheduler task                         ``schtasks /Create``
           ``SpecivoAIUsageReporter``
=========  ====================================================  ===========================

The kind follows the system (macOS, Windows, otherwise systemd). cron needs
no installer; see the docs. Installing again replaces the schedule (a changed
file is backed up first); removing a schedule that is not there does nothing.
The push writes its output to ``schedule.log`` in the state directory.

Files are written under the injected ``home`` and ``env``, and commands are
run through the injected ``run``, so tests never touch the real system.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ai_usage_reporter.hooks import backup, write_atomically

LABEL = "io.specivo.ai-usage-reporter"
UNIT = "ai-usage-reporter"
TASK = "SpecivoAIUsageReporter"
KINDS = ("launchd", "systemd", "schtasks")
DEFAULT_MINUTES = 30

Runner = Callable[[Sequence[str]], int]


class ScheduleError(Exception):
    pass


def run_command(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False, capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def default_kind(platform: str = sys.platform) -> str:
    if platform == "darwin":
        return "launchd"
    if platform.startswith("win"):
        return "schtasks"
    return "systemd"


@dataclass
class ScheduleContext:
    home: Path
    env: Mapping[str, str]
    command: tuple[str, ...]
    log: Path
    run: Runner = run_command
    uid: int = field(default_factory=lambda: getattr(os, "getuid", lambda: 0)())


@dataclass(frozen=True, slots=True)
class Result:
    kind: str
    action: str  # installed, updated, unchanged, removed, absent
    files: tuple[Path, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    failed: tuple[str, ...] = ()


def _check_minutes(minutes: int) -> int:
    if not 5 <= minutes <= 24 * 60:
        raise ScheduleError("--every takes minutes between 5 and 1440")
    return minutes


# -- launchd -----------------------------------------------------------------------


def launchd_path(ctx: ScheduleContext) -> Path:
    return ctx.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_plist(ctx: ScheduleContext, minutes: int) -> str:
    data = {
        "Label": LABEL,
        "ProgramArguments": [*ctx.command, "push"],
        "StartInterval": minutes * 60,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(ctx.log),
        "StandardErrorPath": str(ctx.log),
    }
    return plistlib.dumps(data, sort_keys=False).decode("utf-8")


# -- systemd -----------------------------------------------------------------------


def systemd_dir(ctx: ScheduleContext) -> Path:
    xdg = ctx.env.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else ctx.home / ".config") / "systemd" / "user"


def _systemd_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-+=:@," for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_units(ctx: ScheduleContext, minutes: int) -> dict[str, str]:
    exec_line = " ".join(_systemd_quote(a) for a in (*ctx.command, "push"))
    service = (
        "[Unit]\n"
        "Description=Report AI coding-tool usage to Specivo\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_line}\n"
        "Nice=10\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Report AI coding-tool usage to Specivo regularly\n\n"
        "[Timer]\n"
        f"OnBootSec={minutes}min\n"
        f"OnUnitActiveSec={minutes}min\n"
        "RandomizedDelaySec=60\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


# -- install / remove ----------------------------------------------------------------


def _write(path: Path, text: str) -> bool:
    try:
        old = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old = None
    if old == text:
        return False
    backup(path)
    write_atomically(path, text)
    return True


def _run_all(ctx: ScheduleContext, commands: Sequence[tuple[str, ...]], *, tolerate: int = 0) -> tuple[str, ...]:
    """Run *commands*; the first *tolerate* may fail (cleanup of an old schedule)."""
    failed: list[str] = []
    for index, argv in enumerate(commands):
        if ctx.run(argv) != 0 and index >= tolerate:
            failed.append(" ".join(argv))
    return tuple(failed)


def install(ctx: ScheduleContext, kind: str, minutes: int = DEFAULT_MINUTES) -> Result:
    minutes = _check_minutes(minutes)
    commands: list[tuple[str, ...]]
    if kind == "launchd":
        path = launchd_path(ctx)
        changed = _write(path, launchd_plist(ctx, minutes))
        domain = f"gui/{ctx.uid}"
        commands = [
            ("launchctl", "bootout", f"{domain}/{LABEL}"),
            ("launchctl", "bootstrap", domain, str(path)),
        ]
        failed = _run_all(ctx, commands, tolerate=1)
        return Result(kind, "updated" if changed else "unchanged", (path,), tuple(commands), failed)
    if kind == "systemd":
        folder = systemd_dir(ctx)
        changed = False
        files = []
        for name, text in systemd_units(ctx, minutes).items():
            changed |= _write(folder / name, text)
            files.append(folder / name)
        commands = [
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", f"{UNIT}.timer"),
        ]
        failed = _run_all(ctx, commands)
        return Result(kind, "updated" if changed else "unchanged", tuple(files), tuple(commands), failed)
    if kind == "schtasks":
        action = subprocess.list2cmdline([*ctx.command, "push"])
        commands = [("schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes), "/TN", TASK, "/TR", action)]
        failed = _run_all(ctx, commands)
        return Result(kind, "updated", (), tuple(commands), failed)
    raise ScheduleError(f"unknown schedule kind {kind!r}; known: {', '.join(KINDS)}")


def remove(ctx: ScheduleContext, kind: str) -> Result:
    commands: list[tuple[str, ...]]
    if kind == "launchd":
        path = launchd_path(ctx)
        commands = [("launchctl", "bootout", f"gui/{ctx.uid}/{LABEL}")]
        _run_all(ctx, commands, tolerate=1)
        if not path.exists():
            return Result(kind, "absent", (), tuple(commands))
        backup(path)
        path.unlink()
        return Result(kind, "removed", (path,), tuple(commands))
    if kind == "systemd":
        folder = systemd_dir(ctx)
        files = [folder / name for name in (f"{UNIT}.timer", f"{UNIT}.service")]
        if not any(f.exists() for f in files):
            return Result(kind, "absent")
        commands = [("systemctl", "--user", "disable", "--now", f"{UNIT}.timer")]
        _run_all(ctx, commands, tolerate=1)
        for path in files:
            if path.exists():
                backup(path)
                path.unlink()
        reload = ("systemctl", "--user", "daemon-reload")
        _run_all(ctx, [reload], tolerate=1)
        return Result(kind, "removed", tuple(files), (*commands, reload))
    if kind == "schtasks":
        commands = [("schtasks", "/Delete", "/F", "/TN", TASK)]
        failed = _run_all(ctx, commands, tolerate=1)
        return Result(kind, "removed", (), tuple(commands), failed)
    raise ScheduleError(f"unknown schedule kind {kind!r}; known: {', '.join(KINDS)}")
