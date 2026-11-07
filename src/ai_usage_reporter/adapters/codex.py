"""Codex CLI: rollouts under ``$CODEX_HOME/sessions`` and ``$CODEX_HOME/archived_sessions``.

What the rollout format looks like, and what this adapter does with it:

- A rollout is a JSONL file named ``rollout-<time>-<session uuid>.jsonl``,
  optionally zstd-compressed (``.jsonl.zst``; needs Python 3.14 or the
  ``zstd`` extra). Archiving moves a rollout from ``sessions/`` to
  ``archived_sessions/``; when both hold the same session, the active copy is
  read, a plain copy before a compressed one, the newest first.
- ``session_meta`` opens the file: the session ``id``, ``cwd``,
  ``cli_version`` and, inside a git checkout, ``git.branch`` and
  ``git.repository_url``.
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
- **Issue marks.** A ``response_item`` of type ``function_call`` names the
  tool (``name``, with the MCP server in ``namespace``) and its ``arguments``
  as JSON text; an ``event_msg`` of type ``mcp_tool_call_end`` repeats the
  call (``invocation.tool`` and ``invocation.arguments``) with its
  ``result``. Marking Specivo calls (see :mod:`ai_usage_reporter.issue_marks`)
  become markers at the call's time; ``specivo_create_issue`` takes the
  created key from the ``mcp_tool_call_end`` result. Only the tool name, the
  arguments that decide the mark and the issue key are read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_dict, get_str, iter_json_lines, zstd_available
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call
from ai_usage_reporter.model import SourceFile, TokenCounts, UsageEvent, count, parse_timestamp

_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RAW_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")

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
    parser_version = "codex/2"

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
        session = src.session_hint or src.path.name
        model: str | None = None
        cwd: str | None = None
        version: str | None = None
        branch: str | None = None
        repo_url: str | None = None
        running: Raw | None = None
        #: Calls already marked, and specivo_create_issue calls waiting for their result.
        marked: set[str] = set()
        creating: dict[str, datetime] = {}
        for line in iter_json_lines(src.path, src.stats):
            kind = line.get("type")
            payload = line.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") in ("function_call", "mcp_tool_call_end"):
                for issue, marked_at in self._marks(line, payload, marked, creating):
                    yield UsageEvent(
                        tool=self.name,
                        session_id=session,
                        model="",
                        timestamp=marked_at,
                        tokens=TokenCounts(),
                        requests=0,
                        cwd=cwd,
                        issue_mark=issue,
                    )
                continue
            if kind == "session_meta":
                session = (get_str(payload, "id") or session).lower()
                cwd = get_str(payload, "cwd") or cwd
                version = get_str(payload, "cli_version") or version
                git = get_dict(payload, "git")
                branch = get_str(git, "branch") or branch
                repo_url = get_str(git, "repository_url") or repo_url
                continue
            if kind == "turn_context":
                model = get_str(payload, "model") or model
                cwd = get_str(payload, "cwd") or cwd
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
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
            src.stats.events += 1
            yield UsageEvent(
                tool=self.name,
                session_id=session,
                model=model or "unknown",
                timestamp=when,
                tokens=tokens,
                provider=self.provider,
                tool_version=version,
                message_id=f"{when.isoformat()}|{','.join(map(str, snapshot))}",
                git_branch=branch if branch and branch != "HEAD" else None,
                repo_url=repo_url,
                cwd=cwd,
                source_path=str(src.path),
            )

    @staticmethod
    def _marks(
        line: dict[str, object], payload: dict[str, object], marked: set[str], creating: dict[str, datetime]
    ) -> Iterator[tuple[str, datetime]]:
        """The issues one line marks, with the time of the call (see :mod:`ai_usage_reporter.issue_marks`)."""
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

    def dedup_key(self, event: UsageEvent) -> str | None:
        # The same point of the same session's running total, in a copied rollout.
        return f"{event.session_id}|{event.message_id}" if event.message_id else None

    def hook(self) -> HookSpec:
        return HookSpec(kind="codex-notify", events=("agent-turn-complete",), notes="the notify program in config.toml")
