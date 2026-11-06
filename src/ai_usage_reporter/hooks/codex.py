"""Codex CLI: the ``notify`` program in ``$CODEX_HOME/config.toml`` (default ``~/.codex``).

Codex runs ``notify`` after every agent turn with one more argument: a JSON
object whose ``type`` is ``agent-turn-complete`` and whose ``thread-id`` is
the session id (it also holds ``turn-id``, ``cwd``, ``input-messages`` and
``last-assistant-message``, which the hook never reads). Only the user's
``config.toml`` may set it.

``notify`` holds one program. When the file already names another one, the
reporter's hook takes its place and runs it in turn (``--chain`` keeps its
command line), and ``uninstall`` puts it back.

The standard library reads TOML but does not write it, so the file is edited
as text: only the top-level ``notify = ...`` value is replaced, inserted or
removed, and the result is parsed again and compared with the original. If
anything other than ``notify`` would differ, nothing is written.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from ai_usage_reporter.hooks import Change, HookContext, HookError, is_reporter_command, read_text, replace_file

TOOL = "codex"
CHAIN = "--chain"
COMMENT = "# Reports token usage to Specivo after each turn (ai-usage hooks install --tool codex).\n"


def config_path(ctx: HookContext) -> Path:
    override = ctx.env.get("CODEX_HOME", "").strip()
    base = ctx.expand(override) if override else ctx.home / ".codex"
    return base / "config.toml"


def _parse(path: Path, text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HookError(f"{path.name} is not valid TOML ({exc}); left unchanged") from None


def ours(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(v, str) for v in value)
        and is_reporter_command(" ".join(value), TOOL)
    )


def chained(value: list[str]) -> list[str] | None:
    """The program the reporter's notify runs after itself, if any."""
    if CHAIN in value:
        index = value.index(CHAIN)
        if index + 1 < len(value):
            try:
                original = json.loads(value[index + 1])
            except ValueError:
                return None
            if isinstance(original, list) and original and all(isinstance(v, str) for v in original):
                return original
    return None


def install(ctx: HookContext) -> Change:
    path = config_path(ctx)
    old = read_text(path)
    data = _parse(path, old or "")
    current = data.get("notify")
    if current is not None and not (isinstance(current, list) and all(isinstance(v, str) for v in current)):
        raise HookError(f"{path.name}: notify is not a list of strings; left unchanged")
    previous: list[str] | None = chained(current) if isinstance(current, list) and ours(current) else current
    argv = [*ctx.command, "hook", TOOL]
    if previous:
        argv += [CHAIN, json.dumps(previous)]
    new = set_notify(path, old or "", argv)
    return replace_file(TOOL, path, old, new, installing=True)


def uninstall(ctx: HookContext) -> Change:
    path = config_path(ctx)
    old = read_text(path)
    if old is None:
        return Change(TOOL, path, "absent")
    current = _parse(path, old).get("notify")
    if not ours(current):
        return Change(TOOL, path, "absent")
    assert isinstance(current, list)
    previous = chained(current)
    new: str | None = set_notify(path, old, previous)
    if new is not None and not new.strip():
        new = None  # the file held nothing but the reporter's notify (a backup is kept)
    return replace_file(TOOL, path, old, new, installing=False)


def toml_array(values: list[str]) -> str:
    # A JSON string with ASCII escapes is a valid TOML basic string.
    return "[" + ", ".join(json.dumps(v, ensure_ascii=True) for v in values) + "]"


def set_notify(path: Path, text: str, argv: list[str] | None) -> str:
    """*text* with the top-level ``notify`` set to *argv* (removed when ``None``), checked by re-parsing."""
    before = _parse(path, text)
    lines = text.splitlines(keepends=True)
    span = _notify_span(lines)
    replacement = [COMMENT, f"notify = {toml_array(argv)}\n"] if argv is not None else []
    if span is not None:
        start, end = span
        if start > 0 and lines[start - 1] == COMMENT:
            start -= 1
        if (
            not replacement
            and end < len(lines)
            and not lines[end].strip()
            and (start == 0 or not lines[start - 1].strip())
        ):
            end += 1  # the blank line the install put before the next table
        lines[start:end] = replacement
    elif replacement:
        at = _first_table(lines)
        if at > 0 and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        block = replacement + (["\n"] if at < len(lines) else [])
        lines[at:at] = block
    result = "".join(lines)
    after = _parse(path, result)
    expected = {k: v for k, v in before.items() if k != "notify"}
    if argv is not None:
        expected["notify"] = argv
    if after != expected:
        raise HookError(f"{path.name}: could not change notify safely; left unchanged, edit it by hand")
    return result


def _first_table(lines: list[str]) -> int:
    """The index of the first table header (top-level keys must come before it)."""
    in_multiline: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if in_multiline:
            if stripped.count(in_multiline) % 2 == 1:
                in_multiline = None
            continue
        for quote in ('"""', "'''"):
            if stripped.count(quote) % 2 == 1:
                in_multiline = quote
        if stripped.startswith("[") and not in_multiline:
            return index
    return len(lines)


def _notify_span(lines: list[str]) -> tuple[int, int] | None:
    """The lines ``[start, end)`` of the top-level ``notify = ...`` assignment."""
    end_of_top = _first_table(lines)
    for index in range(end_of_top):
        stripped = lines[index].lstrip()
        key = stripped.split("=", 1)[0].strip().strip('"').strip("'")
        if "=" not in stripped or key != "notify":
            continue
        depth = 0
        quote: str | None = None
        escaped = False
        started = False
        for line_no in range(index, end_of_top):
            text = lines[line_no] if line_no > index else stripped.split("=", 1)[1]
            for char in text:
                if quote:
                    if escaped:
                        escaped = False
                    elif char == "\\" and quote == '"':
                        escaped = True
                    elif char == quote:
                        quote = None
                    continue
                if char in "\"'":
                    quote = char
                elif char == "#":
                    break
                elif char == "[":
                    depth += 1
                    started = True
                elif char == "]":
                    depth -= 1
            if started and depth <= 0:
                return index, line_no + 1
            if not started and text.strip():
                return index, line_no + 1  # a scalar value on one line
        return index, end_of_top
    return None
