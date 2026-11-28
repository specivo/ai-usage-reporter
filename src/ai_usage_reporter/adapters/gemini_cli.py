"""Gemini CLI: chat recordings under ``~/.gemini/tmp/<project>/chats/``.

Where the data is:

- Every session is recorded in ``~/.gemini/tmp/<project>/chats/``, where
  ``<project>`` is a short project name (older releases used a hash of the
  project directory). Under the macOS sandbox the directory is
  ``~/.cache/.gemini``. The ``[sources.gemini-cli] roots`` setting names other
  ``.gemini`` directories.
- Current releases write ``session-<time>-<id>.jsonl``; older ones wrote one
  JSON document, ``session-<time>-<id>.json``, which the CLI converts to JSONL
  when it opens the session again, so both copies can exist. Each file is read
  as one JSON document first and as JSONL otherwise. Temporary
  (``.tmp-*``) and quarantined (``.unreadable-*``) files are ignored.
- A subagent's session is a file ``<sub session id>.jsonl`` in a directory
  named after the parent session's id.

What the data looks like, and what this adapter does with it:

- The JSON document has ``sessionId``, ``startTime`` and ``messages``. In the
  JSONL form the first line is that header without messages, then one line
  per message. A message is written again in full when it changes (the last
  copy wins), and lines with ``$set``, ``$patch`` or ``$rewindTo`` change the
  header or the message list. A ``$set`` that carries ``messages`` is read
  for its messages; the others change nothing about usage. Rewound or
  removed messages were still sent to the model, so their usage is counted.
- Messages of ``type`` ``gemini`` carry ``id``, ``timestamp``, ``model`` and
  ``tokens``: ``input`` (the prompt), ``output`` (the response), ``cached``
  (the part of the prompt served from the context cache), ``thoughts``
  (reasoning), ``tool`` (the prompt of tool-use calls) and ``total``.
- **Token semantics.** ``cached`` is part of ``input``, so it is subtracted
  and reported as ``cache_read``. ``thoughts`` is not part of ``output`` and
  is reported as ``reasoning``. ``tool`` is prompt that ``input`` does not
  include, so it is added to ``input``.
- A message is counted once per session and message id, across the JSON
  and JSONL copies of a session. Subagent sessions count in their parent.

**Active time (coarse).** The recording keeps few times: a message's
``timestamp`` is set once, when the CLI creates the message, and each thought
summary of a ``gemini`` message has its own ``timestamp``. Tool calls carry no
time at all (their results are written into the calling message later), and
nothing marks the end of a turn. So :meth:`GeminiCliAdapter.parse_events`
yields only ``prompt`` events (a ``user`` message that is not just a tool's
function response) and ``model`` events (each ``gemini`` message and each of
its thoughts), and only the definition's gap rule applies: the time between
two of them counts when it is 10 minutes or less and no prompt ends it. A
tool run is counted only through the gaps around it, so a tool that runs
longer than 10 minutes counts nothing, and the generation of a turn's last
answer after its message was created is not seen. Gemini CLI has no tool that
waits for a person or a child in this record, so no event is flagged. A
subagent's session is its own thread (its file), reported under the parent.
Only the times, ids and types of messages are read for this, never their
content. The adapter yields no work activity (its tool calls have no times).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import DiscoveryContext, HookSpec
from ai_usage_reporter.adapters._jsonl import get_str, iter_json_lines
from ai_usage_reporter.model import (
    MODEL,
    PROMPT,
    SourceFile,
    ThreadEvent,
    TokenCounts,
    UsageEvent,
    count,
    parse_timestamp,
)

#: Larger files are not read as one JSON document (they are JSONL anyway).
MAX_DOCUMENT_BYTES = 256 * 1024 * 1024


def gemini_dirs(ctx: DiscoveryContext) -> list[Path]:
    raw = list(ctx.roots) if ctx.roots else [ctx.home / ".gemini", ctx.home / ".cache" / ".gemini"]
    found: list[Path] = []
    for directory in raw:
        tmp = directory if directory.name == "tmp" else directory / "tmp"
        if tmp.is_dir() and tmp not in found:
            found.append(tmp)
    return found


def _is_chat_file(path: Path) -> bool:
    name = path.name
    if ".tmp-" in name or ".unreadable-" in name:
        return False
    return name.endswith((".json", ".jsonl")) and path.is_file()


def _tokens(value: object) -> TokenCounts:
    tokens = value if isinstance(value, dict) else {}
    prompt = count(tokens.get("input"))
    cached = min(count(tokens.get("cached")), prompt)
    return TokenCounts(
        input=prompt - cached + count(tokens.get("tool")),
        cache_read=cached,
        output=count(tokens.get("output")),
        reasoning=count(tokens.get("thoughts")),
    )


def _only_function_responses(content: object) -> bool:
    """Whether a ``user`` message's content is only tool results (not a prompt)."""
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(part, dict) and "functionResponse" in part for part in content)


