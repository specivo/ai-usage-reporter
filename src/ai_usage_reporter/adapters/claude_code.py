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

``CLAUDE_CONFIG_DIR`` moves the whole Claude config directory; the
``[sources.claude-code] roots`` setting lists other directories to read.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_str, iter_json_lines
from ai_usage_reporter.issue_marks import CREATE, created_issue, marking_call
from ai_usage_reporter.model import SourceFile, TokenCounts, UsageEvent, count, parse_timestamp


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
    parser_version = "claude-code/2"

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        for projects in _config_dirs(ctx):
            for path in sorted(projects.rglob("*.jsonl")):
                if path.is_file():
                    yield SourceFile(adapter=self.name, path=path, root=projects, session_hint=session_from_path(path))

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        file_session = src.session_hint or session_from_path(src.path)
        is_subagent_file = "subagents" in src.path.parts
        thread = src.path.name.split(".", 1)[0] if is_subagent_file else None
        #: specivo_create_issue calls waiting for their result: tool_use id -> (time, session, cwd).
        creating: dict[str, tuple[Any, str, str | None]] = {}
        for line in iter_json_lines(src.path, src.stats):
            message = line.get("message")
            if not isinstance(message, dict):
                continue
            kind = line.get("type")
            if kind in ("assistant", "user") and isinstance(message.get("content"), list):
                yield from self._marks(line, message["content"], kind, file_session, thread, creating)
            if not isinstance(message.get("usage"), dict):
                continue
            when = parse_timestamp(line.get("timestamp"))
            if when is None:
                src.stats.skipped_events += 1
                continue
            # A subagent file always folds into the session directory it sits in.
            session = file_session if thread is not None else (get_str(line, "sessionId") or file_session)
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
                yield main
            iterations = message["usage"].get("iterations")
            if isinstance(iterations, list):
                advisors = [i for i in iterations if isinstance(i, dict) and i.get("type") == "advisor_message"]
                for number, item in enumerate(advisors, start=1):
                    advisor_id = f"{message_id}#advisor{number}" if message_id else None
                    extra = self._event(common, get_str(item, "model"), advisor_id, item)
                    if extra is not None:
                        src.stats.events += 1
                        yield extra

    def _marks(
        self,
        line: dict[str, Any],
        blocks: list[Any],
        kind: object,
        file_session: str,
        thread: str | None,
        creating: dict[str, tuple[Any, str, str | None]],
    ) -> Iterator[UsageEvent]:
        """Markers for the marking Specivo calls of one line (see :mod:`ai_usage_reporter.issue_marks`)."""
        session = file_session if thread is not None else (get_str(line, "sessionId") or file_session)
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
            events=("SessionEnd", "Stop"),
            notes="settings.json hooks; stdin gives session_id, transcript_path and cwd",
        )
