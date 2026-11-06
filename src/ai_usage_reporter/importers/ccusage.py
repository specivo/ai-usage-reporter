"""``ai-usage import ccusage``: day-level usage from the JSON report of ``ccusage daily --by-agent --json``.

ccusage reads the local logs of many AI tools. Its documented JSON report has
a ``daily`` list (``data`` in some versions; ``projects`` maps project names
to such lists with ``--instances``). A row has ``date`` (or ``period``) and,
with ``--by-agent``, ``agents``: one entry per tool with ``agent`` and
``modelBreakdowns``, each breakdown holding ``modelName``, ``inputTokens``,
``outputTokens``, ``cacheCreationTokens`` and ``cacheReadTokens``. A row
without ``agents`` stands for its own ``agent``, or for ``--default-agent``.

What the import sends:

- One record per tool, model and day, with ``request_count`` 0 and no
  session id: ccusage reports neither. ``inputTokens`` already excludes the
  cache; cache creation is reported as 5-minute cache writes.
- The idempotency key is the contract's recommended one with the session
  ``import-ccusage``, so importing the same day again replaces its totals.
- ccusage's own cost estimate is not sent; Specivo prices the tokens.
- Days are taken as ccusage wrote them. Buckets are UTC days, so run ccusage
  in UTC (``--timezone UTC`` where your version has it).

**One source per tool.** A tool is counted either by its native adapter or by
this import, never both: rows of a tool whose native adapter is enabled are
skipped unless ``[sources.<tool>] via = "ccusage"`` hands the tool over to the
import (which also stops ``push`` from reading its logs).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ai_usage_reporter.config import Config
from ai_usage_reporter.model import TokenCounts, count
from ai_usage_reporter.record import ContractRecord, RecordError, idempotency_key

IMPORT_SESSION = "import-ccusage"
DEFAULT_MAX_AGE_DAYS = 400

#: ccusage agent names that differ from the reporter's tool slugs.
AGENT_TOOLS: dict[str, str] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "gemini": "gemini-cli",
    "gemini-cli": "gemini-cli",
    "opencode": "opencode",
}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOOL = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")


class CcusageFormatError(ValueError):
    """The input is not a ccusage JSON report."""


@dataclass(slots=True)
class ImportResult:
    records: list[ContractRecord] = field(default_factory=list)
    rows: int = 0
    malformed_rows: int = 0
    #: Rows of tools read natively (or switched off), by tool.
    skipped_tools: Counter[str] = field(default_factory=Counter)
    too_old: int = 0
    in_future: int = 0


def tool_slug(agent: str) -> str | None:
    name = agent.strip().lower()
    if name in AGENT_TOOLS:
        return AGENT_TOOLS[name]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:50]
    return slug if _TOOL.match(slug) else None


def _rows(report: object) -> Iterator[object]:
    if isinstance(report, list):
        yield from report
        return
    if not isinstance(report, dict):
        raise CcusageFormatError("not a ccusage JSON report (expected an object with 'daily')")
    found = False
    for key in ("daily", "data"):
        if isinstance(report.get(key), list):
            found = True
            yield from report[key]
    projects = report.get("projects")
    if isinstance(projects, dict):
        for rows in projects.values():
            if isinstance(rows, list):
                found = True
                yield from rows
    if not found:
        raise CcusageFormatError("not a ccusage daily report (no 'daily' list); run 'ccusage daily --by-agent --json'")


def _breakdowns(entry: Mapping[str, Any]) -> Iterator[tuple[str, TokenCounts]]:
    breakdowns = entry.get("modelBreakdowns")
    if isinstance(breakdowns, list) and breakdowns:
        for item in breakdowns:
            if isinstance(item, dict):
                name = item.get("modelName")
                yield (name.strip() if isinstance(name, str) and name.strip() else "unknown"), _tokens(item)
        return
    yield "unknown", _tokens(entry)


def _tokens(item: Mapping[str, Any]) -> TokenCounts:
    return TokenCounts(
        input=count(item.get("inputTokens")),
        output=count(item.get("outputTokens")),
        cache_write_5m=count(item.get("cacheCreationTokens")),
        cache_read=count(item.get("cacheReadTokens")),
    )


def _agents(row: Mapping[str, Any], default_agent: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    agents = row.get("agents")
    if isinstance(agents, list):
        for entry in agents:
            if isinstance(entry, dict) and isinstance(entry.get("agent"), str) and entry["agent"] != "all":
                yield entry["agent"], entry
        return
    agent = row.get("agent")
    if isinstance(agent, str) and agent != "all":
        yield agent, row
    elif agent is None:
        yield default_agent, row


def import_report(
    report: object,
    config: Config,
    native: Iterable[str],
    *,
    default_agent: str = "claude",
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> ImportResult:
    """Contract records from a parsed ccusage report.

    *native* names the tools read by an enabled native adapter; their rows
    are skipped unless the tool is configured with ``via = "ccusage"``.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    oldest = today - timedelta(days=max_age_days - 1)
    native_tools = {t for t in native if config.source(t).via != "ccusage"}
    result = ImportResult()
    totals: dict[tuple[str, str, date], TokenCounts] = {}
    for row in _rows(report):
        result.rows += 1
        if not isinstance(row, dict):
            result.malformed_rows += 1
            continue
        day_text = row.get("date", row.get("period"))
        if not isinstance(day_text, str) or not _DAY.match(day_text):
            result.malformed_rows += 1
            continue
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            result.malformed_rows += 1
            continue
        for agent, entry in _agents(row, default_agent):
            tool = tool_slug(agent)
            if tool is None:
                result.malformed_rows += 1
                continue
            if tool in native_tools or not config.source(tool).enabled:
                result.skipped_tools[tool] += 1
                continue
            if day < oldest:
                result.too_old += 1
                continue
            if day > today:
                result.in_future += 1
                continue
            for model, tokens in _breakdowns(entry):
                if tokens.is_zero():
                    continue
                key = (tool, model, day)
                totals[key] = totals.get(key, TokenCounts()) + tokens
    for (tool, model, day), tokens in sorted(totals.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        rule = config.cost_for(tool, model, None)
        record = ContractRecord(
            idempotency_key=idempotency_key(tool, IMPORT_SESSION, model, day),
            tool=tool,
            model=model,
            usage_date=day,
            tokens=tokens,
            cost_type=rule.cost_type if rule else None,
            subscription_hint=rule.subscription_hint if rule else None,
            on_behalf_of=config.on_behalf_of,
        )
        try:
            record.to_wire()
        except RecordError:
            result.malformed_rows += 1
            continue
        result.records.append(record)
    return result
