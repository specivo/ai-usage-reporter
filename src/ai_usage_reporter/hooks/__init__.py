"""Session hooks: the AI tools call the reporter when a session ends or goes idle.

``ai-usage hooks install --tool <tool>`` adds one entry to the tool's own
config, ``uninstall`` takes it out again. Both are idempotent, leave every
other setting and every other hook as it was, and copy a file to
``<name>.ai-usage-backup-<time>`` before changing it.

=============  =========================================  ===============================
Tool           What is installed                          Events
=============  =========================================  ===============================
claude-code    a command hook in ``settings.json``        ``SessionEnd``, ``Stop``
codex          the ``notify`` program in ``config.toml``  ``agent-turn-complete``
gemini-cli     a command hook in ``settings.json``        ``SessionEnd``, ``AfterAgent``
opencode       a plugin file in ``plugins/``              ``session.idle``
=============  =========================================  ===============================

The hook command is ``ai-usage hook <tool>`` (see
:mod:`ai_usage_reporter.hooks.runner`): it reads the session id from what the
tool passes, starts ``ai-usage push --source <tool> --session <id>`` in the
background and returns at once. It always exits 0, so a reporter problem
never blocks or slows the tool. The OpenCode plugin starts the push itself.

Every location comes from the injected ``home`` and ``env``, so tests never
touch real config files.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

HOOK_TOOLS = ("claude-code", "codex", "gemini-cli", "opencode")
BACKUP_INFIX = ".ai-usage-backup-"


class HookError(Exception):
    """A config file cannot be changed safely; it is left as it was."""


@dataclass(frozen=True, slots=True)
class HookContext:
    home: Path
    env: Mapping[str, str]
    #: The reporter's command line, e.g. ``["/home/me/.local/bin/ai-usage"]``.
    command: tuple[str, ...]
    windows: bool = sys.platform.startswith("win")

    @classmethod
    def from_environment(cls) -> HookContext:
        return cls(home=Path.home(), env=dict(os.environ), command=tuple(reporter_command()))

    def expand(self, raw: str) -> Path:
        if raw == "~" or raw.startswith(("~/", "~\\")):
            return self.home / raw[2:] if len(raw) > 1 else self.home
        return Path(raw)

    def shell(self, *args: str) -> str:
        """The reporter command plus *args* as one shell command line."""
        argv = [*self.command, *args]
        return subprocess.list2cmdline(argv) if self.windows else shlex.join(argv)


@dataclass(frozen=True, slots=True)
class Change:
    tool: str
    path: Path
    #: ``installed``, ``updated``, ``unchanged``, ``removed`` or ``absent``.
    action: str
    backup: Path | None = None
    note: str = ""


def reporter_command() -> list[str]:
    """How the tools should start the reporter: the ``ai-usage`` launcher, else this Python."""
    found = shutil.which("ai-usage")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "ai_usage_reporter"]


def is_reporter_command(text: str, tool: str) -> bool:
    """Whether a hook command line is this reporter's hook for *tool*."""
    if "ai-usage" not in text and "ai_usage_reporter" not in text:
        return False
    parts = text.replace('"', " ").replace("'", " ").split()
    return any(a == "hook" and b == tool for a, b in zip(parts, parts[1:], strict=False))


def backup(path: Path, directory: Path | None = None) -> Path | None:
    """Copy *path* before it is changed, next to it or into *directory*; ``None`` when there is nothing to keep."""
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    folder = directory or path.parent
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.name}{BACKUP_INFIX}{stamp}"
    shutil.copy2(path, target)
    return target


def write_atomically(path: Path, text: str) -> None:
    """Replace *path* with *text* in one step, keeping its permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def replace_file(
    tool: str, path: Path, old: str | None, new: str | None, *, installing: bool, backup_dir: Path | None = None
) -> Change:
    """Write *new* (or delete the file when ``None``) after a backup, unless nothing changes."""
    if new == old:
        return Change(tool, path, "unchanged" if installing else "absent")
    copy = backup(path, backup_dir)
    if new is None:
        path.unlink(missing_ok=True)
    else:
        write_atomically(path, new)
    if installing:
        action = "updated" if old is not None else "installed"
    else:
        action = "removed"
    return Change(tool, path, action, copy)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HookError(f"cannot read {path.name}: {exc.strerror}") from None


def load_json_object(path: Path, text: str | None) -> dict[str, object]:
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise HookError(f"{path.name} is not plain JSON (comments?); left unchanged, edit it by hand") from None
    if not isinstance(data, dict):
        raise HookError(f"{path.name} does not hold a JSON object; left unchanged")
    return data


def install(tool: str, ctx: HookContext) -> Change:
    return _installers(tool)[0](ctx)


def uninstall(tool: str, ctx: HookContext) -> Change:
    return _installers(tool)[1](ctx)


def _installers(tool: str) -> tuple[Callable[[HookContext], Change], Callable[[HookContext], Change]]:
    from ai_usage_reporter.hooks import claude_code, codex, gemini_cli, opencode

    modules = {"claude-code": claude_code, "codex": codex, "gemini-cli": gemini_cli, "opencode": opencode}
    module = modules.get(tool)
    if module is None:
        raise HookError(f"no hook for {tool!r}; known: {', '.join(HOOK_TOOLS)}")
    return module.install, module.uninstall


def json_hook_edit(
    *,
    tool: str,
    path: Path,
    events: Sequence[str],
    entry: dict[str, object] | None,
) -> Change:
    """Add (``entry``) or remove (``None``) this reporter's command hook in a settings.json.

    The layout is ``{"hooks": {"<Event>": [{"hooks": [{"type": "command", ...}]}]}}``,
    shared by Claude Code and Gemini CLI. Only entries whose command is this
    reporter's hook for *tool* are touched; a group left empty by removing one
    is removed, and so is an event left without groups.
    """
    old = read_text(path)
    data = load_json_object(path, old)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HookError(f"{path.name}: 'hooks' is not an object; left unchanged")
    hooks = dict(hooks)
    for event in events:
        groups = hooks.get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise HookError(f"{path.name}: hooks.{event} is not a list; left unchanged")
        kept: list[object] = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                inner = [h for h in group["hooks"] if not _ours(h, tool)]
                if not inner and group["hooks"]:
                    continue  # the group held only this reporter's hook
                kept.append({**group, "hooks": inner})
            else:
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if entry is not None:
        for event in events:
            groups = list(hooks.get(event) or [])
            groups.append({"hooks": [entry]})
            hooks[event] = groups
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    new: str | None = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if old is None and entry is None:
        new = None
    elif old is not None and _same_json(old, new):
        new = old
    return replace_file(tool, path, old, new, installing=entry is not None)


def _ours(hook: object, tool: str) -> bool:
    return (
        isinstance(hook, dict) and isinstance(hook.get("command"), str) and is_reporter_command(hook["command"], tool)
    )


def _same_json(old: str, new: str | None) -> bool:
    if new is None:
        return False
    try:
        return bool(json.loads(old) == json.loads(new))
    except ValueError:
        return False
