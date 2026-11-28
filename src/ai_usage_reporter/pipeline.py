"""The pipeline: discover, parse (or reuse), deduplicate, measure, bucket, enrich, serialise, send.

``collect`` turns the local logs into contract records; ``deliver`` sends them
through a sink and keeps the ledger in the state database.

**Work activity.** ``collect`` also builds one activity record per session,
thread, UTC day and issue segment (:mod:`ai_usage_reporter.work_activity`): each
tool run, active piece and spawn goes with the request owning it, so it lands
on that request's day and issue segment like its active time. A record is
anchored to the usage record of its session, day and segment with the earliest
request (``usage_key``), and built at the level the config gives the session's
repository (none at ``off``). ``deliver_activity`` sends them after the usage, to
a server that takes them, only once their usage record is in the ledger.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_usage_reporter.activity import Span, attribute_threads, deduplicate_thread_events
from ai_usage_reporter.activity_record import (
    OFF,
    ActivityRecord,
    ActivityRecordError,
    activity_key,
    thread_hash,
)
from ai_usage_reporter.adapters import Adapter, DiscoveryContext, Registry, parse_all
from ai_usage_reporter.aggregate import Activity, aggregate
from ai_usage_reporter.config import Config
from ai_usage_reporter.dedup import resolve
from ai_usage_reporter.enrich import Enricher, GitInfo
from ai_usage_reporter.longcontext import Thresholds
from ai_usage_reporter.model import (
    SourceBusyError,
    SourceFile,
    SourceSchemaError,
    ThreadEvent,
    TokenCounts,
    UsageEvent,
)
from ai_usage_reporter.record import (
    ACTIVE_TIME_FIELDS,
    AGENT_LOG,
    OPTIONAL_FIELDS,
    ContractRecord,
    RecordError,
    canonical_json,
    gate_optional_fields,
    opaque_session_id,
)
from ai_usage_reporter.segments import segment_issues, split_markers
from ai_usage_reporter.sinks import PENDING, Outcome, Sink
from ai_usage_reporter.sinks.specivo import SinkError
from ai_usage_reporter.state import CONFLICT, REJECTED, SENT, FileIdentity, State
from ai_usage_reporter.state import PENDING as STATE_PENDING
from ai_usage_reporter.work_activity import GroupWork, ToolRun, summarise, work_by_thread

#: The contract refuses usage_date older than this many days.
DEFAULT_MAX_AGE_DAYS = 400
#: Error codes that mean "try again later" rather than "this record is wrong".
RETRYABLE_ERRORS = frozenset({"internal_error"})
EXPLICIT_HINTS = ("issue_ref", "issue_ref_source", "project_key")
#: Rejections of a record carrying an agent_log issue after which it is sent again without that issue.
#: A server that knows agent_log drops an unusable one with a warning instead; these come from older ones.
AGENT_LOG_FALLBACK_ERRORS = frozenset({"not_found", "forbidden", "attribution_invalid", "target_not_member"})
#: After a server refused issue_ref_source "agent_log", it is left out for this long, then tried again.
AGENT_LOG_RETRY_AFTER = timedelta(days=7)
AGENT_LOG_META = "agent_log_unsupported"
#: ``doctor`` shows the active time measured in this many most recent UTC days.
MEASURED_DAYS = 7


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
    #: Files in a layout the adapter cannot read, with the reason (the tool's schema changed).
    schema_errors: list[str] = field(default_factory=list)
    #: What the adapter wants the person to know about the files it read.
    notes: list[str] = field(default_factory=list)
    #: Thread events read for active time (after deduplication); 0: the source measures no time.
    thread_events: int = 0
    #: Active seconds of pieces no request owns (not sent), and the active seconds of the records
    #: of the last :data:`MEASURED_DAYS` UTC days.
    unplaced_seconds: float = 0.0
    measured_seconds: float = 0.0
    #: Work activity records built, groups left out because their repository's level is off, and tool runs
    #: no request owns (not sent).
    activity_records: int = 0
    activity_off: int = 0
    activity_unplaced_runs: int = 0


@dataclass(slots=True)
class Collected:
    records: list[ContractRecord]
    reports: dict[str, SourceReport]
    too_old: int = 0
    invalid: int = 0
    #: Work activity records (``specivo.agent-activity.v1``), sent after the usage records.
    activity: list[ActivityRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Part:
    """A piece of a thread's work, given to the request (an index into the collected events) owning it."""

    event: int
    thread: str | None
    run: ToolRun | None = None
    piece: tuple[float, float, str | None] | None = None
    spawn: bool = False

    def moved(self, event: int) -> _Part:
        return _Part(event, self.thread, self.run, self.piece, self.spawn)


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


