"""Builds synthetic Claude Code transcripts in which the agent marks issues with Specivo tool calls.

Everything is invented. The CANARY-* strings are planted in prompts, in the
arguments of the Specivo calls (notes, subjects, descriptions), in their
results and in paths; none of them may leave the reporter. Only the issue keys
(ACME-*) may.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"

SESSION = "77777777-7777-4777-8777-777777777777"
QUIET = "88888888-8888-4888-8888-888888888888"
CWD = f"/work/{CANARY_PATH}/app"
MODEL = "claude-sonnet-4-5"


class Transcript:
    """Lines of one transcript file, written with :meth:`write`."""

    def __init__(self, session: str, *, sidechain: bool = False, name: str = "main") -> None:
        self.session = session
        self.name = name
        self.sidechain = sidechain
        self.lines: list[dict[str, Any]] = []
        self._n = 0

    def _base(self, kind: str, when: str) -> dict[str, Any]:
        return {
            "type": kind,
            "isSidechain": self.sidechain,
            "sessionId": self.session,
            "cwd": CWD,
            "gitBranch": "main",
            "version": "2.0.30",
            "timestamp": when,
        }

    def prompt(self, when: str, text: str = CANARY_PROMPT) -> Transcript:
        self.lines.append({**self._base("user", when), "message": {"role": "user", "content": text}})
        return self

    def usage(self, when: str, tokens: int = 100, *, blocks: list[dict[str, Any]] | None = None) -> Transcript:
        self._n += 1
        tag = f"{self.session[:4]}{self.name}{self._n}"
        message = {
            "role": "assistant",
            "model": MODEL,
            "id": f"msg_{tag}",
            "content": blocks or [{"type": "text", "text": f"Working on it. {CANARY_OUTPUT}"}],
            "usage": {"input_tokens": tokens, "output_tokens": tokens // 10, "cache_read_input_tokens": tokens * 2},
        }
        self.lines.append({**self._base("assistant", when), "message": message, "requestId": f"req_{tag}"})
        return self

    def call(self, when: str, tool: str, arguments: dict[str, Any], *, call_id: str | None = None) -> Transcript:
        """An assistant response that calls one tool (with its own usage)."""
        block = {"type": "tool_use", "id": call_id or f"toolu_{len(self.lines)}", "name": tool, "input": arguments}
        return self.usage(when, 50, blocks=[block])

    def result(self, when: str, call_id: str, content: Any, *, is_error: bool = False) -> Transcript:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": content}
        if is_error:
            block["is_error"] = True
        self.lines.append({**self._base("user", when), "message": {"role": "user", "content": [block]}})
        return self

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in self.lines), encoding="utf-8")
        return path


def projects_dir(home: Path) -> Path:
    return home / ".claude" / "projects" / f"-work-{CANARY_PATH}-app"


def build(home: Path) -> Path:
    """A session that works on three issues in a day, with two subagents, and a session without marks."""
    folder = projects_dir(home)
    main = Transcript(SESSION)
    main.prompt("2026-10-05T08:00:00.000Z")
    main.usage("2026-10-05T08:00:10.000Z", 400)  # research before the first mark: ACME-12
    main.call("2026-10-05T08:05:00.000Z", "mcp__specivo__specivo_show_issue", {"issue_ref": "ACME-99"})
    main.call(
        "2026-10-05T08:10:00.000Z",
        "mcp__specivo__specivo_update_issue",
        {"issue_ref": "acme-12", "status_id": 2, "notes": f"Starting. {CANARY_PROMPT}"},
    )
    main.usage("2026-10-05T09:00:00.000Z", 300)
    main.call(
        "2026-10-05T13:00:00.000Z",
        "mcp__specivo__specivo_create_issue",
        {"project_key": "ACME", "subject": f"Follow-up {CANARY_PROMPT}", "description": CANARY_OUTPUT},
        call_id="toolu_create",
    )
    main.result(
        "2026-10-05T13:00:01.000Z",
        "toolu_create",
        json.dumps({"result": f"Created issue ACME-13: Follow-up {CANARY_OUTPUT}"}),
    )
    main.usage("2026-10-05T14:00:00.000Z", 200)
    main.call(
        "2026-10-05T15:00:00.000Z",
        "mcp__work__specivo_timer",
        {"op": "start", "issue_ref": "ACME-14", "comments": CANARY_PROMPT},
    )
    main.usage("2026-10-05T15:30:00.000Z", 100)
    main.write(folder / f"{SESSION}.jsonl")

    # A subagent started while the main thread worked on ACME-12, without marks of its own.
    helper = Transcript(SESSION, sidechain=True, name="helper")
    helper.prompt("2026-10-05T09:30:00.000Z", f"Explore {CANARY_PROMPT}")
    helper.usage("2026-10-05T09:30:05.000Z", 70)
    helper.usage("2026-10-05T13:30:00.000Z", 30)  # still ACME-12, although the main thread moved on
    helper.write(folder / SESSION / "subagents" / "agent-helper01.jsonl")

    # A subagent that marks its own issue after some research.
    worker = Transcript(SESSION, sidechain=True, name="worker")
    worker.prompt("2026-10-05T10:00:00.000Z")
    worker.usage("2026-10-05T10:00:05.000Z", 60)
    worker.call(
        "2026-10-05T10:01:00.000Z", "mcp__specivo__specivo_update_issue", {"issue_ref": "ACME-20", "assigned_to_id": 7}
    )
    worker.usage("2026-10-05T10:30:00.000Z", 40)
    worker.write(folder / SESSION / "subagents" / "agent-worker02.jsonl")

    # A session that never marks an issue keeps today's records.
    quiet = Transcript(QUIET)
    quiet.prompt("2026-10-05T11:00:00.000Z")
    quiet.usage("2026-10-05T11:00:05.000Z", 90)
    quiet.call("2026-10-05T11:01:00.000Z", "mcp__specivo__specivo_add_comment", {"issue_ref": "ACME-5", "notes": "x"})
    quiet.call("2026-10-05T11:02:00.000Z", "mcp__specivo__specivo_timer", {"op": "stop", "issue_ref": "ACME-5"})
    quiet.call("2026-10-05T11:03:00.000Z", "mcp__specivo__specivo_update_issue", {"issue_ref": "ACME-5", "notes": "y"})
    quiet.write(folder / f"{QUIET}.jsonl")
    return home
