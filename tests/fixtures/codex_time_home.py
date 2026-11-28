"""Builds synthetic Codex rollouts in the current format, for measuring active time.

Everything is invented: ids, paths, commands, prompts and numbers. The CANARY-*
strings are planted in commands, tool arguments and output, messages, reasoning
and paths; none of them may leave the reporter.

:class:`Rollout` writes one ``rollout-*.jsonl`` file line by line, the way Codex
does since September 2026: every line has a ``timestamp``, a ``type``, an
``ordinal`` and a ``payload``; turns are ``task_started`` .. ``task_complete``
(or ``turn_aborted``) with whole-second times, and each item is logged once, as
an ``item_completed`` event with its own ``started_at_ms`` and
``completed_at_ms``, when it completes. A subagent's rollout starts with its own
``session_meta`` and a copy of the parent's history (see :meth:`Rollout.child`).

:func:`build` writes the conformance home: a session with two subagents (one
marks its own issue) and an older rollout without items. The expected seconds,
worked out by hand, are in the comments and in ``tests/test_codex_active_time.py``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"
CANARY_COMMAND = "CANARY-COMMAND-3b8d"
CANARY_MESSAGE = "CANARY-MESSAGE-e61f"

CWD = f"/work/{CANARY_PATH}/svc"
MODEL = "gpt-5-codex"
MINI = "gpt-5-mini"
PARENT = "c0dec0de-0000-4000-8000-000000000001"
CHILD_UNMARKED = "c0dec0de-0000-4000-8000-000000000002"
CHILD_MARKED = "c0dec0de-0000-4000-8000-000000000003"
OLD = "c0dec0de-0000-4000-8000-000000000004"


def at(when: str) -> datetime:
    """``HH:MM:SS[.fff]`` on 2026-11-05, or a full ISO 8601 time, as an aware UTC datetime."""
    text = when if "T" in when else f"2026-11-05T{when}"
    return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _ms(moment: datetime) -> int:
    return round(moment.timestamp() * 1000)


class Rollout:
    """The lines of one rollout file."""

    def __init__(self, session: str, *, model: str = MODEL) -> None:
        self.session = session
        self.model = model
        self.lines: list[dict[str, Any]] = []
        self.turn: str | None = None
        self.turns = 0
        self.items = 0
        self.total = [0, 0, 0, 0]
        self.parent: str | None = None
        self.started: datetime | None = None

    # -- writing lines -----------------------------------------------------------------------------

    def line(self, when: datetime, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {"timestamp": _iso(when), "type": kind, "ordinal": len(self.lines), "payload": payload}
        self.lines.append(entry)
        return entry

    def meta(self, when: str, *, parent: str | None = None, history_start: int | None = None) -> Rollout:
        moment = at(when)
        self.started = moment
        payload: dict[str, Any] = {
            "id": self.session,
            "session_id": parent or self.session,
            "timestamp": _iso(moment),
            "cwd": CWD,
            "originator": "codex_cli_rs",
            "cli_version": "0.80.0",
            "model_provider": "openai",
            "base_instructions": {"text": f"Notes {CANARY_PROMPT}"},
            "source": "cli",
            "git": {"branch": "main", "repository_url": "https://github.com/acme/svc.git"},
        }
        if parent is not None:
            self.parent = parent
            payload.update(
                parent_thread_id=parent,
                forked_from_id=parent,
                agent_path="/root/worker",
                agent_role="worker",
                thread_source="subagent",
                source={"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
            )
            if history_start is not None:
                payload["subagent_history_start_ordinal"] = history_start
        self.line(moment, "session_meta", payload)
        self.line(moment, "turn_context", {"cwd": CWD, "model": self.model})
        return self

    def child(self, parent: Rollout, when: str) -> Rollout:
        """Start a subagent's rollout: its own ``session_meta``, then a copy of *parent*'s lines so far.

        The copied lines keep their payloads (the parent's turn ids and times) but are written with
        the child's start as their ``timestamp``, as Codex does. Token counts and items are not copied.
        """
        moment = at(when)
        copied = [
            line
            for line in parent.lines
            if line["type"] != "event_msg" or line["payload"].get("type") not in ("token_count", "item_completed")
        ]
        self.meta(when, parent=parent.session, history_start=len(copied) + 1)
        del self.lines[-1]  # the turn_context comes after the copy
        for line in copied:
            self.line(moment, line["type"], json.loads(json.dumps(line["payload"])))
        self.line(moment, "turn_context", {"cwd": CWD, "model": self.model})
        return self

    def prompt(self, when: str, *, trigger: str | None = "user", exact: str | None = None) -> Rollout:
        """A ``task_started`` at whole second *when*, its line written at *exact* (default: *when*)."""
        moment = at(when)
        self.turns += 1
        self.turn = f"{self.session[-4:]}-turn-{self.turns}"
        attribution = None if trigger is None else {"turn_trigger": trigger}
        self.line(
            at(exact or when),
            "event_msg",
            {
                "type": "task_started",
                "turn_id": self.turn,
                "started_at": int(moment.timestamp()),
                "turn_attribution": attribution,
                "model_context_window": 272000,
            },
        )
        self.line(at(exact or when), "event_msg", {"type": "user_message", "message": f"Fix it {CANARY_PROMPT}"})
        return self

    def end(self, when: str, *, aborted: bool = False, exact: str | None = None, no_time: bool = False) -> Rollout:
        """A ``task_complete`` (or ``turn_aborted``) at whole second *when*, written at *exact*."""
        moment = at(when)
        payload: dict[str, Any] = {
            "type": "turn_aborted" if aborted else "task_complete",
            "turn_id": self.turn,
            "completed_at": None if no_time else int(moment.timestamp()),
            "duration_ms": 1,
        }
        if aborted:
            payload["reason"] = "interrupted"
        else:
            payload["last_agent_message"] = f"Done {CANARY_MESSAGE}"
        self.line(at(exact or when), "event_msg", payload)
        return self

    def item(self, start: str, end: str, item_type: str, **fields: Any) -> Rollout:
        """An ``item_completed`` line, written when the item ended."""
        self.items += 1
        item = {"type": item_type, "id": fields.pop("item_id", f"{self.session[-4:]}-item-{self.items}"), **fields}
        payload = {
            "type": "item_completed",
            "thread_id": self.session,
            "turn_id": self.turn,
            "started_at_ms": _ms(at(start)),
            "completed_at_ms": _ms(at(end)),
            "content_token_count": 12,
            "item": item,
        }
        self.line(at(end), "event_msg", payload)
        return self

    def reasoning(self, start: str, end: str) -> Rollout:
        return self.item(start, end, "Reasoning", summary_text=[f"Think {CANARY_MESSAGE}"], raw_content=[])

    def message(self, start: str, end: str) -> Rollout:
        return self.item(start, end, "AgentMessage", content=[{"text": f"Here {CANARY_MESSAGE}"}], phase="commentary")

    def command(self, start: str, end: str, **fields: Any) -> Rollout:
        values = {
            "command": f"make test {CANARY_COMMAND}",
            "cwd": CWD,
            "aggregated_output": f"ok {CANARY_OUTPUT}",
            "exit_code": 0,
            "status": "completed",
            **fields,
        }
        return self.item(start, end, "CommandExecution", **values)

    def file_change(self, start: str, end: str) -> Rollout:
        changes = {f"{CWD}/main.py": {"type": "update", "unified_diff": f"+{CANARY_OUTPUT}"}}
        return self.item(start, end, "FileChange", changes=changes, status="completed")

    def mcp(self, start: str, end: str, tool: str, arguments: dict[str, Any], text: str = "") -> Rollout:
        result = {"content": [{"type": "text", "text": text or f"ok {CANARY_OUTPUT}"}], "isError": False}
        return self.item(
            start,
            end,
            "McpToolCall",
            server="specivo",
            tool=tool,
            arguments=arguments,
            status="completed",
            result=result,
        )

    def mark(self, start: str, end: str, issue: str) -> Rollout:
        arguments = {"issue_ref": issue, "status_id": 2, "notes": f"Start {CANARY_PROMPT}"}
        return self.mcp(start, end, "specivo_update_issue", arguments, f"Updated issue {issue}: {CANARY_OUTPUT}")

    def wait(self, start: str, end: str, tool: str = "wait") -> Rollout:
        return self.item(start, end, "CollabAgentToolCall", tool=tool, status="completed", receiver_thread_ids=[])

    def usage(self, when: str, input_tokens: int = 1000, output_tokens: int = 100, *, repeat: bool = False) -> Rollout:
        """A ``token_count`` with the running total grown by the given tokens (or repeated as it was)."""
        if not repeat:
            self.total = [self.total[0] + input_tokens, self.total[1], self.total[2] + output_tokens, self.total[3]]
        names = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        total = dict(zip(names, self.total, strict=True))
        self.line(at(when), "event_msg", {"type": "token_count", "info": {"total_token_usage": total}})
        return self

    def write(self, home: Path, *, archived: bool = False) -> Path:
        started = self.started or at("00:00:00")
        name = f"rollout-{started.strftime('%Y-%m-%dT%H-%M-%S')}-{self.session}.jsonl"
        folder = home / ".codex" / ("archived_sessions" if archived else "sessions/2026/11/05")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in self.lines), encoding="utf-8")
        return path


def session_with_subagents(home: Path) -> tuple[Rollout, Rollout, Rollout]:
    """A parent session that marks ACME-40 and waits for two subagents; the second marks ACME-41.

    Parent, earlier: prompt 12:50:00, reasoning ..12:50:30, request 12:50:29, end 12:50:31, an
    old-form marking call for ACME-99, then a turn 12:55:00..12:55:10 that is aborted: 31 + 10 = 41 s,
    under ACME-99 (the 10 s go to the last request before them).

    Parent, then: prompt 13:00:00, mark 13:00:05..13:00:06, request 13:00:09, wait 13:00:10..13:20:10
    (a subagent wait: nothing, and its 20 minutes are no short gap), reasoning 13:20:10..13:20:20,
    request 13:20:21, end 13:20:22. Active: 13:00:00..13:00:10 (10) + 13:20:10..13:20:22 (12) = 22 s.

    Unmarked subagent: prompt 13:01:00, reasoning ..13:02:00, command 13:02:00..13:05:00, request
    13:05:01, message 13:05:01..13:05:10, request 13:05:11, end 13:05:12: 13:01:00..13:05:12 = 252 s, under ACME-40 (the
    parent's issue at its start). The copied parent history (two turns from 12:50:00 and an old-form
    marking call for ACME-99) adds nothing.

    Marked subagent (on another model): prompt 13:02:00, mark ACME-41 13:02:05..13:02:06, reasoning ..13:03:00, request
    13:03:01, end 13:03:02: 62 s, under ACME-41.
    """
    parent = Rollout(PARENT).meta("12:50:00")
    # Earlier turns, which the subagents' rollouts copy.
    parent.prompt("12:50:00").reasoning("12:50:00", "12:50:30").usage("12:50:29", 100, 10).end("12:50:31")
    parent.line(
        at("12:50:31"),
        "response_item",
        {
            "type": "function_call",
            "name": "specivo_update_issue",
            "namespace": "specivo",
            "arguments": json.dumps({"issue_ref": "ACME-99", "status_id": 2}),
            "call_id": "call-old-form",
        },
    )
    parent.prompt("12:55:00").end("12:55:10", aborted=True)
    unmarked = Rollout(CHILD_UNMARKED).child(parent, "13:01:00")
    marked = Rollout(CHILD_MARKED, model=MINI).child(parent, "13:02:00")

    parent.prompt("13:00:00").mark("13:00:05", "13:00:06", "ACME-40").usage("13:00:09")
    parent.wait("13:00:10", "13:20:10")
    parent.reasoning("13:20:10", "13:20:20").usage("13:20:21", 2000, 200).end("13:20:22")

    unmarked.prompt("13:01:00").reasoning("13:01:00", "13:02:00").command("13:02:00", "13:05:00")
    unmarked.usage("13:05:01", 300, 30).message("13:05:01", "13:05:10").usage("13:05:11", 400, 40).end("13:05:12")

    marked.prompt("13:02:00").mark("13:02:05", "13:02:06", "ACME-41").reasoning("13:02:06", "13:03:00")
    marked.usage("13:03:01", 500, 50).end("13:03:02")
    for rollout in (parent, unmarked, marked):
        rollout.write(home)
    return parent, unmarked, marked


def old_format(home: Path) -> Rollout:
    """A rollout written before items were logged: turns and token counts only (no active time)."""
    old = Rollout(OLD).meta("2026-11-04T10:00:00.000Z")
    old.prompt("2026-11-04T10:00:00.000Z").usage("2026-11-04T10:00:30.000Z", 700, 70)
    old.line(at("2026-11-04T10:00:31.000Z"), "response_item", {"type": "message", "content": CANARY_MESSAGE})
    old.end("2026-11-04T10:00:40.000Z")
    old.write(home)
    return old


def build(home: Path) -> Path:
    """The conformance home: the session with two subagents and an older rollout."""
    session_with_subagents(home)
    old_format(home)
    for number, path in enumerate(sorted((home / ".codex").rglob("*.jsonl"))):
        os.utime(path, (1_790_000_000 + number, 1_790_000_000 + number))
    return home
