"""Claude Code: transcripts under ``~/.claude/projects`` and ``~/.config/claude/projects``.

What the transcript format looks like, and what this adapter does with it:

- Every session is a JSONL file. Lines of type ``assistant`` carry
  ``message.usage`` for one model response, next to ``message.id``,
  ``message.model``, ``requestId``, ``sessionId``, ``isSidechain``,
  ``timestamp``, ``gitBranch``, ``cwd`` and ``version``.
- One response can be written several times: once per content block, again
  in a resumed or forked session's file, and in a subagent's transcript. The
  pair ``message.id`` + ``requestId`` names the response, so it is counted
  once, from the copy with the most tokens (the final usage of a streamed
  response is the largest).
- Subagent transcripts (``<session>/subagents/*.jsonl``) and in-file Task
  sidechains are marked ``isSidechain: true``. Their own responses are counted
  in the parent session, so a session's total includes the work it fanned out.
  A sidechain line that repeats a message of the main thread of the same
  session is a replay and is not counted again.
- ``usage.input_tokens`` already excludes cache reads and writes.
  ``usage.cache_creation`` splits cache writes into 5-minute and 1-hour ones;
  older transcripts only have ``cache_creation_input_tokens`` (5-minute).
- ``usage.iterations`` may hold extra model calls made for the response. Those
  of type ``advisor_message`` name their own model and are reported under it.
- Lines with no tokens at all (locally generated messages) are skipped.
- **Issue marks.** An ``assistant`` line's ``tool_use`` blocks that are
  marking Specivo calls (see :mod:`ai_usage_reporter.issue_marks`) become
  markers at the line's time; ``specivo_create_issue`` takes the created key
  from the ``tool_result`` block with the same ``tool_use_id`` in a later
  ``user`` line. Only the tool name, the few arguments that decide the mark
  and the issue key are read. A subagent's markers and usage carry its
  transcript's name as their ``thread``.

- **Active time.** :meth:`ClaudeCodeAdapter.parse_events` also yields the
  thread events :mod:`ai_usage_reporter.activity` measures. Each thread is the
  main conversation of a session, one subagent transcript (named after its
  file, like the markers) or an in-file sidechain (``agent-<agentId>``):

  - ``prompt``: a ``user`` line without a ``tool_result`` block. A line with
    ``isMeta`` is a prompt only when its ``origin.kind`` names who started the
    turn (background notifications and subagent hand-backs are written as
    ``isMeta`` lines with an origin); other ``isMeta`` lines (injected context)
    are no events;
  - ``model``: every ``assistant`` line;
  - ``tool_start``: a ``tool_use`` block (its ``id`` and, to classify it, its
    ``name``); ``tool_end``: a ``tool_result`` block (its ``tool_use_id``);
  - ``turn_end``: a ``system`` line of subtype ``turn_duration`` (its
    ``durationMs`` is not used: it covers background waits).

  ``AskUserQuestion`` and ``ExitPlanMode`` wait for a person. An ``Agent`` or
  ``Task`` call whose result is not an async launch waits for its subagent. A
  call a person approved or rejected by hand has a ``permissionDecision``
  whose ``source`` is not ``config``, or ``toolDenialKind: user-rejected``.
  An event is named by the line's ``uuid``, so a line replayed in a resumed or
  copied file counts once. Nothing else of a line is read for this.

- **Work activity.** The same tool events carry what
  :mod:`ai_usage_reporter.work_activity` summarises, read in memory and reduced at
  once (no command, path, patch or output is kept):

  - the category, from the tool's name: ``Read`` read; ``Grep``, ``Glob``,
    ``ToolSearch`` search; ``Edit``, ``MultiEdit``, ``Write``, ``NotebookEdit``
    edit; ``Bash`` by :func:`~ai_usage_reporter.work_activity.classify_command` of
    ``input.command`` (test, git or shell); ``WebFetch``, ``WebSearch`` web;
    ``Agent``, ``Task``, ``SendMessage`` subagent; ``mcp__*`` mcp; anything else
    other. ``Agent`` and ``Task`` start a subagent;
  - failure: ``is_error`` on the ``tool_result`` block;
  - for an edit: lines added and removed from ``toolUseResult.structuredPatch``
    (the ``+`` and ``-`` lines of its hunks; a new file written by ``Write``
    counts its content's lines as added), and the file as a local hash of
    ``toolUseResult.filePath``.

- **Route.** Claude Code does not log the endpoint it called, so the route
  comes from, in this order:

  1. the host of ``ANTHROPIC_BASE_URL`` the reporter's ``SessionStart`` hook
     saw for the session (kept in the state database; exact per session). A
     session the hook saw without one goes straight to step 3;
  2. the host of ``env.ANTHROPIC_BASE_URL`` in the session directory's
     ``.claude/settings.local.json`` and ``.claude/settings.json``, then in the
     Claude config directory's ``settings.json`` (today's value, a best guess
     for older sessions). These ``env`` blocks hold API tokens: every other key
     is dropped while the file is parsed, and only the host is kept, never the
     URL or anything in it;
  3. the transcript's ``session-owner`` line (recent Claude Code versions), as
     ``claude.ai:org-<first 8 hex of the SHA-256 of organizationUuid>``, which
     tells a personal login from a company Team or Enterprise login without
     sending the organization id.

  ``provider`` stays ``anthropic``; the route never changes the price.

``CLAUDE_CONFIG_DIR`` moves the whole Claude config directory; the
``[sources.claude-code] roots`` setting lists other directories to read.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_str, iter_json_lines
from ai_usage_reporter.adapters._routes import url_host
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call
from ai_usage_reporter.model import (
    MODEL,
    PROMPT,
    TOOL_END,
    TOOL_START,
    TURN_END,
    SourceFile,
    ThreadEvent,
    TokenCounts,
    UsageEvent,
    count,
    parse_timestamp,
)
from ai_usage_reporter.state import State
from ai_usage_reporter.work_activity import classify_command, file_identity, patch_lines, text_lines

#: The environment variable that sends Claude Code to another endpoint (a gateway).
BASE_URL_ENV = "ANTHROPIC_BASE_URL"
#: Settings files are read up to this size; a bigger one is skipped.
MAX_SETTINGS_BYTES = 1024 * 1024
#: Settings files of a session directory, the one that wins first.
PROJECT_SETTINGS = (Path(".claude") / "settings.local.json", Path(".claude") / "settings.json")
#: The route of a claude.ai login: the prefix of the hashed organization id.
ORG_ROUTE_PREFIX = "claude.ai:org-"
#: Tools whose run is a person answering (active time: they count nothing).
HUMAN_WAIT_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
#: Tools that start a subagent; in the foreground the call waits for it to finish.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
#: Work activity categories by tool name (``Bash`` and ``mcp__*`` are decided apart).
TOOL_CATEGORIES = {
    "Read": "read",
    "Grep": "search",
    "Glob": "search",
    "ToolSearch": "search",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "WebFetch": "web",
    "WebSearch": "web",
    "Agent": "subagent",
    "Task": "subagent",
    "SendMessage": "subagent",
}
#: Tools whose result tells which file changed and how (``toolUseResult``).
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def tool_category(name: str | None, arguments: object) -> str:
    """The work activity category of a tool call; a ``Bash`` command is read here and dropped."""
    if name == "Bash":
        command = arguments.get("command") if isinstance(arguments, dict) else None
        return classify_command(command) if isinstance(command, str) else "shell"
    if name and name.startswith("mcp__"):
        return "mcp"
    return TOOL_CATEGORIES.get(name or "", "other")


def edit_detail(result: object) -> tuple[int, int, tuple[str, ...]]:
    """Lines added and removed, and the hashed file, of an edit's ``toolUseResult`` (nothing of it is kept)."""
    if not isinstance(result, dict):
        return 0, 0, ()
    added = removed = 0
    patch = result.get("structuredPatch")
    if isinstance(patch, list):
        for hunk in patch:
            if isinstance(hunk, dict) and isinstance(hunk.get("lines"), list):
                plus, minus = patch_lines(hunk["lines"])
                added, removed = added + plus, removed + minus
    content = result.get("content")
    if result.get("type") == "create" and not patch and isinstance(content, str):
        added = text_lines(content)
    path = result.get("filePath")
    return added, removed, (file_identity(path),) if isinstance(path, str) and path else ()


