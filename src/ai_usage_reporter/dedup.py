"""Deduplication of usage events across all files of one adapter.

Two steps, both driven by what the adapter says about its events:

1. Events with the same :meth:`Adapter.dedup_key` are copies of one request.
   One copy is kept: a non-replay copy before a replay copy, then the copy
   with more tokens (a log may record a request's usage more than once, and
   the last, complete record is the largest). On a full tie the copy read
   first is kept, and files are read in path order, so the result is stable.
2. A replay event (``is_replay``) whose ``replay_key`` is also the
   ``replay_key`` of a non-replay event repeats that event and is dropped.
"""

from __future__ import annotations

from collections.abc import Iterable

from ai_usage_reporter.adapters import Adapter
from ai_usage_reporter.model import UsageEvent


def _rank(event: UsageEvent) -> tuple[bool, int]:
    return (not event.is_replay, event.tokens.total)


def deduplicate(adapter: Adapter, events: Iterable[UsageEvent]) -> list[UsageEvent]:
    unique: list[UsageEvent] = []
    by_key: dict[str, int] = {}
    for event in events:
        key = adapter.dedup_key(event)
        if key is None:
            unique.append(event)
            continue
        index = by_key.get(key)
        if index is None:
            by_key[key] = len(unique)
            unique.append(event)
        elif _rank(event) > _rank(unique[index]):
            unique[index] = event
    originals = {e.replay_key for e in unique if not e.is_replay and e.replay_key is not None}
    kept = [e for e in unique if not (e.is_replay and e.replay_key in originals)]
    kept.sort(key=lambda e: (e.timestamp, e.session_id, e.model, e.message_id or ""))
    return kept
