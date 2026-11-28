"""Builds synthetic Claude Code transcripts and Codex rollouts for work activity summaries.

Everything is invented. The CANARY-* strings are planted wherever content could
hide: prompts, command arguments, environment assignments, edited file paths,
file contents, patch text, tool output and MCP arguments. None of them may reach
an activity payload, the console or the state database (the working directory,
which the state keeps by design for enrichment, holds no canary here).

The numbers every record must hold are worked out by hand in the docstrings and
checked in ``tests/test_work_activity.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.fixtures.active_time_home import Transcript
from tests.fixtures.codex_time_home import Rollout

CANARY_PROMPT = "CANARY-PROMPT-5c1e"
CANARY_OUTPUT = "CANARY-TOOL-OUTPUT-9d07"
CANARY_ARG = "CANARY-ARG-41d2"
CANARY_ENV = "CANARY-ENV-88a0"
CANARY_FILE = "CANARY-FILE-0b6e"
CANARY_CONTENT = "CANARY-CONTENT-e3f9"
CANARY_PATCH = "CANARY-PATCH-7c21"
CANARY_MCP = "CANARY-MCP-2a5d"
CANARIES = (CANARY_PROMPT, CANARY_OUTPUT, CANARY_ARG, CANARY_ENV, CANARY_FILE, CANARY_CONTENT, CANARY_PATCH, CANARY_MCP)

SESSION = "b0b0b0b0-0000-4000-8000-000000000001"
CODEX_SESSION = "c0ffee00-0000-4000-8000-000000000001"
CODEX_CHILD = "c0ffee00-0000-4000-8000-000000000002"
APP = "/work/app"
SOURCE = f"{APP}/src/{CANARY_FILE}.py"
NEW_FILE = f"{APP}/src/{CANARY_FILE}-new.py"


class Session(Transcript):
    """A Claude Code transcript whose tool results can fail and carry an edit's ``toolUseResult``."""

    def tool_result(
        self, when: str, call: str, *, error: bool = False, result: dict[str, Any] | str | None = None
    ) -> Session:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call, "content": f"out {CANARY_OUTPUT}"}
        if error:
            block["is_error"] = True
        line: dict[str, Any] = {**self._base("user", when), "message": {"role": "user", "content": [block]}}
        line["permissionDecision"] = {"decision": "accept", "source": "config"}
        line["toolUseResult"] = result if result is not None else {"stdout": CANARY_OUTPUT}
        self.lines.append(line)
        return self

    def _base(self, kind: str, when: str) -> dict[str, Any]:
        line = super()._base(kind, when)
        line["cwd"] = APP
        return line


def _edit(path: str, lines: list[str]) -> dict[str, Any]:
    hunk = {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2, "lines": lines}
    return {
        "filePath": path,
        "oldString": f"old {CANARY_CONTENT}",
        "newString": f"new {CANARY_CONTENT}",
        "originalFile": f"original {CANARY_CONTENT}",
        "structuredPatch": [hunk],
        "userModified": False,
        "replaceAll": False,
    }


