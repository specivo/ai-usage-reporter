"""Which issue an agent marked, read from its own Specivo tool calls in a transcript.

An agent connected to Specivo over MCP says which issue it works on with a few
tool calls. Those calls, and only those, are **marking calls**:

- ``specivo_update_issue`` that changes the status or the assignee
  (``status_id`` or ``assigned_to_id`` is given): the issue in ``issue_ref``;
- ``specivo_timer`` with ``op = "start"``: the issue in ``issue_ref``;
- ``specivo_create_issue``: the issue it created, read from the tool's result
  (``Created issue ACME-12: ...``). When the result does not start that way,
  the call marks nothing.

The tool may come from any MCP server (``mcp__specivo__specivo_timer``,
``mcp__work__specivo_timer``, ``specivo.specivo_timer``): only the part from
``specivo_`` on counts. Every other call (``specivo_show_issue``,
``specivo_add_comment``, ...) marks nothing.

**Privacy.** Only the tool's name and its ``issue_ref``, ``status_id``,
``assigned_to_id`` and ``op`` arguments are looked at, and only the issue key
is kept, normalised to upper case and checked against the contract's
``issue_ref`` pattern. Nothing else of a call or of its result is read into
the reporter's data, and the key itself is only ever sent as a record's
``attribution.issue_ref``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

#: The contract's issue_ref pattern.
_ISSUE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}-[1-9][0-9]{0,8}$")
#: ``specivo_<tool>``, after an optional MCP server prefix ending in ``__``, ``_`` or ``.``.
_TOOL = re.compile(r"^(?:.*(?:__|[._]))?specivo_([a-z_]+)$")
#: The start of specivo_create_issue's answer; the key must be followed by a separator.
_CREATED = re.compile(r"^Created issue ([A-Za-z][A-Za-z0-9]{0,127}-[1-9][0-9]{0,8})(?=$|[:\s.,;])")

#: Returned by :func:`marking_call` for ``specivo_create_issue``: the key is in the result.
CREATE: Final = "<create>"


def specivo_tool(name: object) -> str | None:
    """The Specivo tool a (possibly prefixed) tool name calls, e.g. ``update_issue``; else ``None``."""
    if not isinstance(name, str) or len(name) > 200:
        return None
    match = _TOOL.match(name.strip())
    return match.group(1) if match else None


def normalise_issue(value: object) -> str | None:
    """An issue key in upper case, or ``None`` when *value* is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text.upper() if _ISSUE.match(text) else None


def marking_call(name: object, arguments: object) -> str | None:
    """The issue a tool call marks, :data:`CREATE` when its result names it, else ``None``.

    *arguments* may be the input object or its JSON text (Codex logs a string).
    """
    tool = specivo_tool(name)
    if tool not in ("update_issue", "timer", "create_issue"):
        return None
    if tool == "create_issue":
        return CREATE
    args = _arguments(arguments)
    if args is None:
        return None
    if tool == "update_issue":
        if args.get("status_id") is None and args.get("assigned_to_id") is None:
            return None
    elif args.get("op") != "start":
        return None
    return normalise_issue(args.get("issue_ref"))


def created_issue(result: object) -> str | None:
    """The key ``specivo_create_issue`` reports in its result, else ``None``.

    *result* is the tool result's content: a string, a list of content blocks
    (``{"type": "text", "text": ...}``), or the JSON text ``{"result": "..."}``
    some clients wrap it in. Only the start of the first text is matched.
    """
    text = _first_text(result)
    if text is None:
        return None
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        inner = data.get("result") if isinstance(data, dict) else None
        if not isinstance(inner, str):
            return None
        text = inner.strip()
    match = _CREATED.match(text)
    return normalise_issue(match.group(1)) if match else None


def _arguments(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        if len(value) > 1_000_000:
            return None
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, Mapping) else None


def _first_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text: str = block["text"]
                return text
            if isinstance(block, str):
                return block
    return None
