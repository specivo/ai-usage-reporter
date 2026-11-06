"""The ``ai-usage`` command line: ``init``, ``push``, ``status``, ``doctor``, ``show-payload``.

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
from ai_usage_reporter.adapters import load_adapters
from ai_usage_reporter.adapters._jsonl import zstd_available
from ai_usage_reporter.apikey import KeyFileError, keyring_available, resolve_api_key, store_api_key
from ai_usage_reporter.config import (
    CONFIG_FILE,
    Config,
    ConfigError,
    config_dir,
    load_config,
    render_template,
    state_dir,
)
from ai_usage_reporter.enrich import GitInfo
from ai_usage_reporter.pipeline import Collected, Selection, collect, deliver
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
    git = None if getattr(args, "no_git", False) else GitInfo()
    return collect(config, registry, selection=selection, state=state, git=git, session_issues=issues)


def _print_collection(collected: Collected, out: TextIO) -> None:
    for report in collected.reports.values():
        print(
            f"{report.name}: {report.files} files ({report.parsed} parsed, {report.reused} unchanged),"
            f" {report.after_dedup} requests after dedup, {report.malformed_lines} malformed lines",
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
        return SpecivoSink(config.url, _api_key(config, cfg_path))
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
    url = args.url or input("Specivo URL (https://...): ").strip()
    if not url.startswith(("https://", "http://")):
        raise CliError("the URL must start with https://")
    if cfg_path.exists() and not args.force:
        print("The config file exists; left unchanged (use --force to rewrite it).")
    else:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(render_template(url), encoding="utf-8")
        print("Wrote the config file.")
    if args.skip_key:
        return 0
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass("API key (spv_..., not shown): ").strip()
    if not key:
        raise CliError("no API key given")
    where = store_api_key(url, key, cfg_path.parent, use_keyring=not args.no_keyring)
    print("Stored the API key in the OS keychain." if where == "keychain" else "Stored the API key in a private file.")
    if not args.offline:
        sink = SpecivoSink(url, key)
        try:
            info = sink.info()
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
        git = None if args.no_git else GitInfo()
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
    git = None if args.no_git else GitInfo()
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
    print(f"records that would be sent: {len(collected.records)}")
    if config.url and key is not None and not args.offline:
        try:
            info = SpecivoSink(config.url, key.value).info()
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


# -- parser ----------------------------------------------------------------------


def _selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", action="append", help="only this source (repeatable): claude-code, codex, ...")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
