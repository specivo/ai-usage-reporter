"""Builds a synthetic Gemini CLI chat recording for measuring active time (coarse).

Everything is invented: ids, paths, prompts and numbers. The CANARY-* strings
are planted in prompts, thoughts, tool calls and their results, and paths; none
of them may leave the reporter. The JSONL form is the one current releases
write: a header line, then one line per message; a message's ``timestamp`` is
set when it is created, thoughts have their own, tool calls have none.

:func:`build` writes the conformance home. The expected seconds, worked out by
hand, are in :func:`build`'s docstring and in ``tests/test_gemini_active_time.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"

SESSION = "7e7e7e7e-0000-4000-8000-000000000001"
SUBAGENT = "7e7e7e7e-0000-4000-8000-000000000002"
MODEL = "gemini-2.5-pro"
DAY = "2026-11-05"


def at(clock: str) -> str:
    return f"{DAY}T{clock}.000Z"


def user(mid: str, clock: str, *, response: bool = False) -> dict[str, Any]:
    if response:
        content: list[dict[str, Any]] = [
            {"functionResponse": {"id": "call-1", "name": "run_shell_command", "response": {"output": CANARY_OUTPUT}}}
        ]
    else:
        content = [{"text": f"Please {CANARY_PROMPT}"}]
    return {"id": mid, "timestamp": at(clock), "type": "user", "content": content}


def gemini(mid: str, clock: str, *thoughts: str, tool: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": mid,
        "timestamp": at(clock),
        "type": "gemini",
        "content": [{"text": f"Done {CANARY_OUTPUT}"}],
        "thoughts": [
            {"subject": "Plan", "description": f"Read {CANARY_PATH}/main.py", "timestamp": at(when)}
            for when in thoughts
        ],
        "model": MODEL,
        "tokens": {"input": 500, "output": 50, "cached": 100, "thoughts": 10, "tool": 0, "total": 560},
    }
    if tool:
        message["toolCalls"] = [
            {"id": "call-1", "name": "run_shell_command", "args": {"command": CANARY_PROMPT},
             "result": [{"text": CANARY_OUTPUT}]}
        ]  # fmt: skip
    return message


def chats(home: Path) -> Path:
    return home / ".gemini" / "tmp" / f"app-{CANARY_PATH}" / "chats"


def _write(path: Path, header: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in [header, *messages]), encoding="utf-8")


def build(home: Path) -> Path:
    """The conformance home: one session with a subagent, on one model and day.

    Main thread: prompt 09:00:00; thoughts 09:00:02 and 09:00:03 and the answer 09:00:04 (it calls a shell
    tool) are model events (4 s); the tool's result at 09:03:00 is a ``user`` message holding only a
    function response, so it is no prompt; the next answer 09:08:00 (476 s: the tool run, seen only as a
    gap of less than 10 minutes); 09:08:00 .. 09:20:00 is 12 minutes (nothing); 09:20:00 .. the prompt at
    09:21:00 crosses a turn boundary (nothing); 09:21:00 .. 09:21:05 (5). Main thread: 485 s.
    Subagent: prompt 09:01:00, answers 09:01:30 and 09:02:00: 60 s. The record: 545 s.
    """
    header = {"sessionId": SESSION, "projectHash": "2e" * 32, "startTime": at("09:00:00"),
              "lastUpdated": at("09:21:05"), "kind": "main"}  # fmt: skip
    _write(
        chats(home) / "session-2026-11-05T09-00-7e7e7e7e.jsonl",
        header,
        [
            user("u-1", "09:00:00"),
            gemini("g-1", "09:00:04", "09:00:02", "09:00:03", tool=True),
            user("u-2", "09:03:00", response=True),
            gemini("g-2", "09:08:00"),
            gemini("g-3", "09:20:00"),
            user("u-3", "09:21:00"),
            gemini("g-4", "09:21:05"),
        ],
    )
    sub_header = {"sessionId": SUBAGENT, "projectHash": "2e" * 32, "startTime": at("09:01:00"), "kind": "subagent"}
    _write(
        chats(home) / SESSION / f"{SUBAGENT}.jsonl",
        sub_header,
        [user("s-1", "09:01:00"), gemini("s-2", "09:01:30"), gemini("s-3", "09:02:00")],
    )
    return home
