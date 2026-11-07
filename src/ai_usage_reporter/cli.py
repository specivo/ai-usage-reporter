"""The ``ai-usage`` command line.

``init``, ``push``, ``status``, ``doctor``, ``show-payload``, ``import ccusage``,
``hooks install|uninstall``, ``schedule install|remove``, and ``hook`` /
``hook-push``, which the AI tools' hooks run.

Nothing printed here contains a prompt, tool output, a transcript path or a
working directory. ``show-payload`` and ``push --dry-run`` print exactly the
records that would be sent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO

from ai_usage_reporter import CONTRACT_VERSION, __version__
from ai_usage_reporter import schedule as schedules
from ai_usage_reporter.adapters import load_adapters
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
from ai_usage_reporter.pipeline import Collected, Selection, collect, deliver, native_sources
from ai_usage_reporter.schedule import KINDS, ScheduleContext, ScheduleError, default_kind
from ai_usage_reporter.sinks import Sink, SinkContext, make_batch, plugin_sinks
from ai_usage_reporter.sinks.json_sink import JsonSink
from ai_usage_reporter.sinks.specivo import SinkError, SpecivoSink
from ai_usage_reporter.state import CONFLICT, PENDING, REJECTED, State

ISSUE_ENV = "SPECIVO_ISSUE"
STATE_FILE = "state.db"
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
    args: argparse.Namespace, config: Config, state: State | None, selection: Selection, *, store_hints: bool
) -> Collected:
    registry = load_adapters()
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
    print(f"{len(collected.records)} records", file=out)
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
        # Nothing is written: no cache, no ledger, no captured hints.
        existing = State(state_path) if state_path.exists() else None
        try:
            hints = _session_issues(existing, selection, dict(os.environ), store=False)
        finally:
            if existing is not None:
                existing.close()
        git = _git(args, config)
        collected = collect(config, load_adapters(), selection=selection, git=git, session_issues=hints)
        sink = _make_sink(args, config, cfg_path)
        sink.send([r.to_wire() for r in collected.records])
        _print_collection(collected, sys.stderr)
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
        state.finish_run(run, {**summary.as_dict(), "too_old": collected.too_old, "invalid": collected.invalid})
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
    return 1 if summary.statuses.get("pending") else 0


def cmd_show_payload(args: argparse.Namespace) -> int:
    config, _cfg_path, state_path = _load(args)
    selection = _selection(args)
    _check_sources(selection)
    existing = State(state_path) if state_path.exists() else None
    try:
        hints = _session_issues(existing, selection, dict(os.environ), store=False)
    finally:
        if existing is not None:
            existing.close()
    git = _git(args, config)
    collected = collect(config, load_adapters(), selection=selection, git=git, session_issues=hints)
    wires = [r.to_wire() for r in collected.records]
    batches = [wires[i : i + 500] for i in range(0, len(wires), 500)] or [[]]
    for batch in batches:
        print(json.dumps(make_batch(batch), indent=2, ensure_ascii=False))
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
        if report.busy_files:
            print(f"  {report.busy_files} files locked by the tool right now; they are read on the next run")
    imported = sorted(n for n, s in config.sources.items() if s.via == "ccusage")
    if imported:
        print(f"read through 'ai-usage import ccusage' instead of their logs: {', '.join(imported)}")
    print(f"records that would be sent: {len(collected.records)}")
    if config.url and key is not None and not args.offline:
        try:
            info = SpecivoSink(config.url, key.value, allow_insecure_http=config.allow_insecure_http).info()
            print(f"server: accepts {', '.join(info.versions)}; up to {info.max_records} records per batch")
            if info.server_time:
                skew = (
                    datetime.now(UTC) - datetime.fromisoformat(info.server_time.replace("Z", "+00:00"))
                ).total_seconds()
                print(f"  clock difference: {skew:+.0f} s" + ("  (more than 10 minutes!)" if abs(skew) > 600 else ""))
                problems += abs(skew) > 600
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
    print("OK" if not problems else f"{problems} problem(s) found")
    return 0 if not problems else 1


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
    push.add_argument("--dry-run", action="store_true", help="print the records instead of sending; change nothing")
    push.add_argument("--sink", default="specivo", help="specivo (default), json or a plugin sink")
    push.add_argument("--output", help="file for the json sink (default: standard output)")
    push.add_argument("--retry-rejected", action="store_true", help="send records the server refused before again")
    push.set_defaults(func=cmd_push)

    status = sub.add_parser("status", help="what was sent, what is waiting, what was refused")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check the setup, the sources and the server")
    doctor.add_argument("--offline", action="store_true", help="do not contact the server")
    doctor.set_defaults(func=cmd_doctor)

    show = sub.add_parser("show-payload", help="print exactly what would be sent")
    _selection_args(show)
    show.set_defaults(func=cmd_show_payload)

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
