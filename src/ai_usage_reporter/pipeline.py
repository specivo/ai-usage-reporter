"""The pipeline: discover, parse (or reuse), deduplicate, bucket, enrich, serialise, send.

``collect`` turns the local logs into contract records; ``deliver`` sends them
through a sink and keeps the ledger in the state database.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_usage_reporter.adapters import Adapter, DiscoveryContext, Registry
from ai_usage_reporter.aggregate import aggregate
from ai_usage_reporter.config import Config
from ai_usage_reporter.dedup import deduplicate
from ai_usage_reporter.enrich import Enricher, GitInfo
from ai_usage_reporter.longcontext import Thresholds
from ai_usage_reporter.model import SourceBusyError, UsageEvent
from ai_usage_reporter.record import ContractRecord, RecordError, canonical_json, opaque_session_id
from ai_usage_reporter.sinks import PENDING, Outcome, Sink
from ai_usage_reporter.state import CONFLICT, REJECTED, SENT, FileIdentity, State
from ai_usage_reporter.state import PENDING as STATE_PENDING

#: The contract refuses usage_date older than this many days.
DEFAULT_MAX_AGE_DAYS = 400
#: Error codes that mean "try again later" rather than "this record is wrong".
RETRYABLE_ERRORS = frozenset({"internal_error"})
EXPLICIT_HINTS = ("issue_ref", "issue_ref_source", "project_key")


@dataclass(frozen=True, slots=True)
class Selection:
    sources: tuple[str, ...] = ()
    session: str | None = None
    since: date | None = None


@dataclass(slots=True)
class SourceReport:
    name: str
    roots: list[str] = field(default_factory=list)
    files: int = 0
    parsed: int = 0
    reused: int = 0
    lines: int = 0
    malformed_lines: int = 0
    skipped_events: int = 0
    events: int = 0
    after_dedup: int = 0
    excluded: int = 0
    failed_files: int = 0
    #: Files the tool held locked; they are read on the next run.
    busy_files: int = 0


@dataclass(slots=True)
class Collected:
    records: list[ContractRecord]
    reports: dict[str, SourceReport]
    too_old: int = 0
    invalid: int = 0


ContextFactory = Callable[[Adapter, tuple[Path, ...]], DiscoveryContext]


def default_context(adapter: Adapter, roots: tuple[Path, ...]) -> DiscoveryContext:
    return DiscoveryContext.from_environment(roots)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _excluded(cwd: str | None, prefixes: Sequence[str]) -> bool:
    if not cwd or not prefixes:
        return False
    target = _norm(cwd)
    return any(target == p or target.startswith(p.rstrip(os.sep) + os.sep) for p in prefixes)


def _parse_file(adapter: Adapter, src: Any, report: SourceReport) -> list[UsageEvent] | None:
    """The file's events, or ``None`` when it could not be read this time (it is not cached then)."""
    try:
        return list(adapter.parse(src))
    except SourceBusyError:
        report.busy_files += 1
        return None
    except Exception:  # an adapter must not fail the run; the file is retried next time
        report.failed_files += 1
        return None


def native_sources(config: Config, registry: Registry) -> tuple[str, ...]:
    """The adapters that read their tool's own logs: enabled, and not handed over to an import."""
    return tuple(n for n in registry.adapters if config.source(n).enabled and config.source(n).via == "native")


def collect(
    config: Config,
    registry: Registry,
    *,
    selection: Selection | None = None,
    state: State | None = None,
    context_for: ContextFactory = default_context,
    git: GitInfo | None = None,
    session_issues: Mapping[str, str] | None = None,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Collected:
    now = now or datetime.now(UTC)
    selection = selection or Selection()
    thresholds = Thresholds(config.long_context)
    # A tool whose usage comes from an import ("via") is never read natively, even when asked for.
    names = tuple(
        n for n in (selection.sources or native_sources(config, registry)) if config.source(n).via == "native"
    )
    excluded = [_norm(str(Path(p).expanduser())) for p in config.exclude_paths]
    reports: dict[str, SourceReport] = {}
    events: list[UsageEvent] = []
    for name in names:
        adapter = registry.get(name)
        if adapter is None:
            continue
        report = SourceReport(name=name)
        reports[name] = report
        roots = tuple(Path(r).expanduser() for r in config.source(name).roots)
        ctx = context_for(adapter, roots)
        sources = list(adapter.discover(ctx))
        report.roots = sorted({str(src.root) for src in sources})
        report.files = len(sources)
        adapter_events: list[UsageEvent] = []
        for src in sources:
            try:
                identity = FileIdentity.of(src.path, src.companions)
            except OSError:
                report.failed_files += 1
                continue
            cached = state.cached_events(src.path, identity, adapter.parser_version) if state else None
            if cached is not None:
                report.reused += 1
                file_events = cached
            else:
                report.parsed += 1
                parsed = _parse_file(adapter, src, report)
                report.lines += src.stats.lines
                report.malformed_lines += src.stats.malformed_lines
                report.skipped_events += src.stats.skipped_events
                if parsed is None:
                    continue
                file_events = parsed
                if state is not None:
                    state.store_events(
                        src.path,
                        name,
                        identity,
                        adapter.parser_version,
                        file_events,
                        lines=src.stats.lines,
                        malformed_lines=src.stats.malformed_lines,
                        skipped_events=src.stats.skipped_events,
                    )
            adapter_events.extend(file_events)
        if state is not None:
            state.forget_missing_files(name, {str(s.path) for s in sources})
        report.events = len(adapter_events)
        unique = deduplicate(adapter, adapter_events)
        report.after_dedup = len(unique)
        kept = [e for e in unique if not _excluded(e.cwd, excluded)]
        report.excluded = len(unique) - len(kept)
        events.extend(kept)
    if selection.session is not None:
        wanted = selection.session
        # A record holds a session's full totals, so naming a child session (a
        # subagent) selects the whole parent session it is folded into.
        chosen = {
            (e.tool, e.session_id)
            for e in events
            if wanted in (e.session_id, opaque_session_id(e.session_id), e.sub_session_id)
        }
        events = [e for e in events if (e.tool, e.session_id) in chosen]
    oldest = (now - timedelta(days=max_age_days - 1)).date()
    buckets = aggregate(events, thresholds)
    too_old = sum(1 for b in buckets if b.usage_date < oldest)
    buckets = [b for b in buckets if b.usage_date >= oldest]
    if selection.since is not None:
        buckets = [b for b in buckets if b.usage_date >= selection.since]
    enricher = Enricher(
        config=config,
        session_issues=session_issues or {},
        git=git,
        drop_explicit=(lambda key: bool((row := state.bucket(key)) and row.drop_explicit))
        if state
        else lambda k: False,
    )
    records: list[ContractRecord] = []
    invalid = 0
    for bucket in buckets:
        record = enricher.record(bucket)
        try:
            record.to_wire()
        except RecordError:
            invalid += 1
            continue
        records.append(record)
    if state is not None:
        state.commit()
    return Collected(records=records, reports=reports, too_old=too_old, invalid=invalid)


@dataclass(slots=True)
class DeliverySummary:
    records: int = 0
    skipped_unchanged: int = 0
    skipped_final: int = 0
    retried_from_queue: int = 0
    sent: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)
    conflicts_resent: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_final": self.skipped_final,
            "retried_from_queue": self.retried_from_queue,
            "sent": self.sent,
            "statuses": dict(self.statuses),
            "errors": dict(self.errors),
            "warnings": dict(self.warnings),
            "conflicts_resent": self.conflicts_resent,
        }