def _parse_file(adapter: Adapter, src: Any, report: SourceReport) -> list[UsageEvent | ThreadEvent] | None:
    """The file's events, or ``None`` when it could not be read this time (it is not cached then)."""
    try:
        return list(parse_all(adapter, src))
    except SourceBusyError:
        report.busy_files += 1
        return None
    except SourceSchemaError as exc:
        report.failed_files += 1
        report.schema_errors.append(str(exc))
        return None
    except Exception:  # an adapter must not fail the run; the file is retried next time
        report.failed_files += 1
        return None


def _read_sources(
    adapter: Adapter,
    name: str,
    sources: Sequence[SourceFile],
    state: State | None,
    report: SourceReport,
    out: list[UsageEvent | ThreadEvent],
) -> None:
    """Parse each source file (or reuse its cached events) into *out*."""
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
            report.notes.extend(src.stats.notes)
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
        out.extend(file_events)


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
    activity: list[Activity] = []
    markers: list[UsageEvent] = []
    parts: list[_Part] = []
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
        adapter_events: list[UsageEvent | ThreadEvent] = []
        # An adapter that keeps its own reading state (Hermes's counter snapshots) gets this run's state
        # database and instant for the time of its parses, and nothing outside them.
        bind = getattr(adapter, "bind_run", None)
        if callable(bind):
            bind(state, now)
        try:
            _read_sources(adapter, name, sources, state, report, adapter_events)
        finally:
            if callable(bind):
                bind(None, None)
        if state is not None:
            state.forget_missing_files(name, {str(s.path) for s in sources})
        thread_events = deduplicate_thread_events(e for e in adapter_events if isinstance(e, ThreadEvent))
        adapter_usage, adapter_markers = split_markers(e for e in adapter_events if isinstance(e, UsageEvent))
        report.events = len(adapter_usage)
        report.thread_events = len(thread_events)
        kept_indices, copy_of = resolve(adapter, adapter_usage)
        owned = _owned_pieces(thread_events, adapter_usage, copy_of, report)
        report.after_dedup = len(kept_indices)
        kept = [i for i in kept_indices if not _excluded(adapter_usage[i].cwd, excluded)]
        report.excluded = len(kept_indices) - len(kept)
        position: dict[int, int] = {}
        for index in kept:
            event = adapter_usage[index]
            position[index] = len(events)
            events.append(event)
            pieces = owned.pieces.get(index)
            measured = pieces is not None or (event.tool, event.session_id) in owned.sessions
            activity.append(tuple(pieces or ()) if measured else None)
        markers.extend(m for m in adapter_markers if not _excluded(m.cwd, excluded))
        parts.extend(_work_parts(thread_events, adapter_usage, copy_of, position, report))
    if selection.session is not None:
        wanted = selection.session
        # A record holds a session's full totals, so naming a child session (a
        # subagent) selects the whole parent session it is folded into.
        chosen = {
            (e.tool, e.session_id)
            for e in events
            if wanted in (e.session_id, opaque_session_id(e.session_id), e.sub_session_id)
        }
        picked = [i for i, e in enumerate(events) if (e.tool, e.session_id) in chosen]
        events = [events[i] for i in picked]
        activity = [activity[i] for i in picked]
        moved = {old: new for new, old in enumerate(picked)}
        parts = [part.moved(moved[part.event]) for part in parts if part.event in moved]
    oldest = (now - timedelta(days=max_age_days - 1)).date()
    issues = segment_issues(events, markers) if config.agent_log_issues else None
    buckets = aggregate(events, thresholds, issues, activity)
    recent = (now - timedelta(days=MEASURED_DAYS - 1)).date()
    for bucket in buckets:
        if bucket.tool in reports and bucket.usage_date >= recent:
            reports[bucket.tool].measured_seconds += bucket.active_seconds
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
    #: (tool, session, day, segment issue) -> the record of the bucket with the earliest request.
    anchors: dict[tuple[str, str, date, str | None], tuple[tuple[datetime, str], ContractRecord]] = {}
    for bucket in buckets:
        record = enricher.record(bucket)
        try:
            record.to_wire()
        except RecordError:
            invalid += 1
            continue
        records.append(record)
        group = (bucket.tool, bucket.session_id, bucket.usage_date, bucket.segment_issue)
        rank = (bucket.started_at, bucket.model)
        if group not in anchors or rank < anchors[group][0]:
            anchors[group] = (rank, record)
    work = build_activity(config, parts, events, issues, {k: v[1] for k, v in anchors.items()}, reports, now)
    if state is not None:
        records.extend(superseded_records(records, state))
        work.extend(superseded_activity(work, state))
        state.commit()
    return Collected(records=records, reports=reports, too_old=too_old, invalid=invalid, activity=work)