def claude_main() -> Session:
    """The main thread. Day 1 (2026-11-05), one turn 10:00:00 .. 10:02:21, all of it active (no gap over 600 s):

    - Bash ``cd x && FOO=... uv run pytest -k ... | tee log`` 10:00:05 .. 10:01:05, failed: test, 60 s, failed;
    - Edit of SOURCE 10:01:10 .. 10:01:12, patch ``+ - space``: edit 2 s, +1 -1;
    - Edit of SOURCE again 10:01:20 .. 10:01:21, patch ``+ +``: edit 1 s, +2 (still one file);
    - Write of NEW_FILE (created, 4 lines) 10:01:30 .. 10:01:31: edit 1 s, +4;
    - Bash ``git commit -m ...`` 10:01:40 .. 10:01:42: git 2 s;
    - Bash ``ls ...`` 10:01:50 .. 10:01:51, failed: shell 1 s (a failed shell run is no failed test);
    - mcp__notes__save 10:02:00 .. 10:02:03: mcp 3 s;
    - Grep 10:02:10 .. 10:02:11: search 1 s;
    - an answer at 10:02:20 and the turn's end at 10:02:21.

    Totals: test 1/60, edit 3/4, git 1/2, shell 1/1, mcp 1/3, search 1/1; 2 files changed, +7 -1;
    test runs 1, failed 1.

    Timeline: labelled pieces give test 10:00:00-10:01:10 (70 s; the 5 s before the run take its label),
    edit 10:01:10-10:01:40 (30), git 10:01:40-10:01:50 (10), shell -10:02:00 (10), mcp -10:02:10 (10),
    search -10:02:21 (11). Segments under 30 s fold into their longer neighbour, shortest first: git, shell
    and mcp into edit, then search into edit: test 10:00:00-10:01:10, edit 10:01:10-10:02:21.

    A second turn crosses midnight: prompt 23:59:50, an Agent call 23:59:55 .. 00:00:05 (an async launch:
    a subagent run of 10 s that starts a subagent), an answer at 00:00:10, the turn's end at 00:00:11.
    The run belongs to the request owning its start (the answer at 23:59:55, day 1), so day 1 also gets
    subagent 1/10, 1 subagent spawned, and the piece 23:59:50-23:59:55 (5 s, no run: other). Day 2 gets the
    pieces 23:59:55 .. 00:00:11 (owned by the answer at 00:00:10), covered by the Agent run at first:
    one segment subagent 23:59:55-00:00:11, and no runs of its own (categories {}).
    """
    main = Session(SESSION)
    main.prompt("2026-11-05T10:00:00.000Z", origin="human")
    command = f"cd x && FOO={CANARY_ENV} uv run pytest -k {CANARY_ARG} | tee log"
    main.answer("2026-11-05T10:00:05.000Z", tool="Bash", call="t-test", arguments={"command": command})
    main.tool_result("2026-11-05T10:01:05.000Z", "t-test", error=True, result=f"Exit 1 {CANARY_OUTPUT}")
    edit = {"file_path": SOURCE, "old_string": CANARY_CONTENT, "new_string": CANARY_PATCH}
    main.answer("2026-11-05T10:01:10.000Z", tool="Edit", call="t-e1", arguments=edit)
    main.tool_result("2026-11-05T10:01:12.000Z", "t-e1", result=_edit(SOURCE, [f"+{CANARY_PATCH}", "-b", " c"]))
    main.answer("2026-11-05T10:01:20.000Z", tool="Edit", call="t-e2", arguments=edit)
    main.tool_result("2026-11-05T10:01:21.000Z", "t-e2", result=_edit(SOURCE, ["+x", f"+{CANARY_PATCH}"]))
    content = f"one\ntwo\nthree\n{CANARY_CONTENT}"
    main.answer("2026-11-05T10:01:30.000Z", tool="Write", call="t-w", arguments={"file_path": NEW_FILE})
    created: dict[str, Any] = {
        "type": "create",
        "filePath": NEW_FILE,
        "content": content,
        "structuredPatch": [],
        "originalFile": None,
    }
    main.tool_result("2026-11-05T10:01:31.000Z", "t-w", result=created)
    git = {"command": f'git commit -m "{CANARY_ARG}"'}
    main.answer("2026-11-05T10:01:40.000Z", tool="Bash", call="t-git", arguments=git)
    main.tool_result("2026-11-05T10:01:42.000Z", "t-git")
    main.answer("2026-11-05T10:01:50.000Z", tool="Bash", call="t-ls", arguments={"command": f"ls {CANARY_ARG}"})
    main.tool_result("2026-11-05T10:01:51.000Z", "t-ls", error=True, result=f"no such file {CANARY_OUTPUT}")
    mcp = {"text": CANARY_MCP, "path": SOURCE}
    main.answer("2026-11-05T10:02:00.000Z", tool="mcp__notes__save", call="t-mcp", arguments=mcp)
    main.tool_result("2026-11-05T10:02:03.000Z", "t-mcp")
    main.answer("2026-11-05T10:02:10.000Z", tool="Grep", call="t-grep", arguments={"pattern": CANARY_ARG})
    main.tool_result("2026-11-05T10:02:11.000Z", "t-grep")
    main.answer("2026-11-05T10:02:20.000Z")
    main.turn_end("2026-11-05T10:02:21.000Z")
    main.prompt("2026-11-05T23:59:50.000Z", origin="human")
    agent = {"prompt": CANARY_PROMPT, "subagent_type": "worker"}
    main.answer("2026-11-05T23:59:55.000Z", tool="Agent", call="t-agent", arguments=agent)
    main.tool_result("2026-11-06T00:00:05.000Z", "t-agent", result={"status": "async_launched", "isAsync": True})
    main.answer("2026-11-06T00:00:10.000Z")
    main.turn_end("2026-11-06T00:00:11.000Z")
    return main


def claude_subagent() -> Session:
    """The subagent (day 2): prompt 00:00:06, Read 00:00:07 .. 00:00:08, answer 00:00:09.

    read 1/1; timeline: one chain 00:00:06 .. 00:00:09, all labelled read (3 s, alone, kept).
    """
    sub = Session(SESSION, agent="w0rker01")
    sub.prompt("2026-11-06T00:00:06.000Z")
    sub.answer("2026-11-06T00:00:07.000Z", tool="Read", call="t-read", arguments={"file_path": SOURCE})
    sub.tool_result("2026-11-06T00:00:08.000Z", "t-read", result={"file": {"content": CANARY_CONTENT}})
    sub.answer("2026-11-06T00:00:09.000Z")
    return sub