def _config_dirs(ctx: DiscoveryContext) -> list[Path]:
    if ctx.roots:
        raw = list(ctx.roots)
    elif ctx.env.get("CLAUDE_CONFIG_DIR", "").strip():
        raw = [ctx.expand(ctx.env["CLAUDE_CONFIG_DIR"].strip())]
    else:
        xdg = ctx.env.get("XDG_CONFIG_HOME", "").strip()
        raw = [(Path(xdg) if xdg else ctx.home / ".config") / "claude", ctx.home / ".claude"]
    found: list[Path] = []
    for directory in raw:
        # Accept the config directory or its projects/ directory.
        projects = directory if directory.name == "projects" else directory / "projects"
        if projects.is_dir() and projects not in found:
            found.append(projects)
    return found


def session_from_path(path: Path) -> str:
    """The session a transcript file belongs to, judged by its location.

    A file inside a ``subagents`` directory belongs to the session directory
    above it; any other file is named after its session.
    """
    parts = path.parts
    if "subagents" in parts:
        index = len(parts) - 1 - parts[::-1].index("subagents")
        if index >= 1:
            return parts[index - 1]
    return path.name.split(".", 1)[0]


def org_route(organization: str | None) -> str | None:
    """The route of a claude.ai organization: never the id itself, only a short hash of it."""
    if not organization or not organization.strip():
        return None
    return ORG_ROUTE_PREFIX + hashlib.sha256(organization.strip().encode()).hexdigest()[:8]