def _work_parts(
    thread_events: Sequence[ThreadEvent],
    usage: Sequence[UsageEvent],
    copy_of: Sequence[int | None],
    position: Mapping[int, int],
    report: SourceReport,
) -> list[_Part]:
    """The runs, pieces and spawns of every thread, each given to the collected event that owns it.

    As with active time, a part owned by a request dropped as a copy goes with the copy that is counted,
    and a part of a request that is not collected (excluded, or its copy is missing) is left out.
    """
    out: list[_Part] = []
    for (_tool, _session, thread), work in work_by_thread(thread_events, usage):
        report.activity_unplaced_runs += work.unplaced_runs

        def collected(index: int) -> int | None:
            target = copy_of[index]
            return position.get(target) if target is not None else None

        for index, run in work.runs:
            if (event := collected(index)) is not None:
                out.append(_Part(event, thread, run=run))
        for index, start, end, label in work.pieces:
            if (event := collected(index)) is not None:
                out.append(_Part(event, thread, piece=(start, end, label)))
        for index in work.spawns:
            if (event := collected(index)) is not None:
                out.append(_Part(event, thread, spawn=True))
    return out


def build_activity(
    config: Config,
    parts: Sequence[_Part],
    events: Sequence[UsageEvent],
    issues: Sequence[str | None] | None,
    anchors: Mapping[tuple[str, str, date, str | None], ContractRecord],
    reports: Mapping[str, SourceReport],
    now: datetime,
) -> list[ActivityRecord]:
    """One activity record per session, thread, UTC day and issue segment that has work and an anchor record."""
    groups: dict[tuple[str, str, str | None, date, str | None], GroupWork] = {}
    for part in parts:
        event = events[part.event]
        day = event.timestamp.astimezone(UTC).date()
        issue = issues[part.event] if issues is not None else None
        group = groups.setdefault((event.tool, event.session_id, part.thread, day, issue), GroupWork())
        if part.run is not None:
            group.runs.append(part.run)
        if part.piece is not None:
            group.pieces.append(part.piece)
        group.spawns += int(part.spawn)
    out: list[ActivityRecord] = []
    levels: dict[str, str] = {}
    for (tool, session, thread, day, issue), group in sorted(
        groups.items(), key=lambda item: (item[0][3], item[0][0], item[0][1], item[0][2] or "", item[0][4] or "")
    ):
        anchor = anchors.get((tool, session, day, issue))
        if anchor is None:
            continue  # its usage is not reported (too old, before --since, or not expressible)
        if anchor.idempotency_key not in levels:
            repo = (anchor.to_wire().get("attribution") or {}).get("repo")
            levels[anchor.idempotency_key] = config.activity_sent_level(repo if isinstance(repo, str) else None)
        level = levels[anchor.idempotency_key]
        report = reports.get(tool)
        if level == OFF:
            if report is not None:
                report.activity_off += 1
            continue
        wire_session = anchor.session_id or session
        thread_id = thread_hash(tool, wire_session, thread) if thread is not None else None
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        window = ((start - timedelta(days=1)).timestamp(), min(start + timedelta(days=2), now).timestamp())
        summary = summarise(group, window)
        record = ActivityRecord(
            idempotency_key=activity_key(tool, wire_session, thread_id, day, issue),
            usage_key=anchor.idempotency_key,
            tool=tool,
            session_id=wire_session,
            usage_date=day,
            level=level,
            categories=summary.categories,
            thread=thread_id,
            thread_kind="subagent" if thread is not None else "main",
            files_changed=summary.files_changed,
            lines_added=summary.lines_added,
            lines_removed=summary.lines_removed,
            subagents_spawned=summary.subagents_spawned,
            test_runs=(summary.test_runs, summary.test_failed),
            timeline=summary.timeline,
            on_behalf_of=config.on_behalf_of,
        )
        try:
            record.to_wire()
        except ActivityRecordError:
            continue
        if report is not None:
            report.activity_records += 1
        out.append(record)
    return out