def build_claude(home: Path) -> Path:
    for transcript in (claude_main(), claude_subagent()):
        transcript.write(home)
    return home


def codex_parent() -> Rollout:
    """The Codex parent session (2026-11-05), one turn 14:00:00 .. 14:05:00, all active:

    - a command ``zsh -lc "cd x && CI=... npm test -- ..."`` (parsed unknown) 14:00:10 .. 14:01:10, exit 1:
      test 60 s, failed;
    - a command ``cat ...`` (parsed read) 14:01:20 .. 14:01:21: read 1 s;
    - a command ``rg ...`` (parsed search) 14:01:30 .. 14:01:32: search 2 s;
    - a FileChange 14:02:00 .. 14:02:01: add (3 lines), update (+2 -1, headers not counted), delete:
      edit 1 s, 3 files, +5 -1;
    - an MCP call 14:02:10 .. 14:02:14 that failed: mcp 4 s;
    - a ``SubAgentActivity`` started at 14:02:20 (a spawn note, no run: 1 subagent spawned) and a
      collaboration wait 14:02:30 .. 14:04:30 (subagent, 0 s: a wait counts nothing);
    - a web search (Extension) 14:04:40 .. 14:04:45: web 5 s;
    - reasoning 14:04:45 .. 14:04:58, a request at 14:04:59, the end at 14:05:00.

    The 120 s of the wait are a gap under 600 s, so the active set is the whole turn (300 s).

    Totals: test 1/60 (failed), read 1/1, search 1/2, edit 1/1, mcp 1/4, subagent 1/0, web 1/5;
    3 files changed, +5 -1; test runs 1, failed 1; 1 subagent spawned.

    Timeline: test 14:00:00-14:01:20 (80 s), read -14:01:30 (10), search -14:02:00 (30), edit -14:02:10
    (10), mcp -14:02:30 (20; the spawn note is no event, so no cut), subagent -14:04:40 (130), web -14:05:00
    (20). Folding, shortest first: read into test (80 > 30), edit into search (30 > 20), mcp into subagent
    (130 > 40), web into subagent: test 14:00:00-14:01:30, search -14:02:10, subagent -14:05:00.
    """
    r = Rollout(CODEX_SESSION).meta("13:59:59")
    r.prompt("14:00:00")
    script = f"cd x && CI={CANARY_ENV} npm test -- {CANARY_ARG}"
    r.command(
        "14:00:10",
        "14:01:10",
        command=["/bin/zsh", "-lc", script],
        parsed_cmd=[{"type": "unknown", "cmd": script}],
        exit_code=1,
        status="failed",
    )
    r.command(
        "14:01:20",
        "14:01:21",
        command=["/bin/zsh", "-lc", f"cat {CANARY_FILE}"],
        parsed_cmd=[{"type": "read", "cmd": f"cat {CANARY_FILE}", "name": CANARY_FILE, "path": SOURCE}],
    )
    r.command(
        "14:01:30",
        "14:01:32",
        command=["/bin/zsh", "-lc", f"rg {CANARY_ARG}"],
        parsed_cmd=[{"type": "search", "cmd": f"rg {CANARY_ARG}", "query": CANARY_ARG}],
    )
    diff = f"--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n-{CANARY_PATCH}\n+one\n+{CANARY_PATCH}\n same\n"
    changes = {
        NEW_FILE: {"type": "add", "content": f"a\nb\n{CANARY_CONTENT}\n"},
        SOURCE: {"type": "update", "unified_diff": diff, "move_path": None},
        f"{APP}/{CANARY_FILE}-gone.py": {"type": "delete", "content": CANARY_CONTENT},
    }
    r.item("14:02:00", "14:02:01", "FileChange", changes=changes, status="completed", stdout="", stderr="")
    r.item(
        "14:02:10",
        "14:02:14",
        "McpToolCall",
        server="notes",
        tool="save",
        arguments={"text": CANARY_MCP},
        status="failed",
        result=None,
        error={"message": CANARY_OUTPUT},
    )
    r.item("14:02:20", "14:02:20", "SubAgentActivity", kind="started", agent_thread_id=CODEX_CHILD, agent_path="/w")
    r.wait("14:02:30", "14:04:30")
    r.item("14:04:40", "14:04:45", "Extension", kind="web_search", query=CANARY_ARG, results=[CANARY_OUTPUT])
    r.reasoning("14:04:45", "14:04:58").usage("14:04:59").end("14:05:00")
    return r


def build_codex(home: Path) -> Path:
    codex_parent().write(home)
    return home