class GeminiCliAdapter:
    name = "gemini-cli"
    provider = "google"
    parser_version = "gemini-cli/2"
    #: Thread events are for active time only: tool calls have no times, so there is no work activity.
    work_activity = False

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]:
        for tmp in gemini_dirs(ctx):
            for chats in sorted(tmp.glob("*/chats")):
                if not chats.is_dir():
                    continue
                for path in sorted(chats.glob("session-*")):
                    if _is_chat_file(path):
                        yield SourceFile(adapter=self.name, path=path, root=tmp)
                # Subagents: chats/<parent session id>/<sub session id>.jsonl
                for path in sorted(chats.glob("*/*")):
                    if _is_chat_file(path):
                        yield SourceFile(adapter=self.name, path=path, root=tmp, session_hint=path.parent.name)

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]:
        header, messages = self._read(src)
        yield from self._usage(src, header, messages)

    def parse_events(self, src: SourceFile) -> Iterator[UsageEvent | ThreadEvent]:
        """The usage events of :meth:`parse` and the coarse thread events (see the module docs)."""
        header, messages = self._read(src)
        yield from self._usage(src, header, messages)
        own, session = self._ids(src, header)
        thread = own if session != own else None

        def event(when: object, kind: str, key: str) -> ThreadEvent | None:
            moment = parse_timestamp(when)
            if moment is None:
                return None
            return ThreadEvent(
                tool=self.name, session_id=session, thread_id=thread, timestamp=moment, kind=kind, key=f"{own}|{key}"
            )

        for message_id, message in messages.items():
            found: list[ThreadEvent | None] = []
            if message.get("type") == "user":
                if not _only_function_responses(message.get("content")):
                    found.append(event(message.get("timestamp"), PROMPT, f"{message_id}|prompt"))
            else:
                found.append(event(message.get("timestamp"), MODEL, f"{message_id}|model"))
                thoughts = message.get("thoughts")
                for index, thought in enumerate(thoughts if isinstance(thoughts, list) else []):
                    if isinstance(thought, dict):
                        found.append(event(thought.get("timestamp"), MODEL, f"{message_id}|thought|{index}"))
            yield from (e for e in found if e is not None)

    def _ids(self, src: SourceFile, header: dict[str, Any]) -> tuple[str, str]:
        """The file's own session id and the session it is reported under (the parent's, for a subagent)."""
        own = get_str(header, "sessionId") or src.path.name.split(".", 1)[0]
        return own, src.session_hint or own

    def _usage(
        self, src: SourceFile, header: dict[str, Any], messages: dict[str, dict[str, Any]]
    ) -> Iterator[UsageEvent]:
        own, session = self._ids(src, header)
        parent = src.session_hint
        for message in messages.values():
            if message.get("type") != "gemini":
                continue
            tokens = _tokens(message.get("tokens"))
            if tokens.is_zero():
                continue
            when = parse_timestamp(message.get("timestamp"))
            if when is None:
                src.stats.skipped_events += 1
                continue
            src.stats.events += 1
            yield UsageEvent(
                tool=self.name,
                session_id=session,
                sub_session_id=own if parent and own != parent else None,
                model=get_str(message, "model") or "unknown",
                timestamp=when,
                tokens=tokens,
                provider=self.provider,
                message_id=get_str(message, "id"),
                source_path=str(src.path),
            )

    def _read(self, src: SourceFile) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """The session header and the ``gemini`` and ``user`` messages by id (the last copy of each wins)."""
        header: dict[str, Any] = {}
        messages: dict[str, dict[str, Any]] = {}
        document = self._document(src.path)
        if document is not None:
            src.stats.lines += 1
            header = document
            self._take(document.get("messages"), messages)
            return header, messages
        for line in iter_json_lines(src.path, src.stats):
            if "$set" in line:
                update = line["$set"]
                if isinstance(update, dict):
                    self._take(update.get("messages"), messages)
                    header.update({k: v for k, v in update.items() if k != "messages"})
                continue
            if "$patch" in line or "$rewindTo" in line:
                continue
            if "type" in line and "id" in line:
                self._take([line], messages)
            elif "sessionId" in line:
                header.update({k: v for k, v in line.items() if k != "messages"})
                self._take(line.get("messages"), messages)
        return header, messages

    @staticmethod
    def _document(path: Path) -> dict[str, Any] | None:
        try:
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("messages"), list) else None

    @staticmethod
    def _take(items: object, messages: dict[str, dict[str, Any]]) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict) or item.get("type") not in ("gemini", "user"):
                continue
            message_id = get_str(item, "id")
            if message_id is None:
                continue
            messages.pop(message_id, None)  # keep the order of the last copy
            messages[message_id] = item

    def dedup_key(self, event: UsageEvent) -> str | None:
        if event.message_id is None:
            return None
        return f"{event.sub_session_id or event.session_id}|{event.message_id}"

    def hook(self) -> HookSpec:
        return HookSpec(
            kind="gemini-settings-hook",
            events=("SessionEnd", "AfterAgent"),
            notes="settings.json hooks; stdin gives session_id",
        )
