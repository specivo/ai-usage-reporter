"""Buckets: one session of one tool, with one model, on one UTC day.

A bucket holds the complete totals the contract asks for. Buckets are cut at
midnight UTC, whatever the person's time zone, so that the server's price and
exchange-rate lookups for a bucket are the same for everyone.

**Long context.** Some models bill a whole request at higher rates once the
request's context (input plus cache reads and writes) passes a threshold.
That decision is per request, so it is made here, before requests are summed:
the tokens of a long request go to ``long_tokens`` and the others to
``tokens``. The two never overlap (see the README, "Long context").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
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

    @property
    def reported_cost(self) -> tuple[Decimal, str] | None:
        """The bucket's cost as the tool reported it: every request priced, one currency, above zero."""
        if not self.cost_complete or len(self.cost_currencies) != 1 or self.cost_total <= 0:
            return None
        return self.cost_total, next(iter(self.cost_currencies))

    @property
    def key(self) -> tuple[str, str, str, date]:
        return (self.tool, self.session_id, self.model, self.usage_date)

    @property
    def cwd(self) -> str | None:
        """The working directory most requests of the bucket ran in (local only)."""
        if not self.cwds:
            return None
        return sorted(self.cwds.items(), key=lambda item: (-item[1], item[0]))[0][0]


ThresholdLookup = Callable[[str], int | None]


def aggregate(events: Iterable[UsageEvent], threshold_for: ThresholdLookup) -> list[Bucket]:
    buckets: dict[tuple[str, str, str, date], Bucket] = {}
    for event in sorted(events, key=lambda e: e.timestamp):
        when = event.timestamp.astimezone(UTC)
        key = (event.tool, event.session_id, event.model, when.date())
        bucket = buckets.get(key)
        if bucket is None:
            bucket = Bucket(
                tool=event.tool,
                session_id=event.session_id,
                model=event.model,
                usage_date=when.date(),
                started_at=when,
                ended_at=when,
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
    return sorted(buckets.values(), key=lambda b: (b.usage_date, b.tool, b.session_id, b.model))


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
