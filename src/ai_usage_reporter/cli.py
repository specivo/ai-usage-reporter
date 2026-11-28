"""The ``ai-usage`` command line.

``init``, ``push``, ``status``, ``doctor``, ``show-payload``, ``time``, ``import ccusage``,
``hooks install|uninstall``, ``schedule install|remove``, and ``hook`` /
``hook-push``, which the AI tools' hooks run.

Nothing printed here contains a prompt, tool output, a transcript path or a
working directory. ``show-payload`` and ``push --dry-run`` print exactly the
records a push would send now (new or changed since the last push, then the
queue); with ``--all``, every collected record. The work activity records a push
would send follow the usage records, in their own envelope
(``specivo.agent-activity.v1``), after a heading on standard error.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from ai_usage_reporter import CONTRACT_VERSION, __version__
from ai_usage_reporter import schedule as schedules
from ai_usage_reporter.activity_record import ACTIVITY_SCHEMA, BUILT_LEVEL, LEVELS
from ai_usage_reporter.adapters import Registry, load_adapters
from ai_usage_reporter.adapters._jsonl import zstd_available
from ai_usage_reporter.apikey import KeyFileError, keyring_available, resolve_api_key, store_api_key
from ai_usage_reporter.config import (
    CONFIG_FILE,
    Config,
    ConfigError,
    check_server_url,
    config_dir,
    load_config,
    render_template,
    state_dir,
)
from ai_usage_reporter.enrich import GitInfo
from ai_usage_reporter.hooks import HOOK_TOOLS, HookContext, HookError
from ai_usage_reporter.hooks import install as install_hook
from ai_usage_reporter.hooks import uninstall as uninstall_hook
from ai_usage_reporter.hooks.runner import run_hook, run_hook_push
from ai_usage_reporter.importers.ccusage import CcusageFormatError, import_report
from ai_usage_reporter.pipeline import (
    MEASURED_DAYS,
    ActivityDelivery,
    Collected,
    DeliverySummary,
    Selection,
    activity_known_unsupported,
    anchor_sent,
    collect,
    deliver,
    deliver_activity,
    gate_like_last_push,
    native_sources,
    select_activity,
    select_to_send,
)
from ai_usage_reporter.schedule import KINDS, ScheduleContext, ScheduleError, default_kind
from ai_usage_reporter.sinks import Sink, SinkContext, make_batch, plugin_sinks
from ai_usage_reporter.sinks.json_sink import JsonSink
from ai_usage_reporter.sinks.specivo import SinkError, SpecivoSink
from ai_usage_reporter.state import CONFLICT, PENDING, REJECTED, STATE_FILE, State
from ai_usage_reporter.time_report import GROUPINGS, render_table, summarise, to_json

ISSUE_ENV = "SPECIVO_ISSUE"
_ISSUE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}-[1-9][0-9]{0,8}$")


class CliError(Exception):
    pass


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    cfg = Path(args.config) if args.config else config_dir() / CONFIG_FILE
    return cfg, state_dir() / STATE_FILE


def _load(args: argparse.Namespace) -> tuple[Config, Path, Path]:
    cfg_path, state_path = _paths(args)
    try:
        return load_config(cfg_path), cfg_path, state_path
    except ConfigError as exc:
        raise CliError(f"config: {exc}") from None


def _selection(args: argparse.Namespace) -> Selection:
    since: date | None = None
    if getattr(args, "since", None):
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            raise CliError("--since takes a date, YYYY-MM-DD") from None
    return Selection(sources=tuple(args.source or ()), session=args.session, since=since)


def _check_sources(selection: Selection) -> None:
    registry = load_adapters()
    unknown = [s for s in selection.sources if registry.get(s) is None]
    if unknown:
        raise CliError(f"unknown source(s): {', '.join(unknown)}; known: {', '.join(registry.adapters)}")


def _session_issues(state: State | None, selection: Selection, env: dict[str, str], *, store: bool) -> dict[str, str]:
    issues = state.session_issues() if state is not None else {}
    issue = env.get(ISSUE_ENV, "").strip()
    if selection.session and issue:
        if not _ISSUE.match(issue):
            raise CliError(f"{ISSUE_ENV} is not an issue key like ACME-12")
        issues[selection.session] = issue.upper()
        if store and state is not None:
            state.set_session_issue(selection.session, issue.upper())
    return issues


def _collect(
    args: argparse.Namespace,
    config: Config,
    state: State | None,
    selection: Selection,
    *,
    store_hints: bool,
    registry: Registry | None = None,
) -> Collected:
    """Collect as a push does: with the session hints and git hints, so a preview builds the same records.

    ``doctor``, ``show-payload`` and ``push --dry-run`` call it on a scratch copy of the state
    (*store_hints* false), so their "would send" counts match what a push sends.
    """
    registry = registry or load_adapters()
    issues = _session_issues(state, selection, dict(os.environ), store=store_hints)
    git = _git(args, config)
    return collect(config, registry, selection=selection, state=state, git=git, session_issues=issues)


def _git(args: argparse.Namespace, config: Config) -> GitInfo | None:
    """git hints, unless turned off by --no-git or git_enrichment = false."""
    if getattr(args, "no_git", False) or not config.git_enrichment:
        return None
    return GitInfo()


def _print_collection(collected: Collected, out: TextIO) -> None:
    for report in collected.reports.values():
        busy = f", {report.busy_files} busy (read next time)" if report.busy_files else ""
        print(
            f"{report.name}: {report.files} files ({report.parsed} parsed, {report.reused} unchanged),"
            f" {report.after_dedup} requests after dedup, {report.malformed_lines} malformed lines{busy}",
            file=out,
        )
        for message in report.schema_errors:
            print(f"  skipped: {message}", file=out)
        for note in report.notes:
            print(f"  note: {note}", file=out)
    print(f"{len(collected.records)} records, {len(collected.activity)} work activity records", file=out)
    if collected.too_old:
        print(f"{collected.too_old} buckets older than the server keeps were left out", file=out)
    if collected.invalid:
        print(f"{collected.invalid} buckets could not be expressed as records and were left out", file=out)


def _api_key(config: Config, cfg_path: Path) -> str:
    try:
        key = resolve_api_key(config.url, cfg_path.parent)
    except KeyFileError as exc:
        raise CliError(str(exc)) from None
    if key is None:
        raise CliError("no API key: run 'ai-usage init', or set SPECIVO_API_KEY")
    return key.value


def _make_sink(args: argparse.Namespace, config: Config, cfg_path: Path) -> Sink:
    name = args.sink
    if args.dry_run or name == "json":
        return JsonSink(output=Path(args.output) if args.output else None)
    if name == "specivo":
        if not config.url:
            raise CliError("no server URL: run 'ai-usage init' or set url in the config file")
        try:
            return SpecivoSink(config.url, _api_key(config, cfg_path), allow_insecure_http=config.allow_insecure_http)
        except SinkError as exc:
            raise CliError(str(exc)) from None
    factories = plugin_sinks()
    if name not in factories:
        raise CliError(f"unknown sink {name!r}")
    key = None
    if config.url:
        try:
            resolved = resolve_api_key(config.url, cfg_path.parent)
        except KeyFileError:
            resolved = None
        key = resolved.value if resolved else None
    return factories[name](SinkContext(url=config.url, api_key=key, output=Path(args.output) if args.output else None))


# -- commands ---------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path, _ = _paths(args)
    url = args.url
    if not url and cfg_path.exists() and not args.force:
        try:
            url = load_config(cfg_path).url
        except ConfigError as exc:
            raise CliError(str(exc)) from None
    if not url:
        if not sys.stdin.isatty() or args.key_stdin:
            raise CliError("no server URL: pass --url https://...")
        url = input("Specivo URL (https://...): ").strip()
    try:
        url = check_server_url(url, allow_insecure_http=args.allow_insecure_http)
    except ConfigError as exc:
        raise CliError(str(exc)) from None
    if cfg_path.exists() and not args.force:
        print("The config file exists; left unchanged (use --force to rewrite it).")
    else:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(render_template(url, allow_insecure_http=args.allow_insecure_http), encoding="utf-8")
        print("Wrote the config file.")
    if args.skip_key:
        return 0
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            raise CliError("no terminal to ask for the API key; pipe it in with --key-stdin")
        try:
            key = getpass.getpass("API key (spv_..., not shown): ").strip()
        except EOFError:
            raise CliError("no API key given; pipe it in with --key-stdin") from None
    if not key:
        raise CliError("no API key given")
    if not key.startswith("spv_") or any(ch.isspace() for ch in key):
        raise CliError("that is not a Specivo API key (they start with spv_); nothing was stored")
    where = store_api_key(url, key, cfg_path.parent, use_keyring=not args.no_keyring)
    print("Stored the API key in the OS keychain." if where == "keychain" else "Stored the API key in a private file.")
    if not args.offline:
        try:
            info = SpecivoSink(url, key, allow_insecure_http=args.allow_insecure_http).info()
        except SinkError as exc:
            raise CliError(f"server check failed: {exc}") from None
        print(f"Server OK: accepts {', '.join(info.versions)}.")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    config, cfg_path, state_path = _load(args)
    selection = _selection(args)
    _check_sources(selection)
    if args.dry_run:
        # Nothing is written: no cache, no ledger, no captured hints. On a copy of the state: what a push
        # would send now.
        with State.scratch_copy(state_path) as scratch:
            collected = _collect(args, config, scratch, selection, store_hints=False)
            wires, preview = _would_send(collected, scratch, selection, everything=args.all)
            activity_wires, activity_preview = _would_send_activity(collected, scratch, wires, everything=args.all)
        sink = _make_sink(args, config, cfg_path)
        sink.send(wires)
        send_activity = getattr(sink, "send_activity", None)
        if activity_wires and callable(send_activity):
            _activity_heading()
            send_activity(activity_wires)
        _print_collection(collected, sys.stderr)
        _print_preview(preview, everything=args.all)
        _print_activity_preview(activity_preview, everything=args.all)
        return 0
    sink = _make_sink(args, config, cfg_path)
    with State(state_path) as state:
        run = state.start_run(sink.name)
        state.commit()
        collected = _collect(args, config, state, selection, store_hints=True)
        try:
            summary = deliver(
                collected.records,
                sink,
                state,
                retry_rejected=args.retry_rejected,
                include_queue=selection.session is None,
                queue_tools=selection.sources,
            )
        except SinkError as exc:
            state.finish_run(run, {"error": str(exc)})
            state.commit()
            raise CliError(str(exc)) from None
        # After the usage, so every anchor exists; never fails the push.
        activity = deliver_activity(collected.activity, sink, state, retry_rejected=args.retry_rejected)
        sources = {
            name: {"schema_errors": report.schema_errors, "notes": report.notes}
            for name, report in collected.reports.items()
            if report.schema_errors or report.notes
        }
        state.finish_run(
            run,
            {
                **summary.as_dict(),
                "too_old": collected.too_old,
                "invalid": collected.invalid,
                "sources": sources,
                "activity": activity.as_dict(),
            },
        )
        state.commit()
    _print_collection(collected, sys.stderr)
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(summary.statuses.items())) or "nothing to send"
    print(
        f"sent {summary.sent} ({statuses}); {summary.skipped_unchanged} unchanged since the last push,"
        f" {summary.skipped_final} refused earlier",
        file=sys.stderr,
    )
    if summary.errors:
        print("errors: " + ", ".join(f"{k} {v}" for k, v in sorted(summary.errors.items())), file=sys.stderr)
    if summary.warnings:
        print("warnings: " + ", ".join(f"{k} {v}" for k, v in sorted(summary.warnings.items())), file=sys.stderr)
    if summary.agent_log_resent or summary.agent_log_left_out:
        print(
            f"{summary.agent_log_resent + summary.agent_log_left_out} records went without the issue read from the"
            " agent's log (the server does not take it, or could not use it)",
            file=sys.stderr,
        )
    if summary.active_time_resent:
        print(
            f"{summary.active_time_resent} records went without their active time (the server refused it;"
            " check the clock difference with ai-usage doctor)",
            file=sys.stderr,
        )
    _print_activity_delivery(activity)
    return 1 if summary.statuses.get("pending") else 0


def _print_activity_delivery(activity: ActivityDelivery) -> None:
    if activity.error:
        print(f"work activity: not sent ({activity.error}); it is tried again on the next push", file=sys.stderr)
        return
    if activity.supported is None:
        return
    if not activity.supported:
        print("work activity: the server takes none; nothing was sent", file=sys.stderr)
        return
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(activity.statuses.items())) or "nothing to send"
    line = (
        f"work activity: sent {activity.sent} ({statuses}); {activity.skipped_unchanged} unchanged,"
        f" {activity.skipped_final} refused earlier"
    )
    if activity.waiting:
        line += f", {activity.waiting} waiting for their usage record"
    print(line, file=sys.stderr)
    if activity.errors:
        print("  errors: " + ", ".join(f"{k} {v}" for k, v in sorted(activity.errors.items())), file=sys.stderr)
    if activity.warnings:
        print("  warnings: " + ", ".join(f"{k} {v}" for k, v in sorted(activity.warnings.items())), file=sys.stderr)


def cmd_show_payload(args: argparse.Namespace) -> int:
    config, _cfg_path, state_path = _load(args)
    selection = _selection(args)
    _check_sources(selection)
    with State.scratch_copy(state_path) as scratch:
        collected = _collect(args, config, scratch, selection, store_hints=False)
        wires, preview = _would_send(collected, scratch, selection, everything=args.all)
        activity_wires, activity_preview = _would_send_activity(collected, scratch, wires, everything=args.all)
    batches = [wires[i : i + 500] for i in range(0, len(wires), 500)] or [[]]
    for batch in batches:
        print(json.dumps(make_batch(batch), indent=2, ensure_ascii=False))
    if activity_wires:
        _activity_heading()
        for start in range(0, len(activity_wires), 500):
            batch = activity_wires[start : start + 500]
            print(json.dumps(make_batch(batch, ACTIVITY_SCHEMA), indent=2, ensure_ascii=False))
    _print_preview(preview, everything=args.all)
    _print_activity_preview(activity_preview, everything=args.all)
    return 0


def _activity_heading() -> None:
    """The heading before the activity envelopes; on standard error, so standard output stays JSON."""
    sys.stdout.flush()
    print(
        f"# work activity records ({ACTIVITY_SCHEMA}), sent after the usage records to a server that takes them:",
        file=sys.stderr,
        flush=True,
    )


def _would_send_activity(
    collected: Collected, state: State, usage_wires: Sequence[dict[str, Any]], *, everything: bool
) -> tuple[list[dict[str, Any]], ActivityDelivery]:
    """The activity records a push would send now, assuming the usage records it sends are accepted."""
    wires = [r.to_wire() for r in collected.activity]
    summary = ActivityDelivery(records=len(wires))
    if not everything and wires and activity_known_unsupported(state):
        summary.supported = False
        return [], summary
    if not everything:
        sending = {w["idempotency_key"] for w in usage_wires}
        stored = anchor_sent(state)
        wires = select_activity(wires, state, summary, anchored=lambda key: key in sending or stored(key))
    summary.sent = len(wires)
    return wires, summary


def _print_activity_preview(summary: ActivityDelivery, *, everything: bool) -> None:
    if not summary.records:
        return
    if everything:
        print(f"{summary.sent} work activity records collected (--all)", file=sys.stderr)
        return
    if summary.supported is False:
        print(
            f"{summary.records} work activity records collected; the server took none at the last push,"
            " so a push sends none (--all shows them)",
            file=sys.stderr,
        )
        return
    line = (
        f"{summary.sent} work activity records would be sent to a server that takes them;"
        f" {summary.skipped_unchanged} unchanged since the last push, {summary.skipped_final} refused earlier"
    )
    if summary.waiting:
        line += f", {summary.waiting} waiting for their usage record"
    print(line, file=sys.stderr)


def _would_send(
    collected: Collected, state: State, selection: Selection, *, everything: bool
) -> tuple[list[dict[str, Any]], DeliverySummary]:
    """The records a push would send now (``select_to_send`` on *state*), or every collected one."""
    wires = [r.to_wire() for r in collected.records]
    summary = DeliverySummary(records=len(wires))
    if not everything:
        wires = select_to_send(
            gate_like_last_push(wires, state),
            state,
            summary,
            include_queue=selection.session is None,
            queue_tools=selection.sources,
        )
    summary.sent = len(wires)
    return wires, summary


def _print_preview(summary: DeliverySummary, *, everything: bool) -> None:
    if everything:
        print(f"{summary.sent} records collected (--all: including those a push would skip)", file=sys.stderr)
        return
    print(
        f"{summary.sent} records would be sent ({summary.retried_from_queue} from the queue);"
        f" {summary.skipped_unchanged} unchanged since the last push, {summary.skipped_final} refused earlier"
        " (--all shows every collected record)",
        file=sys.stderr,
    )


def _day(value: str | None, flag: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CliError(f"{flag} takes a date, YYYY-MM-DD") from None


def cmd_time(args: argparse.Namespace) -> int:
    """Measured agent time per issue (or day, session, tool), from the records a push would send. Local only."""
    config, _cfg_path, state_path = _load(args)
    until = _day(args.until, "--until")
    since = _day(args.since, "--since")
    if since is None:
        since = (until or datetime.now(UTC).date()) - timedelta(days=MEASURED_DAYS - 1)
    if until is not None and until < since:
        raise CliError("--until is before --since")
    selection = Selection(sources=tuple(args.source or ()), since=since)
    _check_sources(selection)
    # Measured even when active_time = false: nothing leaves the machine here.
    measuring = replace(config, active_time=True)
    with State.scratch_copy(state_path) as scratch:
        collected = _collect(args, measuring, scratch, selection, store_hints=False)
        wires = [r.to_wire() for r in collected.current_records]
        gated = gate_like_last_push(wires, scratch)
    server_refuses = any("active_seconds" in w for w in wires) and not any("active_seconds" in w for w in gated)
    would_send = config.active_time and not server_refuses
    report = summarise(wires, args.by, since=since, until=until)
    if args.json:
        print(json.dumps(to_json(report, since=since, until=until, would_send=would_send), indent=2))
    else:
        span = f"{since.isoformat()} to {until.isoformat() if until else 'today'}"
        print(f"Measured agent time, UTC days {span}, by {args.by}:", file=sys.stderr, flush=True)
        if report.rows:
            sys.stdout.write(render_table(report))
            sys.stdout.flush()
        else:
            print("no records in this period", file=sys.stderr)
        print(
            "wall clock: the union of the agents' active intervals; agent time: their sum (parallel agents add up)."
            " not measured: records of sources without active time.",
            file=sys.stderr,
        )
    if not config.active_time:
        print(
            "active_time = false in the config: these values are measured here and would not be sent.", file=sys.stderr
        )
    elif server_refuses:
        print("the server took no active time at the last push: a push would not send these values.", file=sys.stderr)
    return 0


def _local_time(value: str | None, config: Config) -> str:
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if config.timezone:
        try:
            from zoneinfo import ZoneInfo

            return moment.astimezone(ZoneInfo(config.timezone)).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            pass
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _print_problem_buckets(state: State, out: TextIO, limit: int = 20) -> None:
    for status, title in ((CONFLICT, "attribution conflicts"), (REJECTED, "refused records"), (PENDING, "retry queue")):
        rows = list(state.buckets(status))
        if not rows:
            continue
        print(f"{title} ({len(rows)}):", file=out)
        for row in rows[:limit]:
            print(
                f"  {row.usage_date} {row.tool} {row.model} key {row.idempotency_key[:12]}"
                f" {row.error_code or ''} {row.error_message or ''}".rstrip(),
                file=out,
            )
        if len(rows) > limit:
            print(f"  ... and {len(rows) - limit} more", file=out)
    resolved = [r for r in state.buckets() if r.drop_explicit and r.status not in (CONFLICT,)]
    if resolved:
        print(
            f"{len(resolved)} records were sent without issue_ref/project_key after an attribution_conflict"
            " (the entry in Specivo keeps the attribution it had; move it there if needed)",
            file=out,
        )


def cmd_status(args: argparse.Namespace) -> int:
    config, _cfg_path, state_path = _load(args)
    if not state_path.exists():
        print("Nothing pushed yet.")
        return 0
    with State(state_path) as state:
        last = state.last_run()
        if last:
            print(f"last push: {_local_time(last['started_at'], config)} to {last['sink']}")
            summary = last["summary"]
            if "error" in summary:
                print(f"  failed: {summary['error']}")
            else:
                print(f"  sent {summary.get('sent', 0)}: {summary.get('statuses', {})}")
        counts = state.status_counts()
        print("buckets: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
        _print_problem_buckets(state, sys.stdout)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config, cfg_path, state_path = _load(args)
    problems = 0
    print(f"ai-usage-reporter {__version__}, contract {CONTRACT_VERSION}")
    print(f"config file: {'found' if cfg_path.exists() else 'missing (run ai-usage init)'} in {cfg_path.parent}")
    print(f"state: {state_path.parent}")
    print(f"server URL: {config.url or 'not set'}")
    try:
        key = resolve_api_key(config.url, cfg_path.parent)
        print(f"API key: {'from the ' + key.source if key else 'not found'}")
        problems += key is None
    except KeyFileError as exc:
        print(f"API key: {exc}")
        problems += 1
        key = None
    print(f"keychain support: {'yes' if keyring_available() else 'no (install the keyring extra)'}")
    print(f"zstd support: {'yes' if zstd_available() else 'no (compressed Codex rollouts are skipped)'}")
    registry = load_adapters()
    for error in registry.errors:
        print(f"plugin: {error}")
        problems += 1
    preview: DeliverySummary | None = None
    if state_path.exists():
        # On a copy of the state, collected as a push collects (with the session and git hints, so the
        # records and their hashes are the ones a push compares with the ledger).
        with State.scratch_copy(state_path) as scratch:
            collected = _collect(args, config, scratch, Selection(), store_hints=False, registry=registry)
            _wires, preview = _would_send(collected, scratch, Selection(), everything=False)
    else:
        collected = collect(config, registry, git=None)
    for report in collected.reports.values():
        enabled = config.source(report.name).enabled
        print(f"source {report.name}{'' if enabled else ' (disabled)'}:")
        roots = [_tilde(r) for r in report.roots]
        print(f"  data directories: {', '.join(roots) if roots else 'none found'}")
        print(
            f"  {report.files} files, {report.lines} lines, {report.malformed_lines} malformed lines,"
            f" {report.skipped_events} unusable usage lines, {report.failed_files} unreadable files"
        )
        print(f"  {report.events} requests, {report.after_dedup} after dedup, {report.excluded} excluded")
        if report.thread_events:
            print(
                f"  active time: {report.measured_seconds / 3600:.1f} h measured in the last {MEASURED_DAYS} days,"
                f" {report.unplaced_seconds:.0f} s unplaced (no request to give them to; not sent)"
            )
        else:
            print("  active time: not measured (no thread events from this source)")
        if report.busy_files:
            print(f"  {report.busy_files} files locked by the tool right now; they are read on the next run")
        for message in report.schema_errors:
            print(f"  cannot read: {message}")
            problems += 1
        for note in report.notes:
            print(f"  note: {note}")
    problems += _print_activity_settings(config, collected, args)
    imported = sorted(n for n, s in config.sources.items() if s.via == "ccusage")
    if imported:
        print(f"read through 'ai-usage import ccusage' instead of their logs: {', '.join(imported)}")
    line = f"records collected: {len(collected.records)}"
    if preview is not None:
        line += (
            f"; a push would send {preview.sent} ({preview.skipped_unchanged} unchanged since the last push,"
            f" {preview.skipped_final} refused earlier, {preview.retried_from_queue} from the queue)"
        )
    print(line)
    if config.url and key is not None and not args.offline:
        try:
            sink = SpecivoSink(config.url, key.value, allow_insecure_http=config.allow_insecure_http)
            info = sink.info()
            print(f"server: accepts {', '.join(info.versions)}; up to {info.max_records} records per batch")
            if info.server_time:
                skew = (
                    datetime.now(UTC) - datetime.fromisoformat(info.server_time.replace("Z", "+00:00"))
                ).total_seconds()
                print(f"  clock difference: {skew:+.0f} s" + ("  (more than 10 minutes!)" if abs(skew) > 600 else ""))
                problems += abs(skew) > 600
            activity_info = sink.activity_info()
            if activity_info is None:
                print("  work activity: the server takes none (no activity endpoint); none is sent")
            else:
                print(f"  work activity: takes {ACTIVITY_SCHEMA} at levels {', '.join(activity_info.levels)}")
        except SinkError as exc:
            print(f"server: {exc}")
            problems += 1
    if state_path.exists():
        with State(state_path) as state:
            errors = state.error_counts()
            if errors:
                print("last results by error code: " + ", ".join(f"{k} {v}" for k, v in sorted(errors.items())))
                if errors.get("forbidden") and config.on_behalf_of:
                    print(
                        "  hint: on_behalf_of refusals all read 'forbidden'; map the repository to a project"
                        " ([projects]) to get the precise reason"
                    )
            _print_problem_buckets(state, sys.stdout)
            activity_counts = state.activity_status_counts()
            if activity_counts:
                print("work activity sent: " + ", ".join(f"{k} {v}" for k, v in sorted(activity_counts.items())))
                activity_errors = state.activity_error_counts()
                if activity_errors:
                    print("  by error code: " + ", ".join(f"{k} {v}" for k, v in sorted(activity_errors.items())))
    print("OK" if not problems else f"{problems} problem(s) found")
    return 0 if not problems else 1


def _print_activity_settings(config: Config, collected: Collected, args: argparse.Namespace) -> int:
    """The work activity level, per repository pattern and per source (for doctor); returns 0 (notes only)."""
    unknown = config.activity_level_for(None)
    print(f"work activity: level {config.activity_level} (sessions without a known repository: {unknown})")
    for pattern, level in config.activity_repos.items():
        print(f"  repositories {pattern}: {level}")
    configured = {config.activity_level, *config.activity_repos.values()}
    higher = sorted((lv for lv in configured if LEVELS.index(lv) > LEVELS.index(BUILT_LEVEL)), key=LEVELS.index)
    if higher:
        print(
            f"  note: level {', '.join(higher)} is not built yet; those sessions send {BUILT_LEVEL} records"
            " (no names, commands, paths or commits)"
        )
    if config.activity_repos and (getattr(args, "no_git", False) or not config.git_enrichment):
        print(
            "  note: git enrichment is off, so sessions of tools that do not log their repository (Claude Code)"
            f" have no known repository and get {unknown}"
        )
    for report in collected.reports.values():
        if report.activity_records or report.activity_off:
            print(
                f"  {report.name}: {report.activity_records} records at {BUILT_LEVEL},"
                f" {report.activity_off} left out (level off)"
            )
    return 0


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def cmd_import_ccusage(args: argparse.Namespace) -> int:
    config, cfg_path, state_path = _load(args)
    try:
        if args.file in (None, "-"):
            text = sys.stdin.read()
        else:
            text = Path(args.file).read_text(encoding="utf-8")
        report = json.loads(text)
    except OSError as exc:
        raise CliError(f"cannot read the report: {exc.strerror}") from None
    except ValueError:
        raise CliError("the report is not JSON; run 'ccusage daily --by-agent --json'") from None
    try:
        result = import_report(
            report, config, native_sources(config, load_adapters()), default_agent=args.default_agent
        )
    except CcusageFormatError as exc:
        raise CliError(str(exc)) from None
    summary = (
        f"ccusage: {result.rows} rows, {len(result.records)} records, {result.malformed_rows} malformed rows"
        + (f", {result.too_old} too old" if result.too_old else "")
        + (f", {result.in_future} in the future" if result.in_future else "")
    )
    skipped = ", ".join(f"{tool} {n}" for tool, n in sorted(result.skipped_tools.items()))
    sink = _make_sink(args, config, cfg_path)
    wires = [r.to_wire() for r in result.records]
    if args.dry_run or not sink.records_state:
        if wires:
            sink.send(wires)
        print(summary, file=sys.stderr)
    else:
        with State(state_path) as state:
            run = state.start_run(sink.name)
            try:
                delivered = deliver(
                    result.records, sink, state, retry_rejected=args.retry_rejected, include_queue=False
                )
            except SinkError as exc:
                state.finish_run(run, {"error": str(exc)})
                state.commit()
                raise CliError(str(exc)) from None
            state.finish_run(run, delivered.as_dict())
            state.commit()
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(delivered.statuses.items())) or "nothing to send"
        print(
            f"{summary}; sent {delivered.sent} ({statuses}), {delivered.skipped_unchanged} unchanged", file=sys.stderr
        )
    if skipped:
        print(
            f"skipped rows of tools read from their own logs: {skipped}"
            ' (set [sources.<tool>] via = "ccusage" to import them instead)',
            file=sys.stderr,
        )
    return 0


def _tools(args: argparse.Namespace) -> list[str]:
    chosen: list[str] = []
    for tool in args.tool:
        for name in HOOK_TOOLS if tool == "all" else [tool]:
            if name not in chosen:
                chosen.append(name)
    return chosen


def cmd_hooks(args: argparse.Namespace) -> int:
    ctx = HookContext.from_environment()
    problems = 0
    for tool in _tools(args):
        try:
            change = install_hook(tool, ctx) if args.action == "install" else uninstall_hook(tool, ctx)
        except (HookError, OSError) as exc:
            print(f"{tool}: {exc}", file=sys.stderr)
            problems += 1
            continue
        line = f"{tool}: {change.action} ({_tilde(str(change.path))})"
        if change.backup is not None:
            line += f"; backup {change.backup.name}"
        print(line)
    return 1 if problems else 0


def cmd_schedule(args: argparse.Namespace) -> int:
    kind = args.kind or default_kind()
    ctx = ScheduleContext(
        home=Path.home(),
        env=dict(os.environ),
        command=tuple(HookContext.from_environment().command),
        log=state_dir() / "schedule.log",
        run=schedules.run_command,
    )
    try:
        if args.action == "install":
            result = schedules.install(ctx, kind, args.every)
        else:
            result = schedules.remove(ctx, kind)
    except (ScheduleError, OSError) as exc:
        raise CliError(str(exc)) from None
    detail = f" every {args.every} minutes" if args.action == "install" else ""
    print(f"{kind}: {result.action}{detail}")
    for path in result.files:
        print(f"  {_tilde(str(path))}")
    for command in result.failed:
        print(f"  failed: {command}", file=sys.stderr)
    return 1 if result.failed else 0


def cmd_hook_push(args: argparse.Namespace) -> int:
    return run_hook_push(args.tool, args.session, args.nonce, args.delay, state=state_dir(), push=main)


def hook_main(argv: Sequence[str]) -> int:
    """``ai-usage hook <tool> ...``, run by the AI tools. Never fails, never blocks."""
    try:
        tool = argv[0] if argv else ""
        command = HookContext.from_environment().command
        return run_hook(tool, argv[1:], stdin=sys.stdin, stdout=sys.stdout, command=command, state=state_dir())
    except Exception:
        return 0


# -- parser ----------------------------------------------------------------------


def _selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source", action="append", help="only this source (repeatable): claude-code, codex, gemini-cli, opencode"
    )
    parser.add_argument("--session", help="only this session id")
    parser.add_argument("--since", help="only buckets on or after this UTC day (YYYY-MM-DD)")
    parser.add_argument("--no-git", action="store_true", help="do not run git for repository and commit hints")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-usage", description="Report AI coding-tool usage to Specivo.")
    parser.add_argument("--version", action="version", version=f"ai-usage-reporter {__version__}")
    parser.add_argument("--config", help="the config file (default: config.toml in the user config directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write the config file and store the API key")
    init.add_argument("--url", help="the Specivo server, e.g. https://specivo.example.com")
    init.add_argument("--key-stdin", action="store_true", help="read the API key from standard input")
    init.add_argument("--skip-key", action="store_true", help="do not ask for the API key")
    init.add_argument("--no-keyring", action="store_true", help="store the key in a private file, not the keychain")
    init.add_argument("--force", action="store_true", help="rewrite an existing config file")
    init.add_argument("--offline", action="store_true", help="do not check the server")
    init.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="accept a plain http:// URL to another machine (the API key travels unencrypted; trusted networks only)",
    )
    init.set_defaults(func=cmd_init)

    push = sub.add_parser("push", help="send new and changed usage")
    _selection_args(push)
    push.add_argument(
        "--dry-run", action="store_true", help="print the records a push would send instead of sending; change nothing"
    )
    push.add_argument("--all", action="store_true", help="with --dry-run: every collected record")
    push.add_argument("--sink", default="specivo", help="specivo (default), json or a plugin sink")
    push.add_argument("--output", help="file for the json sink (default: standard output)")
    push.add_argument("--retry-rejected", action="store_true", help="send records the server refused before again")
    push.set_defaults(func=cmd_push)

    status = sub.add_parser("status", help="what was sent, what is waiting, what was refused")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check the setup, the sources and the server")
    doctor.add_argument("--offline", action="store_true", help="do not contact the server")
    doctor.set_defaults(func=cmd_doctor)

    show = sub.add_parser("show-payload", help="print exactly what a push would send now")
    _selection_args(show)
    show.add_argument("--all", action="store_true", help="every collected record, also those a push would skip")
    show.set_defaults(func=cmd_show_payload)

    times = sub.add_parser("time", help="measured agent time per issue, from the local logs (sends nothing)")
    times.add_argument("--since", help="first UTC day (YYYY-MM-DD; default: 6 days before --until or today)")
    times.add_argument("--until", help="last UTC day (YYYY-MM-DD; default: today)")
    times.add_argument(
        "--by", choices=GROUPINGS, default="issue", help="group by issue (default), day, session or tool"
    )
    times.add_argument("--json", action="store_true", help="print JSON, durations in seconds")
    times.add_argument(
        "--source", action="append", help="only this source (repeatable): claude-code, codex, gemini-cli, opencode"
    )
    times.add_argument("--no-git", action="store_true", help="do not run git for repository and commit hints")
    times.set_defaults(func=cmd_time)

    imports = sub.add_parser("import", help="import usage reported by another program")
    imports_sub = imports.add_subparsers(dest="importer", required=True)
    ccusage = imports_sub.add_parser(
        "ccusage", help="import 'ccusage daily --by-agent --json' (a file, or - for stdin)"
    )
    ccusage.add_argument("file", nargs="?", default="-", help="the JSON report (default: standard input)")
    ccusage.add_argument(
        "--default-agent", default="claude", help="the agent of rows without one (default: claude, ccusage's own)"
    )
    ccusage.add_argument("--dry-run", action="store_true", help="print the records instead of sending; change nothing")
    ccusage.add_argument("--sink", default="specivo", help="specivo (default), json or a plugin sink")
    ccusage.add_argument("--output", help="file for the json sink (default: standard output)")
    ccusage.add_argument("--retry-rejected", action="store_true", help="send records the server refused before again")
    ccusage.set_defaults(func=cmd_import_ccusage)

    hooks = sub.add_parser("hooks", help="report each session when it ends, from the AI tool's own hooks")
    hooks.add_argument("action", choices=("install", "uninstall"))
    hooks.add_argument(
        "--tool",
        action="append",
        required=True,
        choices=(*HOOK_TOOLS, "all"),
        help="the tool whose config to change (repeatable)",
    )
    hooks.set_defaults(func=cmd_hooks)

    sched = sub.add_parser("schedule", help="run 'ai-usage push' on a timer")
    sched.add_argument("action", choices=("install", "remove"))
    sched.add_argument("--every", type=int, default=schedules.DEFAULT_MINUTES, help="minutes between pushes (30)")
    sched.add_argument("--kind", choices=KINDS, help="launchd, systemd or schtasks (default: this system's)")
    sched.set_defaults(func=cmd_schedule)

    hook_push = sub.add_parser("hook-push")  # started by "ai-usage hook"; not for people
    hook_push.add_argument("--tool", required=True)
    hook_push.add_argument("--session", required=True)
    hook_push.add_argument("--nonce", required=True)
    hook_push.add_argument("--delay", type=float, default=0.0)
    hook_push.set_defaults(func=cmd_hook_push)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["hook"]:
        # Hooks bypass argparse: a usage error must not become exit code 2 ("block") in the tool.
        return hook_main(arguments[1:])
    args = build_parser().parse_args(arguments)
    try:
        result: int = args.func(args)
        return result
    except CliError as exc:
        print(f"ai-usage: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