def superseded_activity(records: Sequence[ActivityRecord], state: State) -> list[ActivityRecord]:
    """Empty summaries for activity records sent earlier that today's groups no longer have.

    Like :func:`superseded_records`: when a session's issue split changes, a group sent before can be
    left without work; it is sent again empty, so Specivo does not keep its old counts. Only groups of
    a session and day present in *records* are looked at (a session held at ``off`` is never touched).
    """
    current = {r.idempotency_key for r in records}
    days = {(r.tool, r.session_id, r.usage_date.isoformat()) for r in records}
    out: list[ActivityRecord] = []
    for row in state.activities(SENT):
        if row.idempotency_key in current or (row.tool, row.session_id, row.usage_date) not in days:
            continue
        try:
            day = date.fromisoformat(row.usage_date)
        except ValueError:
            continue
        payload = row.payload
        thread = payload.get("thread")
        kind = payload.get("thread_kind")
        behalf = payload.get("on_behalf_of")
        out.append(
            ActivityRecord(
                idempotency_key=row.idempotency_key,
                usage_key=row.usage_key,
                tool=row.tool,
                session_id=row.session_id,
                usage_date=day,
                thread=thread if isinstance(thread, str) else None,
                thread_kind=kind if isinstance(kind, str) else None,
                files_changed=0,
                lines_added=0,
                lines_removed=0,
                subagents_spawned=0,
                test_runs=(0, 0),
                timeline=(),
                on_behalf_of=behalf if isinstance(behalf, str) else None,
            )
        )
    return out


@dataclass(slots=True)
class _Owned:
    #: Index of a kept usage event -> the pieces it owns.
    pieces: dict[int, list[Span]] = field(default_factory=dict)
    #: The sessions (tool, session id) whose time was measured.
    sessions: set[tuple[str, str]] = field(default_factory=set)


def _owned_pieces(
    thread_events: Sequence[ThreadEvent],
    usage: Sequence[UsageEvent],
    copy_of: Sequence[int | None],
    report: SourceReport,
) -> _Owned:
    """The active time pieces of every thread, given to the kept copy of the request that owns each.

    A piece owned by a request dropped as a copy goes with the copy that is
    counted, wherever its tokens go; a request whose counted copy is not among
    the events takes its pieces with it.
    """
    owned = attribute_threads(thread_events, usage)
    result = _Owned(sessions=owned.measured_sessions)
    report.unplaced_seconds += sum(owned.unplaced.values())
    for index, pieces in owned.pieces.items():
        target = copy_of[index]
        if target is not None:
            result.pieces.setdefault(target, []).extend(pieces)
    return result