def payload_hash(wire: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(wire).encode()).hexdigest()


def strip_explicit(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The record without the explicit hints the server refused to apply on a replay."""
    out = dict(wire)
    attribution = dict(out.get("attribution") or {})
    for key in EXPLICIT_HINTS:
        attribution.pop(key, None)
    if attribution:
        out["attribution"] = attribution
    else:
        out.pop("attribution", None)
    return out


def deliver(
    records: Sequence[ContractRecord],
    sink: Sink,
    state: State | None,
    *,
    retry_rejected: bool = False,
    include_queue: bool = True,
    queue_tools: Sequence[str] = (),
) -> DeliverySummary:
    summary = DeliverySummary(records=len(records))
    wires = [r.to_wire() for r in records]
    ledger = state if (state is not None and sink.records_state) else None
    to_send: list[dict[str, Any]] = []
    if ledger is None:
        to_send = wires
    else:
        current = set()
        for wire in wires:
            key = wire["idempotency_key"]
            current.add(key)
            row = ledger.bucket(key)
            digest = payload_hash(wire)
            if row is not None and row.payload_hash == digest and row.status in (SENT, CONFLICT):
                summary.skipped_unchanged += 1
                continue
            if row is not None and row.status == REJECTED and not retry_rejected:
                summary.skipped_final += 1
                continue
            to_send.append(wire)
        if include_queue:
            for row in ledger.buckets(STATE_PENDING):
                if row.idempotency_key in current or (queue_tools and row.tool not in queue_tools):
                    continue
                to_send.append(row.payload)
                summary.retried_from_queue += 1
    summary.sent = len(to_send)
    outcomes = sink.send(to_send) if to_send else []
    conflicts = _record(outcomes, to_send, ledger, summary)
    if conflicts and ledger is not None:
        # Send once more without the explicit hints the stored entry contradicts.
        stripped = [strip_explicit(w) for w in conflicts]
        summary.conflicts_resent = len(stripped)
        again = sink.send(stripped)
        for wire, outcome in zip(stripped, again, strict=False):
            outcome.warnings = [*outcome.warnings, "attribution_conflict"]
            if outcome.status == "rejected" and outcome.error_code == "attribution_conflict":
                ledger.save_bucket(
                    wire,
                    payload_hash(wire),
                    CONFLICT,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message,
                    drop_explicit=True,
                )
                summary.errors[outcome.error_code] += 1
                continue
            _record([outcome], [wire], ledger, summary, drop_explicit=True)
    if ledger is not None:
        ledger.commit()
    return summary


def _record(
    outcomes: Sequence[Outcome],
    wires: Sequence[dict[str, Any]],
    ledger: State | None,
    summary: DeliverySummary,
    *,
    drop_explicit: bool | None = None,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_key = {w["idempotency_key"]: w for w in wires}
    for outcome in outcomes:
        summary.statuses[outcome.status] += 1
        for code in outcome.warnings:
            summary.warnings[code] += 1
        if outcome.error_code and outcome.status not in ("created", "updated", "unchanged"):
            summary.errors[outcome.error_code] += 1
        wire = by_key.get(outcome.idempotency_key)
        if ledger is None or wire is None:
            continue
        digest = payload_hash(wire)
        if outcome.accepted:
            ledger.save_bucket(
                wire, digest, SENT, warnings=outcome.warnings, entry_id=outcome.entry_id, drop_explicit=drop_explicit
            )
        elif outcome.status == PENDING or outcome.error_code in RETRYABLE_ERRORS:
            ledger.save_bucket(wire, digest, STATE_PENDING, error_code=outcome.error_code)
        elif outcome.error_code == "attribution_conflict":
            ledger.save_bucket(
                wire,
                digest,
                CONFLICT,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                drop_explicit=True,
            )
            conflicts.append(wire)
        else:
            ledger.save_bucket(
                wire, digest, REJECTED, error_code=outcome.error_code, error_message=outcome.error_message
            )
    return conflicts
