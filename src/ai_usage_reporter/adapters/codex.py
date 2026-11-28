"""Codex CLI: rollouts under ``$CODEX_HOME/sessions`` and ``$CODEX_HOME/archived_sessions``.

What the rollout format looks like, and what this adapter does with it:

- A rollout is a JSONL file named ``rollout-<time>-<session uuid>.jsonl``,
  optionally zstd-compressed (``.jsonl.zst``; needs Python 3.14 or the
  ``zstd`` extra). Archiving moves a rollout from ``sessions/`` to
  ``archived_sessions/``; when both hold the same session, the active copy is
  read, a plain copy before a compressed one, the newest first.
- ``session_meta`` opens the file: the session ``id``, ``cwd``,
  ``cli_version``, ``model_provider`` and, inside a git checkout,
  ``git.branch`` and ``git.repository_url``.
- **Route and provider.** ``model_provider`` is the provider profile the
  session ran on (``openai`` for the ChatGPT login or an OpenAI key, or the id
  of a ``[model_providers.<id>]`` entry in ``config.toml``, such as a company
  gateway). It is sent as ``route``. ``provider`` stays ``openai`` for OpenAI's
  models (``gpt-*``, ``o*``, ``codex-*``) so the server can price them; a local
  engine (``ollama``, ``lmstudio``) gives ``local``, paid as ``local``; any
  other model is priced by the maker the route or the model id names, or by
  its name alone (see :mod:`ai_usage_reporter.adapters._routes`).
- ``turn_context`` starts each turn and names the ``model`` in use (and the
  ``cwd``); usage that follows belongs to that model.
- ``event_msg`` lines of ``payload.type`` ``token_count`` report usage:
  ``info.total_token_usage`` is the running total of the session and
  ``info.last_token_usage`` the usage of the last request. The running total
  is the source of truth: each event counts the growth since the previous
  one, and a repeated snapshot counts nothing. When the total shrinks (the
  conversation was compacted and the counter started again), the event counts
  ``last_token_usage``.
- OpenAI counts cached input inside ``input_tokens`` and reasoning inside
  ``output_tokens``. Both are moved to their own classes so each token is
  counted once (``cache_read`` and ``reasoning``).
- **Issue marks.** Marking Specivo calls (see
  :mod:`ai_usage_reporter.issue_marks`) become markers at the time the call
  started. Codex has logged MCP calls in three ways, all read:

  - current rollouts (since September 2026): an ``event_msg`` of type
    ``item_completed`` whose ``item.type`` is ``McpToolCall``, with the bare
    tool name in ``item.tool``, the MCP server in ``item.server``,
    ``item.arguments`` as an object, ``item.status`` (``completed`` or
    ``failed``) and ``item.result`` (``content`` blocks and ``isError``, or
    ``null``). The call started at ``started_at_ms`` (the line's
    ``timestamp`` when that is missing). Calls made from code mode (the
    ``exec`` custom tool) are logged the same way. A failed call, a ``null``
    result or ``isError: true`` marks nothing;
  - older rollouts: a ``response_item`` of type ``function_call`` names the
    tool (``name``, with the MCP server in ``namespace``) and its
    ``arguments`` as JSON text;
  - older rollouts: an ``event_msg`` of type ``mcp_tool_call_end`` repeats
    the call (``invocation.tool`` and ``invocation.arguments``) with its
    ``result`` under ``Ok``.

  ``specivo_create_issue`` takes the created key from the start of the
  result's first text block. One call marks at most once: the
  ``item.id`` and ``call_id`` of the forms are the same call. Only the tool
  name, the arguments that decide the mark, the call's status and error flag
  and the issue key are read.
- **Subagent rollouts.** A subagent (a collaboration agent) has a rollout of
  its own. Its ``session_meta`` names the parent in ``parent_thread_id``
  (``session_id`` is the root session, and ``source`` is an object with a
  ``subagent`` key). Such a rollout is reported under the root session (the
  parent when ``session_id`` is missing), with its own id as
  ``sub_session_id``, which is also its thread: its usage, marks and thread
  events are one thread of the parent session, so an unmarked subagent takes
  the parent's issue at its start, and ``push --session <child>`` selects the
  parent. Only the first ``session_meta`` of a file counts: the subagent's
  rollout starts with a copy of the parent's history (its own
  ``session_meta``, then the parent's, the parent's turns and calls), and
  every line before ``subagent_history_start_ordinal`` (by the line's
  ``ordinal``) is that copy and is skipped. The copied turns keep the parent's
  times, so a subagent's thread events from before its own ``session_meta``
  line (less two seconds) are dropped as well, for a copy without that ordinal.
- **Active time.** :meth:`CodexAdapter.parse_events` also yields the thread
  events :mod:`ai_usage_reporter.activity` measures, from rollouts that have
  ``item_completed`` lines (written since September 2026); older rollouts
  yield none, so their records carry no active time:

  - ``prompt``: ``event_msg`` / ``task_started``, at ``started_at``;
  - ``turn_end``: ``task_complete`` and ``turn_aborted``, at ``completed_at``.
    These two are whole seconds (cut down); the line's ``timestamp`` is used
    instead when it lies less than two seconds after them, which it does when
    the line is written as the turn starts or ends;
  - ``model``: an ``item_completed`` of ``item.type`` ``Reasoning`` or
    ``AgentMessage`` gives two, at ``started_at_ms`` and ``completed_at_ms``;
  - ``tool_start`` and ``tool_end``: an ``item_completed`` of ``item.type``
    ``CommandExecution``, ``McpToolCall``, ``FileChange``, ``ImageView``,
    ``Extension`` or ``CollabAgentToolCall``, at ``started_at_ms`` and
    ``completed_at_ms``, paired by ``item.id``. A ``CollabAgentToolCall``
    whose ``item.tool`` is ``wait`` waits for a subagent, and an item for a
    user-input request (``request_user_input``, as its type or tool name)
    waits for a person. ``UserMessage``, ``ContextCompaction``,
    ``SubAgentActivity`` and other items give no event.

  Items are logged when they complete, so the events are sorted by their own
  time. An event is named by its ``turn_id`` and ``item.id``, so a copied line
  counts once. Only timestamps, ids, the item's type and the collaboration
  tool's name are read for this; no command, argument, output or message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_dict, get_str, iter_json_lines, zstd_available
from ai_usage_reporter.adapters._routes import is_local, maker_of
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

_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
#: OpenAI's own model names: priced as ``openai`` whatever profile served them.
_OPENAI_MODEL = re.compile(r"^(?:openai/)?(?:gpt-|chatgpt-|codex-|o\d)", re.IGNORECASE)
_RAW_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")

#: Items whose start and end are model output (active time).
MODEL_ITEMS = frozenset({"Reasoning", "AgentMessage"})
#: Items that are tool runs (active time), paired by ``item.id``.
TOOL_ITEMS = frozenset(
    {"CommandExecution", "McpToolCall", "FileChange", "ImageView", "Extension", "CollabAgentToolCall"}
)
#: The collaboration tool that blocks until a subagent finishes.
SUBAGENT_WAIT_TOOL = "wait"
#: The name a user-input request starts with, as an item type (snake-cased) or tool name.
HUMAN_WAIT_PREFIX = "request_user_input"
#: How far after a turn's whole-second time its line's timestamp may lie and still be the same moment.
_SAME_MOMENT = timedelta(seconds=2)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

Raw = tuple[int, int, int, int]


def _raw(value: object) -> Raw | None:
    if not isinstance(value, dict):
        return None
    raw = tuple(count(value.get(k)) for k in _RAW_KEYS)
    return (raw[0], raw[1], raw[2], raw[3])


def _normalise(raw: Raw) -> TokenCounts:
    fresh_and_cached, cached, output_with_reasoning, reasoning = raw
    cached = min(cached, fresh_and_cached)
    reasoning = min(reasoning, output_with_reasoning)
    return TokenCounts(
        input=fresh_and_cached - cached,
        cache_read=cached,
        output=output_with_reasoning - reasoning,
        reasoning=reasoning,
    )


def resolve_provider(route: str | None, model: str | None) -> tuple[str | None, str | None]:
    """The provider to send for *model* run through the ``model_provider`` *route*, and the cost type it implies."""
    if is_local(route):
        return "local", "local"
    if route is None or model is None or _OPENAI_MODEL.match(model):
        # Rollouts without model_provider come from the OpenAI-only releases.
        return "openai", None
    return maker_of(route, model), None


def codex_homes(ctx: DiscoveryContext) -> list[Path]:
    if ctx.roots:
        homes = [p.parent if p.name in ("sessions", "archived_sessions") else p for p in ctx.roots]
    elif ctx.env.get("CODEX_HOME", "").strip():
        homes = [ctx.expand(ctx.env["CODEX_HOME"].strip())]
    else:
        homes = [ctx.home / ".codex"]
    unique: list[Path] = []
    for home in homes:
        if home not in unique and ((home / "sessions").is_dir() or (home / "archived_sessions").is_dir()):
            unique.append(home)
    return unique


class CodexAdapter:
    name = "codex"
    provider = "openai"
    parser_version = "codex/5"

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        can_decompress = zstd_available()
        candidates: dict[str, list[tuple[tuple[int, float], SourceFile]]] = {}
        for home in codex_homes(ctx):
            for archived, folder in enumerate((home / "sessions", home / "archived_sessions")):
                if not folder.is_dir():
                    continue
                for path in folder.rglob("rollout-*"):
                    compressed = path.name.endswith(".jsonl.zst")
                    if not (path.name.endswith(".jsonl") or compressed) or not path.is_file():
                        continue
                    match = _UUID.search(path.name)
                    session = match.group(0).lower() if match else path.name
                    # Unreadable compressed copies come last; a lone one still shows up in doctor.
                    unreadable = 1 if compressed and not can_decompress else 0
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    rank = (unreadable * 4 + archived * 2 + int(compressed), -mtime)
                    src = SourceFile(adapter=self.name, path=path, root=folder, session_hint=session, priority=rank[0])
                    candidates.setdefault(session, []).append((rank, src))
        for session in sorted(candidates):
            best = min(candidates[session], key=lambda item: (item[0], str(item[1].path)))
            yield best[1]

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        for event in self.parse_events(src):
            if isinstance(event, UsageEvent):
                yield event

    def parse_events(self, src: SourceFile) -> Iterator[UsageEvent | ThreadEvent]:
        """Usage events and markers, and the thread events for active time, in one pass."""
        session = src.session_hint or src.path.name
        #: Set for a subagent rollout: its own id (the thread), reported under the parent ``session``.
        child: str | None = None
        meta_seen = False
        #: Lines before this ordinal of a subagent rollout copy the parent's history.
        history_start: int | None = None
        model: str | None = None
        cwd: str | None = None
        version: str | None = None
        branch: str | None = None
        repo_url: str | None = None
        route: str | None = None
        running: Raw | None = None
        #: Calls already marked, and specivo_create_issue calls waiting for their result.
        marked: set[str] = set()
        creating: dict[str, datetime] = {}
        #: Thread events wait for the end of the file: only a rollout with items yields them.
        thread_events: list[ThreadEvent] = []
        has_items = False
        #: A subagent's rollout starts when its first session_meta was written: anything earlier is copied.
        child_start: datetime | None = None
        for line in iter_json_lines(src.path, src.stats):
            kind = line.get("type")
            payload = line.get("payload")
            if not isinstance(payload, dict):
                continue
            ordinal = line.get("ordinal")
            if (
                history_start is not None
                and isinstance(ordinal, int)
                and not isinstance(ordinal, bool)
                and ordinal < history_start
            ):
                continue  # the parent's history, copied into a subagent's rollout
            if kind == "session_meta":
                if meta_seen:
                    continue  # a copied session_meta (the parent's) says nothing about this file
                meta_seen = True
                own = (get_str(payload, "id") or session).lower()
                parent = get_str(payload, "parent_thread_id")
                if parent and parent.lower() != own:
                    root = get_str(payload, "session_id")
                    session = (root if root and root.lower() != own else parent).lower()
                    child = own
                    child_start = parse_timestamp(line.get("timestamp"))
                    start = payload.get("subagent_history_start_ordinal")
                    if isinstance(start, int) and not isinstance(start, bool):
                        history_start = start
                else:
                    session = own
                cwd = get_str(payload, "cwd") or cwd
                version = get_str(payload, "cli_version") or version
                route = get_str(payload, "model_provider") or route
                git = get_dict(payload, "git")
                branch = get_str(git, "branch") or branch
                repo_url = get_str(git, "repository_url") or repo_url
                continue
            if payload.get("type") in ("function_call", "mcp_tool_call_end", "item_completed"):
                if kind == "event_msg" and payload.get("type") == "item_completed":
                    has_items = True
                    thread_events.extend(self._item_events(line, payload, session, child))
                for issue, marked_at in self._marks(line, payload, marked, creating):
                    yield UsageEvent(
                        tool=self.name,
                        session_id=session,
                        sub_session_id=child,
                        model="",
                        timestamp=marked_at,
                        tokens=TokenCounts(),
                        requests=0,
                        cwd=cwd,
                        issue_mark=issue,
                    )
                continue
            if kind == "turn_context":
                model = get_str(payload, "model") or model
                cwd = get_str(payload, "cwd") or cwd
                continue
            if kind != "event_msg":
                continue
            if payload.get("type") in ("task_started", "task_complete", "turn_aborted"):
                turn_event = self._turn_event(line, payload, session, child)
                if turn_event is not None:
                    thread_events.append(turn_event)
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            total = _raw(get_dict(info, "total_token_usage"))
            last = _raw(get_dict(info, "last_token_usage"))
            used: Raw | None
            if total is not None:
                if running is None:
                    used = last if last is not None else total
                elif total == running:
                    used = None
                elif any(now < before for now, before in zip(total, running, strict=True)):
                    used = last if last is not None else total
                else:
                    diff = tuple(now - before for now, before in zip(total, running, strict=True))
                    used = (diff[0], diff[1], diff[2], diff[3])
                running = total
            else:
                used = last
            if used is None:
                continue
            tokens = _normalise(used)
            if tokens.is_zero():
                continue
            when = parse_timestamp(line.get("timestamp"))
            if when is None:
                src.stats.skipped_events += 1
                continue
            snapshot = total if total is not None else used
            provider, cost_type = resolve_provider(route, model)
            src.stats.events += 1
            yield UsageEvent(
                tool=self.name,
                session_id=session,
                sub_session_id=child,
                model=model or "unknown",
                timestamp=when,
                tokens=tokens,
                provider=provider,
                route=route,
                cost_type=cost_type,
                tool_version=version,
                message_id=f"{when.isoformat()}|{','.join(map(str, snapshot))}",
                git_branch=branch if branch and branch != "HEAD" else None,
                repo_url=repo_url,
                cwd=cwd,
                source_path=str(src.path),
            )
        if not has_items:
            return  # an older rollout: its turns alone cannot be measured
        # Items are logged when they complete: put every event at its own time (sorted() is stable).
        if child_start is not None:
            # Belt and braces for a copy without subagent_history_start_ordinal: the copied turns keep the
            # parent's (earlier) times, so they lie before the subagent started.
            floor = child_start - _SAME_MOMENT
            thread_events = [e for e in thread_events if e.timestamp >= floor]
        for thread_event in sorted(thread_events, key=lambda e: e.timestamp):
            src.stats.thread_events += 1
            yield thread_event

    def _turn_event(
        self, line: dict[str, object], payload: dict[str, object], session: str, child: str | None
    ) -> ThreadEvent | None:
        """The ``prompt`` of a ``task_started`` line, or the ``turn_end`` of a ``task_complete``/``turn_aborted``."""
        started = payload.get("type") == "task_started"
        exact = parse_timestamp(payload.get("started_at" if started else "completed_at"))
        written = parse_timestamp(line.get("timestamp"))
        if exact is None:
            when = written
        elif written is not None and exact <= written < exact + _SAME_MOMENT:
            when = written  # the same moment, to the millisecond
        else:
            when = exact
        if when is None:
            return None
        name = PROMPT if started else TURN_END
        turn = get_str(payload, "turn_id")
        origin = turn or f"{child or session}|{when.isoformat()}"
        return self._thread_event(session, child, when, name, key=f"{origin}|{name}")

    def _item_events(
        self, line: dict[str, object], payload: dict[str, object], session: str, child: str | None
    ) -> Iterator[ThreadEvent]:
        """The thread events of one ``item_completed`` line: two ``model`` events, a tool run, or nothing."""
        item = get_dict(payload, "item")
        if item is None:
            return
        item_type = get_str(item, "type") or ""
        tool = get_str(item, "tool")
        human_wait = _CAMEL.sub("_", item_type).lower().startswith(HUMAN_WAIT_PREFIX) or bool(
            tool and tool.lower().startswith(HUMAN_WAIT_PREFIX)
        )
        is_tool = item_type in TOOL_ITEMS or human_wait
        if not is_tool and item_type not in MODEL_ITEMS:
            return
        started = parse_timestamp(payload.get("started_at_ms"))
        completed = parse_timestamp(payload.get("completed_at_ms")) or parse_timestamp(line.get("timestamp"))
        item_id = get_str(item, "id")
        turn = get_str(payload, "turn_id") or ""
        if not is_tool:
            for when, edge in ((started, "start"), (completed, "end")):
                if when is not None:
                    origin = f"{turn}|{item_id}" if item_id else f"{child or session}|{when.isoformat()}"
                    yield self._thread_event(session, child, when, MODEL, key=f"{origin}|{MODEL}-{edge}")
            return
        if item_id is None:
            return  # a tool run cannot be paired without its id
        flags = {
            "human_wait": human_wait,
            "subagent_wait": item_type == "CollabAgentToolCall" and tool == SUBAGENT_WAIT_TOOL,
        }
        for when, name in ((started, TOOL_START), (completed, TOOL_END)):
            if when is not None:
                yield self._thread_event(
                    session, child, when, name, call_id=item_id, key=f"{turn}|{item_id}|{name}", **flags
                )

    def _thread_event(
        self,
        session: str,
        child: str | None,
        when: datetime,
        name: str,
        *,
        key: str,
        call_id: str | None = None,
        human_wait: bool = False,
        subagent_wait: bool = False,
    ) -> ThreadEvent:
        return ThreadEvent(
            tool=self.name,
            session_id=session,
            thread_id=child,
            timestamp=when,
            kind=name,
            call_id=call_id,
            human_wait=human_wait,
            subagent_wait=subagent_wait,
            key=key,
        )

    @staticmethod
    def _marks(
        line: dict[str, object], payload: dict[str, object], marked: set[str], creating: dict[str, datetime]
    ) -> Iterator[tuple[str, datetime]]:
        """The issues one line marks, with the time of the call (see :mod:`ai_usage_reporter.issue_marks`)."""
        if payload.get("type") == "item_completed":
            yield from CodexAdapter._item_marks(line, payload, marked, creating)
            return
        when = parse_timestamp(line.get("timestamp"))
        call_id = get_str(payload, "call_id")
        if when is None or call_id is None or call_id in marked:
            return
        if payload.get("type") == "function_call":
            mark = marking_call(payload.get("name"), payload.get("arguments"))
            if mark == CREATE:
                creating.setdefault(call_id, when)
            elif mark is not None:
                marked.add(call_id)
                yield mark, when
            return
        invocation = get_dict(payload, "invocation")
        mark = marking_call(get_str(invocation, "tool"), (invocation or {}).get("arguments"))
        if mark is None:
            return
        marked.add(call_id)
        started = creating.pop(call_id, when)
        if mark != CREATE:
            yield mark, started
            return
        result = get_dict(payload, "result", "Ok")
        if result is None or result.get("isError") is True:
            return
        created = created_issue(result.get("content"))
        if created is not None:
            yield created, started

    @staticmethod
    def _item_marks(
        line: dict[str, object], payload: dict[str, object], marked: set[str], creating: dict[str, datetime]
    ) -> Iterator[tuple[str, datetime]]:
        """The issue a completed ``McpToolCall`` item marks, with the time the call started."""
        item = get_dict(payload, "item")
        if item is None or item.get("type") != "McpToolCall":
            return
        call_id = get_str(item, "id")
        if call_id is None or call_id in marked:
            return
        mark = marking_call(get_str(item, "tool"), item.get("arguments"))
        if mark is None:
            return
        started = (
            creating.pop(call_id, None)
            or parse_timestamp(payload.get("started_at_ms"))
            or parse_timestamp(line.get("timestamp"))
        )
        if started is None:
            return
        marked.add(call_id)
        result = item.get("result")
        if item.get("status") == "failed" or not isinstance(result, dict) or result.get("isError") is True:
            return
        if mark != CREATE:
            yield mark, started
            return
        created = created_issue(result.get("content"))
        if created is not None:
            yield created, started

    def dedup_key(self, event: UsageEvent) -> str | None:
        # The same point of the same rollout's running total, in a copied rollout (a subagent's
        # rollout has a running total of its own).
        if event.message_id is None:
            return None
        return f"{event.sub_session_id or event.session_id}|{event.message_id}"

    def hook(self) -> HookSpec:
        return HookSpec(kind="codex-notify", events=("agent-turn-complete",), notes="the notify program in config.toml")