def superseded_records(records: Sequence[ContractRecord], state: State) -> list[ContractRecord]:
    """Zero totals for buckets sent earlier that today's segments no longer have.

    A session/model/day's usage is split among its segment buckets. When the
    split changes (the agent's first mark moved earlier usage to its issue, or
    ``agent_log_issues`` was switched off), a key sent before can be left
    without usage. Its entry in Specivo would still hold the old totals and be
    counted twice, so it is sent again with no tokens. Only buckets of a
    session/model/day present in *records* are looked at.
    """
    current = {r.idempotency_key for r in records}
    groups = {
        (r.tool, opaque_session_id(r.session_id), r.model, r.usage_date.isoformat()) for r in records if r.session_id
    }
    out: list[ContractRecord] = []
    for row in state.buckets():
        if row.idempotency_key in current or row.status not in (SENT, CONFLICT, STATE_PENDING):
            continue
        if (row.tool, row.session_id, row.model, row.usage_date) not in groups:
            continue
        payload = row.payload
        try:
            day = date.fromisoformat(row.usage_date)
        except ValueError:
            continue
        out.append(
            ContractRecord(
                idempotency_key=row.idempotency_key,
                tool=row.tool,
                model=row.model,
                usage_date=day,
                tokens=TokenCounts(),
                tool_version=payload.get("tool_version"),
                provider=payload.get("provider"),
                session_id=row.session_id,
                on_behalf_of=payload.get("on_behalf_of"),
                # The session's context stays as it was sent, so zeroing changes only the totals.
                parent_session_id=payload.get("parent_session_id"),
                unattended=payload.get("unattended") is True,
                profile=payload.get("profile"),
                route=payload.get("route"),
                # A bucket sent with active time is cleared to zero time, like its tokens.
                **_zero_active_time(payload),
            )
        )
    return out


def _zero_active_time(payload: Mapping[str, Any]) -> dict[str, Any]:
    version = payload.get("active_time_version")
    if not all(name in payload for name in ACTIVE_TIME_FIELDS) or not isinstance(version, int):
        return {}
    return {"active_time_version": version, "active_seconds": 0, "active_spans": ()}


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
    #: Records sent again without their agent_log issue after a rejection.
    agent_log_resent: int = 0
    #: Records sent without their agent_log issue because the server does not take it.
    agent_log_left_out: int = 0
    #: Records sent again without their active time after the server refused it.
    active_time_resent: int = 0

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
            "agent_log_resent": self.agent_log_resent,
            "agent_log_left_out": self.agent_log_left_out,
            "active_time_resent": self.active_time_resent,
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


def has_agent_log_issue(wire: Mapping[str, Any]) -> bool:
    attribution = wire.get("attribution")
    return isinstance(attribution, Mapping) and attribution.get("issue_ref_source") == AGENT_LOG