def _keep_base_url(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Called for every JSON object while a settings file is parsed: whatever is not
    # on the path env.ANTHROPIC_BASE_URL (tokens and keys among it) is dropped at once.
    return {key: value for key, value in pairs if key in ("env", BASE_URL_ENV)}


def settings_host(path: Path) -> str | None:
    """The host of ``env.ANTHROPIC_BASE_URL`` in a settings file, and nothing else from it."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
            return None
        data = json.loads(path.read_bytes(), object_pairs_hook=_keep_base_url)
    except (OSError, ValueError):
        return None
    env = data.get("env") if isinstance(data, dict) else None
    url = env.get(BASE_URL_ENV) if isinstance(env, dict) else None
    host = url_host(url) if isinstance(url, str) else None
    return host[:100] if host else None


def _sidechain_thread(line: dict[str, Any]) -> str | None:
    """The thread of an in-file sidechain line of a main transcript (older Claude Code), else ``None``."""
    if line.get("isSidechain") is not True:
        return None
    agent = get_str(line, "agentId")
    return f"agent-{agent}" if agent else "sidechain"


def _decided_by_person(line: dict[str, Any]) -> bool:
    """Whether a person approved or rejected the tool call of a ``tool_result`` line by hand."""
    source = get_str(line, "permissionDecision", "source")
    return (source is not None and source != "config") or line.get("toolDenialKind") == "user-rejected"


def _async_launch(line: dict[str, Any]) -> bool:
    """Whether an ``Agent``/``Task`` result only launched the subagent in the background."""
    result = line.get("toolUseResult")
    return isinstance(result, dict) and (result.get("status") == "async_launched" or result.get("isAsync") is True)


def _usage_tokens(usage: dict[str, Any]) -> TokenCounts:
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        five_minutes = count(split.get("ephemeral_5m_input_tokens"))
        one_hour = count(split.get("ephemeral_1h_input_tokens"))
    else:
        five_minutes, one_hour = count(usage.get("cache_creation_input_tokens")), 0
    return TokenCounts(
        input=count(usage.get("input_tokens")),
        output=count(usage.get("output_tokens")),
        cache_write_5m=five_minutes,
        cache_write_1h=one_hour,
        cache_read=count(usage.get("cache_read_input_tokens")),
    )


class ClaudeCodeAdapter:
    name = "claude-code"
    provider = "anthropic"
    parser_version = "claude-code/5"

    def __init__(self) -> None:
        #: Session -> the base URL host the hook saw ('' for none), for the parses of one run.
        self._hook_routes: dict[str, str] = {}

    def bind_run(self, state: State | None, now: datetime | None) -> None:
        """The routes the hook recorded, from the pipeline's state database (none after the run)."""
        self._hook_routes = state.session_routes(self.name) if state is not None else {}

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        for projects in _config_dirs(ctx):
            for path in sorted(projects.rglob("*.jsonl")):
                if path.is_file():
                    yield SourceFile(adapter=self.name, path=path, root=projects, session_hint=session_from_path(path))

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        for event in self.parse_events(src):
            if isinstance(event, UsageEvent):
                yield event

    def parse_events(self, src: SourceFile) -> Iterator[UsageEvent | ThreadEvent]:
        """Usage events and markers, and the thread events for active time, in one pass."""
        file_session = src.session_hint or session_from_path(src.path)
        is_subagent_file = "subagents" in src.path.parts
        file_thread = src.path.name.split(".", 1)[0] if is_subagent_file else None
        #: specivo_create_issue calls waiting for their result: tool_use id -> (time, session, cwd).
        creating: dict[str, tuple[Any, str, str | None]] = {}
        #: Tool names by tool_use id, to classify a tool_result for active time.
        names: dict[str, str | None] = {}
        #: Usage waits for the end of the file, where the organization line (wherever it was) is known.
        usage: list[UsageEvent] = []
        organization: str | None = None
        for line in iter_json_lines(src.path, src.stats):
            if line.get("type") == "session-owner":
                organization = organization or get_str(line, "organizationUuid")
                continue
            thread = file_thread if is_subagent_file else _sidechain_thread(line)
            for thread_event in self._thread_events(line, file_session, is_subagent_file, thread, names):
                src.stats.thread_events += 1
                yield thread_event
            message = line.get("message")
            if not isinstance(message, dict):
                continue
            kind = line.get("type")
            if kind in ("assistant", "user") and isinstance(message.get("content"), list):
                session = file_session if is_subagent_file else (get_str(line, "sessionId") or file_session)
                yield from self._marks(line, message["content"], kind, session, thread, creating)
            if not isinstance(message.get("usage"), dict):
                continue
            when = parse_timestamp(line.get("timestamp"))
            if when is None:
                src.stats.skipped_events += 1
                continue
            # A subagent file always folds into the session directory it sits in.
            session = file_session if is_subagent_file else (get_str(line, "sessionId") or file_session)
            branch = get_str(line, "gitBranch")
            common: dict[str, Any] = {
                "tool": self.name,
                "session_id": session,
                "timestamp": when,
                "provider": self.provider,
                "tool_version": get_str(line, "version"),
                "request_id": get_str(line, "requestId"),
                "is_replay": line.get("isSidechain") is True,
                "git_branch": branch if branch and branch != "HEAD" else None,
                "cwd": get_str(line, "cwd"),
                "source_path": str(src.path),
                "thread": thread,
            }
            message_id = get_str(message, "id")
            main = self._event(common, get_str(message, "model"), message_id, message["usage"])
            if main is not None:
                src.stats.events += 1
                usage.append(main)
            iterations = message["usage"].get("iterations")
            if isinstance(iterations, list):
                advisors = [i for i in iterations if isinstance(i, dict) and i.get("type") == "advisor_message"]
                for number, item in enumerate(advisors, start=1):
                    advisor_id = f"{message_id}#advisor{number}" if message_id else None
                    extra = self._event(common, get_str(item, "model"), advisor_id, item)
                    if extra is not None:
                        src.stats.events += 1
                        usage.append(extra)
        fallback = org_route(organization)
        settings: dict[str | None, str | None] = {}
        for event in usage:
            route = self._route(src, event, file_session, settings)
            yield dataclasses.replace(event, route=route or fallback) if (route or fallback) else event

    def _route(
        self, src: SourceFile, event: UsageEvent, file_session: str, settings: dict[str | None, str | None]
    ) -> str | None:
        """The gateway host for *event*: the hook's record, else the settings files (see the module docs)."""
        for session in (event.session_id, file_session):
            if session in self._hook_routes:
                return self._hook_routes[session] or None
        if event.cwd not in settings:
            settings[event.cwd] = self._settings_host(src, event.cwd)
        return settings[event.cwd]

    @staticmethod
    def _settings_host(src: SourceFile, cwd: str | None) -> str | None:
        files = [Path(cwd) / name for name in PROJECT_SETTINGS] if cwd else []
        # The root is the config directory's projects/ folder.
        files.append(src.root.parent / "settings.json")
        for path in files:
            host = settings_host(path)
            if host:
                return host
        return None

    def _thread_events(
        self,
        line: dict[str, Any],
        file_session: str,
        is_subagent_file: bool,
        thread: str | None,
        names: dict[str, str | None],
    ) -> Iterator[ThreadEvent]:
        """The thread events of one line (see the module docs); reads ids, names and the permission source only."""
        kind = line.get("type")
        if kind not in ("user", "assistant", "system"):
            return
        when = parse_timestamp(line.get("timestamp"))
        if when is None:
            return
        session = file_session if is_subagent_file else (get_str(line, "sessionId") or file_session)
        uuid = get_str(line, "uuid")

        def event(name: str, call_id: str | None = None, **detail: Any) -> ThreadEvent:
            origin = uuid or f"{session}|{thread or ''}|{when.isoformat()}"
            return ThreadEvent(
                tool=self.name,
                session_id=session,
                thread_id=thread,
                timestamp=when,
                kind=name,
                call_id=call_id,
                key=f"{origin}|{name}|{call_id or ''}",
                **detail,
            )

        if kind == "system":
            if line.get("subtype") == "turn_duration":
                yield event(TURN_END)
            return
        message = line.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            yield event(MODEL)
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                call_id = get_str(block, "id")
                if call_id is None:
                    continue
                tool = get_str(block, "name")
                names[call_id] = tool
                yield event(
                    TOOL_START,
                    call_id,
                    human_wait=tool in HUMAN_WAIT_TOOLS,
                    category=tool_category(tool, block.get("input")),
                    spawns=tool in SUBAGENT_TOOLS,
                )
            return
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if not results:
            if line.get("isMeta") is not True or get_str(line, "origin", "kind") is not None:
                yield event(PROMPT)
            return
        decided = _decided_by_person(line)
        for block in results:
            call_id = get_str(block, "tool_use_id")
            if call_id is None:
                continue
            tool = names.get(call_id)
            failed = block.get("is_error") is True
            # A line holds one toolUseResult: it describes the block only when the block is alone.
            added, removed, files = (
                edit_detail(line.get("toolUseResult"))
                if tool in EDIT_TOOLS and not failed and len(results) == 1
                else (0, 0, ())
            )
            yield event(
                TOOL_END,
                call_id,
                human_wait=tool in HUMAN_WAIT_TOOLS,
                subagent_wait=tool in SUBAGENT_TOOLS and not _async_launch(line),
                user_decided=decided,
                failed=failed,
                lines_added=added,
                lines_removed=removed,
                files=files,
            )

    def _marks(
        self,
        line: dict[str, Any],
        blocks: list[Any],
        kind: object,
        session: str,
        thread: str | None,
        creating: dict[str, tuple[Any, str, str | None]],
    ) -> Iterator[UsageEvent]:
        """Markers for the marking Specivo calls of one line (see :mod:`ai_usage_reporter.issue_marks`)."""
        cwd = get_str(line, "cwd")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "tool_use":
                mark = marking_call(block.get("name"), block.get("input"))
                if mark is None:
                    continue
                when = parse_timestamp(line.get("timestamp"))
                if when is None:
                    continue
                if mark == CREATE:
                    call_id = get_str(block, "id")
                    if call_id is not None:
                        creating.setdefault(call_id, (when, session, cwd))
                    continue
                yield self._marker(mark, when, session, cwd, thread)
            elif kind == "user" and block.get("type") == "tool_result":
                call_id = get_str(block, "tool_use_id")
                pending = creating.pop(call_id, None) if call_id is not None else None
                if pending is None or block.get("is_error") is True:
                    continue
                key = created_issue(block.get("content"))
                if key is not None:
                    yield self._marker(key, pending[0], pending[1], pending[2], thread)

    def _marker(self, issue: str, when: Any, session: str, cwd: str | None, thread: str | None) -> UsageEvent:
        return UsageEvent(
            tool=self.name,
            session_id=session,
            model="",
            timestamp=when,
            tokens=TokenCounts(),
            requests=0,
            cwd=cwd,
            thread=thread,
            issue_mark=issue,
        )

    @staticmethod
    def _event(
        common: dict[str, Any], model: str | None, message_id: str | None, usage: dict[str, Any]
    ) -> UsageEvent | None:
        tokens = _usage_tokens(usage)
        if tokens.is_zero():
            return None
        session = common["session_id"]
        return UsageEvent(
            model=model or "unknown",
            tokens=tokens,
            message_id=message_id,
            replay_key=f"{session}/{message_id}" if message_id else None,
            **common,
        )

    def dedup_key(self, event: UsageEvent) -> str | None:
        if event.message_id is None:
            return None
        if event.request_id is not None:
            # The same response in any file of any session.
            return f"{event.message_id}|{event.request_id}"
        # No request id: only copies within one session are the same response.
        return f"{event.session_id}|{event.message_id}"

    def hook(self) -> HookSpec:
        return HookSpec(
            kind="claude-settings-hook",
            events=("SessionStart", "SessionEnd", "Stop"),
            notes=(
                "settings.json hooks; stdin gives session_id, transcript_path and cwd;"
                " SessionStart only records the route"
            ),
        )
