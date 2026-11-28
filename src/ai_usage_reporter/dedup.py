"""Deduplication of usage events across all files of one adapter.

Two steps, both driven by what the adapter says about its events:

1. Events with the same :meth:`Adapter.dedup_key` are copies of one request.
   One copy is kept: a non-replay copy before a replay copy, then the copy
   with more tokens (a log may record a request's usage more than once, and
   the last, complete record is the largest). On a full tie the copy read
   first is kept, and files are read in path order, so the result is stable.
2. A replay event (``is_replay``) whose ``replay_key`` is also the
   ``replay_key`` of a non-replay event repeats that event and is dropped.

:func:`resolve` also says, for every event, which kept event it stands for,
so that what belongs to a dropped copy (its active time) follows the request
to the copy that is counted.
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_usage_reporter.adapters import Adapter
from ai_usage_reporter.model import UsageEvent


def _rank(event: UsageEvent) -> tuple[bool, int]:
    return (not event.is_replay, event.tokens.total)


def resolve(adapter: Adapter, events: Sequence[UsageEvent]) -> tuple[list[int], list[int | None]]:
    """The indices of the kept events (in time order), and for each event the index of its kept copy.

    The second list maps every index of *events* to the index of the kept event
    that counts the same request (itself when it is kept); ``None`` when the
    copy it repeats is not among the events.
    """
    winner: list[int] = list(range(len(events)))
    unique: list[int] = []
    by_key: dict[str, int] = {}
    for index, event in enumerate(events):
        key = adapter.dedup_key(event)
        if key is None:
            unique.append(index)
            continue
        slot = by_key.get(key)
        if slot is None:
            by_key[key] = len(unique)
            unique.append(index)
            continue
        current = unique[slot]
        if _rank(event) > _rank(events[current]):
            unique[slot] = index
            winner[current] = index
        else:
            winner[index] = current
    originals: dict[str, int] = {}
    for index in unique:
        event = events[index]
        if not event.is_replay and event.replay_key is not None:
            originals.setdefault(event.replay_key, index)
    kept: list[int] = []
    for index in unique:
        event = events[index]
        if event.is_replay and event.replay_key in originals:
            winner[index] = originals[event.replay_key]
        else:
            kept.append(index)
    kept.sort(key=lambda i: (events[i].timestamp, events[i].session_id, events[i].model, events[i].message_id or ""))
    kept_set = set(kept)
    final: list[int | None] = []
    for index in range(len(events)):
        target = index
        # Follow the chain of replacements (a copy beaten by a later copy, a replay of an original).
        for _ in range(len(events)):
            if winner[target] == target:
                break
            target = winner[target]
        final.append(target if target in kept_set else None)
    return kept, final


def deduplicate(adapter: Adapter, events: Sequence[UsageEvent]) -> list[UsageEvent]:
    kept, _ = resolve(adapter, events)
    return [events[i] for i in kept]