def strip_agent_log(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The record without an issue read from the agent's log; every other hint stays."""
    out = dict(wire)
    if not has_agent_log_issue(out):
        return out
    attribution = {k: v for k, v in out["attribution"].items() if k not in ("issue_ref", "issue_ref_source")}
    if attribution:
        out["attribution"] = attribution
    else:
        out.pop("attribution", None)
    return out


def refuses_agent_log_source(outcome: Outcome) -> bool:
    """The server does not know issue_ref_source "agent_log" (a server older than the value)."""
    if outcome.status != "rejected" or outcome.error_code != "invalid_record":
        return False
    return "issue_ref_source" in f"{outcome.error_field or ''} {outcome.error_message or ''}"


def has_active_time(wire: Mapping[str, Any]) -> bool:
    return any(name in wire for name in ACTIVE_TIME_FIELDS)


def strip_active_time(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The record without its measured active time; everything else stays."""
    return {key: value for key, value in wire.items() if key not in ACTIVE_TIME_FIELDS}


def refuses_active_time(outcome: Outcome) -> bool:
    """The server rejected the record because of one of the active time fields."""
    return (
        outcome.status == "rejected"
        and outcome.error_code == "invalid_record"
        and (outcome.error_field or "").startswith("active_")
    )


def _agent_log_fallback(outcome: Outcome) -> bool:
    return refuses_agent_log_source(outcome) or (
        outcome.status == "rejected" and outcome.error_code in AGENT_LOG_FALLBACK_ERRORS
    )


def _agent_log_unsupported(sink: Sink, state: State | None, now: datetime) -> bool:
    """Whether to leave agent_log issues out: the server says it does not take them, or refused them lately."""
    check = getattr(sink, "supports_issue_ref_source", None)
    if callable(check):
        answer = check(AGENT_LOG)
        if answer is not None:
            return not answer
    if state is None:
        return False
    raw = state.meta(AGENT_LOG_META)
    if not raw:
        return False
    try:
        memo = json.loads(raw)
        until = datetime.fromisoformat(memo["until"])
    except (ValueError, KeyError, TypeError):
        return False
    return memo.get("url") == getattr(sink, "url", None) and now < until


def _agent_log_supported(sink: Sink) -> bool:
    """Whether the server says it takes agent_log issues (it lists the value in its info)."""
    check = getattr(sink, "supports_issue_ref_source", None)
    return bool(callable(check) and check(AGENT_LOG))


def _remember_agent_log_refused(sink: Sink, state: State, now: datetime) -> None:
    memo = {"url": getattr(sink, "url", None), "until": (now + AGENT_LOG_RETRY_AFTER).isoformat()}
    state.set_meta(AGENT_LOG_META, json.dumps(memo))


#: State meta key: the optional record fields the server accepted at the last push.
ACCEPTED_FIELDS_META = "accepted_optional_fields"


def gate_like_last_push(wires: Sequence[dict[str, Any]], state: State) -> list[dict[str, Any]]:
    """*wires* without the optional fields the server did not accept at the last push (all kept before one)."""
    raw = state.meta(ACCEPTED_FIELDS_META)
    if not raw:
        return list(wires)
    try:
        fields = frozenset(json.loads(raw))
    except (ValueError, TypeError):
        return list(wires)
    return [gate_optional_fields(w, fields) for w in wires]


def select_to_send(
    wires: Sequence[dict[str, Any]],
    ledger: State,
    summary: DeliverySummary,
    *,
    built: Mapping[str, str] | None = None,
    upgrade: bool = False,
    retry_rejected: bool = False,
    include_queue: bool = True,
    queue_tools: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The records a push sends: new or changed ones, then the queue; counts what it skips in *summary*.

    A record whose bucket was sent (or conflicted) with the same payload is
    unchanged; one the server refused for good is skipped unless
    *retry_rejected*. With *include_queue*, records waiting in the queue (of
    *queue_tools*, when given) follow. *built* maps a key to the hash of the
    record as built, when what is sent is a reduced copy; with *upgrade*, a
    record carrying an agent_log issue goes again when the sent copy had none.
    ``show-payload`` and ``push --dry-run`` call this on a scratch copy of the
    state, so they show what a push would send.
    """
    built = built or {}
    to_send: list[dict[str, Any]] = []
    current = set()
    for wire in wires:
        key = wire["idempotency_key"]
        current.add(key)
        row = ledger.bucket(key)
        same = row is not None and row.payload_hash in (payload_hash(wire), built.get(key))
        if same and row is not None and upgrade and has_agent_log_issue(wire):
            same = has_agent_log_issue(row.payload)
        if row is not None and same and row.status in (SENT, CONFLICT):
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
    return to_send


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
    accepted = getattr(sink, "accepted_optional_fields", None)
    if callable(accepted) and any(name in w for w in wires for name in OPTIONAL_FIELDS):
        # Before hashing: a bucket sent without them goes again once the server lists them.
        fields = accepted()
        wires = [gate_optional_fields(w, fields) for w in wires]
        if state is not None and sink.records_state:
            # So that show-payload and push --dry-run, which stay offline, gate the same way.
            state.set_meta(ACCEPTED_FIELDS_META, json.dumps(sorted(fields)))
    ledger = state if (state is not None and sink.records_state) else None
    now = datetime.now(UTC)
    #: The hash of each record as built, when what is sent differs from it.
    built: dict[str, str] = {}
    if ledger is not None and any(has_agent_log_issue(w) for w in wires) and _agent_log_unsupported(sink, ledger, now):
        # The server does not take agent_log issues: the records go without them.
        summary.agent_log_left_out = sum(1 for w in wires if has_agent_log_issue(w))
        built = {w["idempotency_key"]: payload_hash(w) for w in wires if has_agent_log_issue(w)}
        wires = [strip_agent_log(w) for w in wires]
    if ledger is None:
        to_send = wires
    else:
        # A record sent without its agent_log issue (to a server that did not take them) goes again
        # with the issue once the server says it takes them, even if the bucket did not change.
        to_send = select_to_send(
            wires,
            ledger,
            summary,
            built=built,
            upgrade=not built and _agent_log_supported(sink),
            retry_rejected=retry_rejected,
            include_queue=include_queue,
            queue_tools=queue_tools,
        )
    summary.sent = len(to_send)
    outcomes = sink.send(to_send) if to_send else []
    fallback: list[dict[str, Any]] = []
    untimed: list[dict[str, Any]] = []
    if ledger is not None:
        by_key = {w["idempotency_key"]: w for w in to_send}
        kept: list[Outcome] = []
        for outcome in outcomes:
            sent = by_key.get(outcome.idempotency_key)
            if sent is not None and has_agent_log_issue(sent) and _agent_log_fallback(outcome):
                fallback.append(sent)
                if refuses_agent_log_source(outcome):
                    _remember_agent_log_refused(sink, ledger, now)
            elif sent is not None and has_active_time(sent) and refuses_active_time(outcome):
                untimed.append(sent)
            else:
                kept.append(outcome)
        outcomes = kept
    conflicts = _record(outcomes, to_send, ledger, summary, hashes=built)
    if fallback and ledger is not None:
        # Send once more without the issue read from the agent's log; the ledger keeps the hash of the
        # record as built, so an unchanged bucket is not sent twice on the next push.
        stripped = [strip_agent_log(w) for w in fallback]
        summary.agent_log_resent = len(stripped)
        hashes = {w["idempotency_key"]: payload_hash(w) for w in fallback}
        again = sink.send(stripped)
        for outcome in again:
            outcome.warnings = [*outcome.warnings, "agent_log_dropped"]
        conflicts.extend(_record(again, stripped, ledger, summary, hashes=hashes))
    if untimed and ledger is not None:
        # The server refused the measured time (a clock ahead of the server's, a stricter rule): the tokens
        # still go, without the three fields. The ledger keeps the hash of the record as built, so the bucket
        # is tried with its time again only when it changes.
        stripped = [strip_active_time(w) for w in untimed]
        summary.active_time_resent = len(stripped)
        hashes = {w["idempotency_key"]: payload_hash(w) for w in untimed}
        again = sink.send(stripped)
        for outcome in again:
            outcome.warnings = [*outcome.warnings, "active_time_dropped"]
        conflicts.extend(_record(again, stripped, ledger, summary, hashes=hashes))
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
    hashes: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Count the outcomes and save them in the ledger; return the wires refused with attribution_conflict.

    *hashes* replaces the payload hash saved for a key: the hash of the record as
    built, when what was sent is a reduced copy of it.
    """
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
        digest = (hashes or {}).get(outcome.idempotency_key) or payload_hash(wire)
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


# -- work activity delivery ----------------------------------------------------------------------------

#: Activity errors after which the record is sent again on a later push.
ACTIVITY_RETRY_ERRORS = frozenset({"usage_not_found", "internal_error"})
#: State meta key: whether the server took work activity at the last push (so previews show what a push sends).
ACTIVITY_SUPPORTED_META = "activity_supported"


def activity_known_unsupported(state: State) -> bool:
    """Whether the last push found that the server takes no work activity."""
    raw = state.meta(ACTIVITY_SUPPORTED_META)
    try:
        return raw is not None and json.loads(raw).get("supported") is False
    except (ValueError, AttributeError):
        return False


@dataclass(slots=True)
class ActivityDelivery:
    """What happened to the work activity records of a push."""

    records: int = 0
    #: Whether the server takes work activity; ``None`` when it was not asked (nothing to send).
    supported: bool | None = None
    sent: int = 0
    skipped_unchanged: int = 0
    skipped_final: int = 0
    #: Records held back because their usage record is not in the ledger as sent (yet).
    waiting: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    warnings: Counter[str] = field(default_factory=Counter)
    #: Why the activity could not be sent at all (the usage push is not affected).
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "supported": self.supported,
            "sent": self.sent,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_final": self.skipped_final,
            "waiting": self.waiting,
            "statuses": dict(self.statuses),
            "errors": dict(self.errors),
            "warnings": dict(self.warnings),
            "error": self.error,
        }


def anchor_sent(ledger: State) -> Callable[[str], bool]:
    """Whether the usage record with this key is stored on the server (sent, or kept there after a conflict)."""

    def check(key: str) -> bool:
        row = ledger.bucket(key)
        return row is not None and row.status in (SENT, CONFLICT)

    return check


def select_activity(
    wires: Sequence[dict[str, Any]],
    ledger: State,
    summary: ActivityDelivery,
    *,
    anchored: Callable[[str], bool],
    retry_rejected: bool = False,
) -> list[dict[str, Any]]:
    """The activity records a push sends: new or changed ones whose usage record is *anchored*.

    ``show-payload`` and ``push --dry-run`` call this on a scratch copy of the state.
    """
    out: list[dict[str, Any]] = []
    for wire in wires:
        row = ledger.activity(wire["idempotency_key"])
        if row is not None and row.status == SENT and row.payload_hash == payload_hash(wire):
            summary.skipped_unchanged += 1
            continue
        if row is not None and row.status == REJECTED and not retry_rejected and row.payload_hash == payload_hash(wire):
            summary.skipped_final += 1
            continue
        if not anchored(wire["usage_key"]):
            summary.waiting += 1
            continue
        out.append(wire)
    return out


def deliver_activity(
    records: Sequence[ActivityRecord], sink: Sink, state: State | None, *, retry_rejected: bool = False
) -> ActivityDelivery:
    """Send the work activity records after the usage push (see the module docs); never fails the push."""
    summary = ActivityDelivery(records=len(records))
    send = getattr(sink, "send_activity", None)
    if not records or not callable(send):
        return summary
    wires = [r.to_wire() for r in records]
    ledger = state if (state is not None and sink.records_state) else None
    try:
        supported = getattr(sink, "activity_supported", None)
        summary.supported = bool(supported()) if callable(supported) else True
        if ledger is not None:
            memo = {"url": getattr(sink, "url", None), "supported": summary.supported}
            ledger.set_meta(ACTIVITY_SUPPORTED_META, json.dumps(memo))
            ledger.commit()
        if not summary.supported:
            return summary
        if ledger is None:
            to_send = wires
        else:
            to_send = select_activity(
                wires, ledger, summary, anchored=anchor_sent(ledger), retry_rejected=retry_rejected
            )
        summary.sent = len(to_send)
        outcomes: list[Outcome] = send(to_send) if to_send else []
    except SinkError as exc:
        summary.error = str(exc)
        return summary
    by_key = {w["idempotency_key"]: w for w in to_send}
    for outcome in outcomes:
        summary.statuses[outcome.status] += 1
        for code in outcome.warnings:
            summary.warnings[code] += 1
        if outcome.error_code and not outcome.accepted:
            summary.errors[outcome.error_code] += 1
        wire = by_key.get(outcome.idempotency_key)
        if ledger is None or wire is None:
            continue
        digest = payload_hash(wire)
        if outcome.accepted:
            ledger.save_activity(wire, digest, SENT)
        elif outcome.status == PENDING or outcome.error_code in ACTIVITY_RETRY_ERRORS:
            ledger.save_activity(wire, digest, STATE_PENDING, error_code=outcome.error_code)
        else:
            ledger.save_activity(
                wire, digest, REJECTED, error_code=outcome.error_code, error_message=outcome.error_message
            )
    if ledger is not None:
        ledger.commit()
    return summary
