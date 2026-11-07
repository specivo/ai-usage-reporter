"""Buckets: one session of one tool, with one model, on one UTC day.

A bucket holds the complete totals the contract asks for. Buckets are cut at
midnight UTC, whatever the person's time zone, so that the server's price and
exchange-rate lookups for a bucket are the same for everyone.

**Long context.** Some models bill a whole request at higher rates once the
request's context (input plus cache reads and writes) passes a threshold.
That decision is per request, so it is made here, before requests are summed:
all the tokens of a long request go to ``long_tokens`` and those of the
others to ``tokens``. The contract's ``tokens`` and ``long_context_tokens``
are two disjoint buckets: a request is counted wholly in one of them, and
the record's usage is their sum (contract README, "Long-context tokens").

**Issue segments.** When the requests come with their segment issues (see
:mod:`ai_usage_reporter.segments`), a bucket is also cut by issue. Of the
buckets of one session, model and day, the one holding the day's earliest
request is the **primary** one: it keeps the idempotency key a bucket without
segments has, so a bucket sent before it was split is updated in place.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ai_usage_reporter.model import TokenCounts, UsageEvent


@dataclass(slots=True)
class Bucket:
    tool: str
    session_id: str
    model: str
    usage_date: date
    started_at: datetime
    ended_at: datetime
    provider: str | None = None
    tool_version: str | None = None
    request_count: int = 0
    tokens: TokenCounts = field(default_factory=TokenCounts)
    long_tokens: TokenCounts = field(default_factory=TokenCounts)
    git_branch: str | None = None
    repo_url: str | None = None
    cwds: Counter[str] = field(default_factory=Counter)
    #: The sum of the requests' reported costs, and their currencies. The sum
    #: is only meaningful when ``cost_complete`` (every request had a cost).
    cost_total: Decimal = Decimal(0)
    cost_currencies: set[str] = field(default_factory=set)
    cost_complete: bool = True
    #: The issue the agent marked for these requests (see :mod:`ai_usage_reporter.segments`).
    segment_issue: str | None = None
    #: Whether this bucket keeps the key of the whole session/model/day (see the module docs).
    primary_segment: bool = True

    @property
    def reported_cost(self) -> tuple[Decimal, str] | None:
        """The bucket's cost as the tool reported it: every request priced, one currency, above zero."""
        if not self.cost_complete or len(self.cost_currencies) != 1 or self.cost_total <= 0:
            return None
        return self.cost_total, next(iter(self.cost_currencies))

    @property
    def key(self) -> tuple[str, str, str, date]:
        """The session, model and day; the segments of one share it."""
        return (self.tool, self.session_id, self.model, self.usage_date)

    @property
    def cwd(self) -> str | None:
        """The working directory most requests of the bucket ran in (local only)."""
        if not self.cwds:
            return None
        return sorted(self.cwds.items(), key=lambda item: (-item[1], item[0]))[0][0]


ThresholdLookup = Callable[[str], int | None]


def aggregate(
    events: Iterable[UsageEvent],
    threshold_for: ThresholdLookup,
    issues: Sequence[str | None] | None = None,
) -> list[Bucket]:
    """Buckets of *events*; *issues*, when given, holds each event's segment issue (same order)."""
    pairs = list(zip(events, issues, strict=True)) if issues is not None else [(e, None) for e in events]
    buckets: dict[tuple[str, str, str, date, str | None], Bucket] = {}
    for event, issue in sorted(pairs, key=lambda pair: pair[0].timestamp):
        when = event.timestamp.astimezone(UTC)
        key = (event.tool, event.session_id, event.model, when.date(), issue)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = Bucket(
                tool=event.tool,
                session_id=event.session_id,
                model=event.model,
                usage_date=when.date(),
                started_at=when,
                ended_at=when,
                segment_issue=issue,
            )
            buckets[key] = bucket
        bucket.started_at = min(bucket.started_at, when)
        bucket.ended_at = max(bucket.ended_at, when)
        bucket.request_count += max(0, event.requests)
        threshold = threshold_for(event.model)
        if threshold is not None and event.tokens.context > threshold:
            bucket.long_tokens = bucket.long_tokens + event.tokens
        else:
            bucket.tokens = bucket.tokens + event.tokens
        # The latest value seen wins for descriptive fields.
        bucket.provider = event.provider or bucket.provider
        bucket.tool_version = event.tool_version or bucket.tool_version
        bucket.git_branch = event.git_branch or bucket.git_branch
        bucket.repo_url = event.repo_url or bucket.repo_url
        if event.cwd:
            bucket.cwds[event.cwd] += 1
        _add_cost(bucket, event)
    _mark_primary(buckets.values())
    return sorted(buckets.values(), key=lambda b: (b.usage_date, b.tool, b.session_id, b.model, b.segment_issue or ""))


def _mark_primary(buckets: Iterable[Bucket]) -> None:
    """Of each session/model/day's segments, the one with the earliest request is the primary one."""
    groups: dict[tuple[str, str, str, date], list[Bucket]] = {}
    for bucket in buckets:
        groups.setdefault(bucket.key, []).append(bucket)
    for group in groups.values():
        first = min(group, key=lambda b: (b.started_at, b.segment_issue is not None, b.segment_issue or ""))
        for bucket in group:
            bucket.primary_segment = bucket is first


def _add_cost(bucket: Bucket, event: UsageEvent) -> None:
    if event.cost is None or not event.cost_currency:
        bucket.cost_complete = False
        return
    try:
        amount = Decimal(event.cost)
    except InvalidOperation:
        bucket.cost_complete = False
        return
    if not amount.is_finite() or amount < 0:
        bucket.cost_complete = False
        return
    bucket.cost_total += amount
    bucket.cost_currencies.add(event.cost_currency)
