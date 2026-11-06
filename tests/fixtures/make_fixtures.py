"""Writes the synthetic fixtures under tests/fixtures/. Run from the repository root:

    uv run python tests/fixtures/make_fixtures.py

Everything here is invented: ids, paths, prompts and numbers. The CANARY-*
strings are planted in prompts, tool output, paths and credentials; the
conformance kit checks that none of them ever leaves the reporter.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_PATH = "CANARY-PATH-7f3a"
CANARY_SECRET = "CANARY-SECRET-b2e4"

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "11111111-2222-4111-8111-111111111111"
CWD = f"/work/{CANARY_PATH}/app"
PROJECT_DIR = "-work-" + CANARY_PATH + "-app"


def _write(path: Path, lines: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n" for line in lines)
    path.write_text(text, encoding="utf-8")


def _assistant(
    session: str,
    ts: str,
    msg: str | None,
    req: str | None,
    model: str,
    usage: dict[str, Any],
    *,
    sidechain: bool = False,
    branch: str = "main",
    text: str = "Done.",
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "assistant",
        "isSidechain": sidechain,
        "sessionId": session,
        "cwd": CWD,
        "gitBranch": branch,
        "version": "2.0.30",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": usage,
        },
    }
    if msg is not None:
        line["message"]["id"] = msg
    if req is not None:
        line["requestId"] = req
    return line


def _user(session: str, ts: str, content: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "cwd": CWD,
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }


def claude() -> None:
    root = HERE / "claude_code"
    shutil.rmtree(root, ignore_errors=True)
    projects = root / "home" / ".claude" / "projects" / PROJECT_DIR
    split = {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 500}
    _write(
        projects / f"{SESSION_A}.jsonl",
        [
            _user(SESSION_A, "2026-10-05T23:40:00.000Z", f"สวัสดี ช่วยแก้บั๊กนี้หน่อย {CANARY_PROMPT}"),
            # One response written twice (two content blocks); the larger usage is final.
            _assistant(
                SESSION_A,
                "2026-10-05T23:41:00.000Z",
                "msg_A1",
                "req_A1",
                "claude-sonnet-4-5",
                {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1500,
                    "cache_creation": split,
                },
            ),
            _assistant(
                SESSION_A,
                "2026-10-05T23:41:01.000Z",
                "msg_A1",
                "req_A1",
                "claude-sonnet-4-5",
                {
                    "input_tokens": 10,
                    "output_tokens": 120,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1500,
                    "cache_creation": split,
                },
            ),
            _user(
                SESSION_A,
                "2026-10-05T23:42:00.000Z",
                [{"type": "tool_result", "tool_use_id": "toolu_1", "content": f"ls output {CANARY_OUTPUT}"}],
            ),
            # The same session goes on past midnight UTC: a second bucket.
            _assistant(
                SESSION_A,
                "2026-10-06T00:05:00.000Z",
                "msg_A2",
                "req_A2",
                "claude-sonnet-4-5",
                {"input_tokens": 30, "output_tokens": 60, "cache_read_input_tokens": 3500},
            ),
            # A response with an advisor call under its own model.
            _assistant(
                SESSION_A,
                "2026-10-06T00:06:00.000Z",
                "msg_A3",
                "req_A3",
                "claude-sonnet-4-5",
                {
                    "input_tokens": 7,
                    "output_tokens": 70,
                    "cache_read_input_tokens": 4000,
                    "iterations": [
                        {"type": "message", "model": None, "input_tokens": 7, "output_tokens": 70},
                        {
                            "type": "advisor_message",
                            "model": "claude-opus-4-6",
                            "input_tokens": 900,
                            "output_tokens": 40,
                            "cache_read_input_tokens": 100,
                        },
                    ],
                },
            ),
            # A locally generated message: no tokens, not a model request.
            _assistant(
                SESSION_A,
                "2026-10-06T00:07:00.000Z",
                "msg_local",
                None,
                "<synthetic>",
                {"input_tokens": 0, "output_tokens": 0},
            ),
            # A gateway-style id without requestId, written twice in the same session.
            _assistant(
                SESSION_A,
                "2026-10-06T00:08:00.000Z",
                "gw-1",
                None,
                "claude-sonnet-4-5",
                {"input_tokens": 11, "output_tokens": 22},
            ),
            _assistant(
                SESSION_A,
                "2026-10-06T00:08:00.500Z",
                "gw-1",
                None,
                "claude-sonnet-4-5",
                {"input_tokens": 11, "output_tokens": 22},
            ),
            "{this line is not json",
            [1, 2, 3],
        ],
    )
    # A subagent transcript of session A: one replay of a parent message, one own response.
    _write(
        projects / SESSION_A / "subagents" / "agent-a1b2c3.jsonl",
        [
            _user(SESSION_A, "2026-10-06T00:06:30.000Z", f"Find the failing test {CANARY_PROMPT}"),
            _assistant(
                SESSION_A,
                "2026-10-06T00:06:31.000Z",
                "msg_A2",
                "req_replay",
                "claude-sonnet-4-5",
                {"input_tokens": 30, "output_tokens": 60, "cache_read_input_tokens": 3500},
                sidechain=True,
            ),
            _assistant(
                SESSION_A,
                "2026-10-06T00:06:40.000Z",
                "msg_S1",
                "req_S1",
                "claude-haiku-4-5",
                {"input_tokens": 400, "output_tokens": 80},
                sidechain=True,
            ),
        ],
    )
    # A resumed session: its file repeats msg_A1 from session A, then works on its own.
    _write(
        projects / f"{SESSION_B}.jsonl",
        [
            _assistant(
                SESSION_B,
                "2026-10-05T23:41:01.000Z",
                "msg_A1",
                "req_A1",
                "claude-sonnet-4-5",
                {
                    "input_tokens": 10,
                    "output_tokens": 120,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1500,
                    "cache_creation": split,
                },
            ),
            _assistant(
                SESSION_B,
                "2026-10-07T03:00:00.000Z",
                "msg_B1",
                "req_B1",
                "claude-sonnet-4-5",
                {"input_tokens": 50, "output_tokens": 500, "cache_creation_input_tokens": 800},
                branch="feat/acme-12-login",
                text=f"Edited {CWD}/src/login.py",
            ),
        ],
    )


def _codex_line(ts: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": ts, "type": kind, "payload": payload}


def _tokens(i: int, c: int, o: int, r: int) -> dict[str, int]:
    return {
        "input_tokens": i,
        "cached_input_tokens": c,
        "output_tokens": o,
        "reasoning_output_tokens": r,
        "total_tokens": i + o,
    }


def _count(ts: str, total: dict[str, int] | None, last: dict[str, int] | None) -> dict[str, Any]:
    info = None if total is None and last is None else {"total_token_usage": total, "last_token_usage": last}
    if info is not None:
        info = {k: v for k, v in info.items() if v is not None}
    return _codex_line(ts, "event_msg", {"type": "token_count", "info": info})


def codex() -> None:
    root = HERE / "codex"
    shutil.rmtree(root, ignore_errors=True)
    codex_home = root / "home" / ".codex"
    s1 = "22222222-2222-4222-8222-222222222222"
    s2 = "33333333-3333-4333-8333-333333333333"
    cwd = f"/work/{CANARY_PATH}/svc"
    meta = {
        "id": s1,
        "timestamp": "2026-10-05T08:00:00.000Z",
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "cli_version": "0.50.0",
        "instructions": f"Project notes {CANARY_PROMPT}",
        "git": {
            "commit_hash": "0123456789abcdef0123456789abcdef01234567",
            "branch": "feat/acme-7-search",
            "repository_url": f"https://ci-bot:{CANARY_SECRET}@github.com/acme/app.git",
        },
    }
    active = [
        _codex_line("2026-10-05T08:00:00.000Z", "session_meta", meta),
        _codex_line(
            "2026-10-05T08:00:01.000Z",
            "turn_context",
            {"cwd": cwd, "model": "gpt-5-codex", "approval_policy": "on-request"},
        ),
        _codex_line(
            "2026-10-05T08:00:02.000Z",
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"แก้ไขการค้นหา {CANARY_PROMPT}"}],
            },
        ),
        _codex_line(
            "2026-10-05T08:00:05.000Z",
            "response_item",
            {"type": "function_call_output", "call_id": "call_1", "output": f"{CANARY_OUTPUT} {cwd}"},
        ),
        _count("2026-10-05T08:00:10.000Z", _tokens(1000, 200, 300, 100), _tokens(1000, 200, 300, 100)),
        # The same snapshot again: nothing new.
        _count("2026-10-05T08:00:11.000Z", _tokens(1000, 200, 300, 100), _tokens(1000, 200, 300, 100)),
        _count("2026-10-05T08:01:00.000Z", _tokens(2500, 1200, 500, 150), _tokens(1500, 1000, 200, 50)),
        _codex_line("2026-10-05T08:02:00.000Z", "turn_context", {"cwd": cwd, "model": "gpt-5"}),
        # After a compaction the running total starts again: the last request counts.
        _count("2026-10-05T08:03:00.000Z", _tokens(800, 100, 90, 10), _tokens(800, 100, 90, 10)),
        _count("2026-10-05T08:04:00.000Z", None, None),
        "not json at all",
    ]
    _write(codex_home / "sessions" / "2026" / "10" / "05" / f"rollout-2026-10-05T08-00-00-{s1}.jsonl", active)
    # An older archived copy of the same session: the active one wins.
    _write(codex_home / "archived_sessions" / f"rollout-2026-10-05T08-00-00-{s1}.jsonl", active[:5])
    # An archived session with running totals only.
    _write(
        codex_home / "archived_sessions" / f"rollout-2026-10-04T10-00-00-{s2}.jsonl",
        [
            _codex_line(
                "2026-10-04T10:00:00.000Z",
                "session_meta",
                {"id": s2, "cwd": f"/work/{CANARY_PATH}/tools", "cli_version": "0.49.0"},
            ),
            _codex_line("2026-10-04T10:00:01.000Z", "turn_context", {"model": "gpt-5-codex"}),
            _count("2026-10-04T10:00:10.000Z", _tokens(100, 0, 50, 0), None),
            _count("2026-10-04T10:00:20.000Z", _tokens(300, 50, 120, 20), None),
        ],
    )
    # Fixed modification times so discovery order is stable.
    for number, path in enumerate(sorted(codex_home.rglob("*.jsonl"))):
        os.utime(path, (1_790_000_000 + number, 1_790_000_000 + number))


GEMINI_MAIN = "44444444-4444-4444-8444-444444444444"
GEMINI_SUB = "55555555-5555-4555-8555-555555555555"
GEMINI_OTHER = "66666666-6666-4666-8666-666666666666"


def _gemini_message(
    message_id: str, ts: str, model: str, inp: int, out: int, cached: int, thoughts: int, tool: int
) -> dict[str, Any]:
    return {
        "id": message_id,
        "timestamp": ts,
        "type": "gemini",
        "content": [{"text": "Here is the change."}],
        "thoughts": [{"subject": "Plan", "description": f"Read {CANARY_PATH}/main.py", "timestamp": ts}],
        "toolCalls": [{"id": "call-1", "name": "read_file", "result": [{"text": CANARY_OUTPUT}]}],
        "model": model,
        "tokens": {
            "input": inp,
            "output": out,
            "cached": cached,
            "thoughts": thoughts,
            "tool": tool,
            "total": inp + out + thoughts + tool,
        },
    }


def gemini() -> None:
    """Gemini CLI chat recordings: a legacy JSON session, its JSONL copy, a subagent and a second project."""
    root = HERE / "gemini_cli"
    shutil.rmtree(root, ignore_errors=True)
    chats = root / "home" / ".gemini" / "tmp" / f"app-{CANARY_PATH}" / "chats"
    user = {"id": "u-1", "timestamp": "2026-10-05T08:00:00.000Z", "type": "user", "content": [{"text": CANARY_PROMPT}]}
    first = _gemini_message("g-1", "2026-10-05T08:00:05.000Z", "gemini-2.5-pro", 1200, 80, 1000, 40, 10)
    second = _gemini_message("g-2", "2026-10-05T23:59:30.000Z", "gemini-2.5-pro", 3000, 150, 2500, 0, 0)
    # The legacy whole-file JSON recording.
    legacy = {
        "sessionId": GEMINI_MAIN,
        "projectHash": "0f" * 32,
        "startTime": "2026-10-05T08:00:00.000Z",
        "lastUpdated": "2026-10-05T23:59:30.000Z",
        "messages": [user, first, second, {"id": "i-1", "type": "info", "content": "Saved."}],
    }
    chats.mkdir(parents=True)
    (chats / "session-2026-10-05T08-00-44444444.json").write_text(json.dumps(legacy, indent=2), encoding="utf-8")
    # The same session converted to JSONL and continued after midnight UTC.
    partial = {**first, "tokens": {"input": 1200, "output": 0, "cached": 0, "thoughts": 0, "tool": 0, "total": 1200}}
    _write(
        chats / "session-2026-10-05T08-00-44444444.jsonl",
        [
            {
                "sessionId": GEMINI_MAIN,
                "projectHash": "0f" * 32,
                "startTime": "2026-10-05T08:00:00.000Z",
                "lastUpdated": "2026-10-05T08:00:00.000Z",
                "kind": "main",
            },
            user,
            partial,  # written again below in full: the last copy wins
            first,
            {"$set": {"lastUpdated": "2026-10-05T08:00:05.000Z", "summary": CANARY_PROMPT}},
            {"$patch": {"id": "g-1", "toolCalls": [{"id": "call-1", "result": [{"text": CANARY_OUTPUT}]}]}},
            second,
            "{this line is not JSON",
            {"$rewindTo": "g-3"},  # rewound, but the request was made: still counted
            _gemini_message("g-3", "2026-10-06T00:00:10.000Z", "gemini-2.5-pro", 4000, 60, 3500, 20, 0),
        ],
    )
    # A subagent of that session, folded into it.
    _write(
        chats / GEMINI_MAIN / f"{GEMINI_SUB}.jsonl",
        [
            {
                "sessionId": GEMINI_SUB,
                "projectHash": "0f" * 32,
                "startTime": "2026-10-05T09:00:00.000Z",
                "kind": "subagent",
                "directories": [f"/work/{CANARY_PATH}/app"],
            },
            _gemini_message("s-1", "2026-10-05T09:00:05.000Z", "gemini-2.5-flash", 500, 20, 0, 5, 0),
        ],
    )
    # Another project, with a message that has no usage and one without a timestamp.
    other = root / "home" / ".gemini" / "tmp" / "tools" / "chats"
    no_time = _gemini_message("o-2", "", "gemini-2.5-flash", 10, 1, 0, 0, 0)
    no_time.pop("timestamp")
    _write(
        other / "session-2026-10-04T10-00-66666666.jsonl",
        [
            {"sessionId": GEMINI_OTHER, "projectHash": "1e" * 32, "startTime": "2026-10-04T10:00:00.000Z"},
            {
                "id": "o-0",
                "timestamp": "2026-10-04T10:00:00.000Z",
                "type": "gemini",
                "content": "Hi",
                "model": "gemini-2.5-flash",
            },
            _gemini_message("o-1", "2026-10-04T10:00:03.000Z", "gemini-2.5-flash", 700, 30, 0, 0, 0),
            no_time,
        ],
    )
    (other / "session-2026-10-04T10-00-66666666.jsonl.tmp-4242").write_text("{}", encoding="utf-8")


if __name__ == "__main__":
    claude()
    codex()
    gemini()
