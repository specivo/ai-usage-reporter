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

**Active time.** When the requests come with the pieces of active time they
own (see :mod:`ai_usage_reporter.activity`), a bucket sums their seconds
(``active_seconds``, the agent time of all its threads) and keeps their union
(``active_spans``, its wall clock). A bucket is *measured* when its session
yielded thread events, even if none of its requests owns a piece; it then
sends zero seconds and no spans.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from ai_usage_reporter.activity import Span, merge_spans
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
    #: The session's parent, whether nobody attended it, and its agent profile (constant within a session).
    parent_session: str | None = None
    unattended: bool = False
    profile: str | None = None
    #: The backend the requests went through, and how the tool says they were paid (the latest value wins).
    route: str | None = None
    cost_type: str | None = None
    #: Whether the session's active time was measured, the summed seconds of the pieces its requests own,
    #: and their union (merged, sorted).
    active_measured: bool = False
    active_seconds: float = 0.0
    active_spans: list[Span] = field(default_factory=list)

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


#: The active time pieces an event owns: ``None`` when its session was not measured.
Activity = Sequence[Span] | None


def aggregate(
    events: Iterable[UsageEvent],
    threshold_for: ThresholdLookup,
    issues: Sequence[str | None] | None = None,
    activity: Sequence[Activity] | None = None,
) -> list[Bucket]:
    """Buckets of *events*.

    *issues*, when given, holds each event's segment issue, and *activity* the
    active time pieces each event owns (both in the order of *events*).
    """
    listed = list(events)
    issue_list: Sequence[str | None] = issues if issues is not None else [None] * len(listed)
    activity_list: Sequence[Activity] = activity if activity is not None else [None] * len(listed)
    triples = list(zip(listed, issue_list, activity_list, strict=True))
    buckets: dict[tuple[str, str, str, date, str | None], Bucket] = {}
    for event, issue, owned in sorted(triples, key=lambda triple: triple[0].timestamp):
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
        # An event that sums several requests says when the first of them began (same UTC day).
        start = event.span_start.astimezone(UTC) if event.span_start is not None else when
        bucket.started_at = min(bucket.started_at, start if start.date() == when.date() and start <= when else when)
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
        bucket.parent_session = event.parent_session or bucket.parent_session
        bucket.profile = event.profile or bucket.profile
        bucket.route = event.route or bucket.route
        bucket.cost_type = event.cost_type or bucket.cost_type
        # One unattended request makes the run unattended (a session is one or the other).
        bucket.unattended = bucket.unattended or event.unattended
        if event.cwd:
            bucket.cwds[event.cwd] += 1
        _add_cost(bucket, event)
        if owned is not None:
            bucket.active_measured = True
            bucket.active_seconds += sum(end - start for start, end in owned)
            bucket.active_spans.extend(owned)
    for bucket in buckets.values():
        bucket.active_spans = merge_spans(bucket.active_spans)
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
