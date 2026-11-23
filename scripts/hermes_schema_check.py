"""Compare a Hermes Agent checkout's ``state.db`` DDL with the columns the hermes source reads.

Usage::

    python scripts/hermes_schema_check.py <path to a hermes-agent checkout>

It reads ``persistence/sqlite/_schema_ddl.py`` as text (Hermes code is never
imported or run), finds the ``CREATE TABLE`` statements of the tables the
adapter reads and checks them against the adapter's required and optional
column sets. It fails (exit 1) when a column the adapter reads is gone, or when
Hermes's ``SCHEMA_VERSION`` is newer than the version the fixtures cover, so
that a Hermes release that changes the schema is noticed here before users see
broken numbers. ``make hermes-schema-check`` clones the pinned release and runs it.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

from ai_usage_reporter.adapters.hermes import (
    MESSAGES_MARKS,
    MESSAGES_RESULTS,
    SESSIONS_OPTIONAL,
    SESSIONS_REQUIRED,
    TESTED_SCHEMA_VERSION,
    USAGE_OPTIONAL,
    USAGE_REQUIRED,
)

DDL_FILE = Path("persistence") / "sqlite" / "_schema_ddl.py"
_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(")
_VERSION = re.compile(r"^SCHEMA_VERSION\s*=\s*(\d+)", re.M)
_CONSTRAINTS = ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "--")

EXPECTED: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "session_model_usage": (USAGE_REQUIRED, USAGE_OPTIONAL),
    "sessions": (SESSIONS_REQUIRED, SESSIONS_OPTIONAL),
    "messages": (frozenset(), MESSAGES_MARKS | MESSAGES_RESULTS),
}


def _body(ddl: str, start: int) -> str:
    """The text between the parenthesis opened just before *start* and the one that closes it."""
    depth = 1
    for index in range(start, len(ddl)):
        if ddl[index] == "(":
            depth += 1
        elif ddl[index] == ")":
            depth -= 1
            if depth == 0:
                return ddl[start:index]
    return ddl[start:]


def _split(body: str) -> list[str]:
    """*body* split at its top-level commas (not those inside parentheses)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def table_columns(ddl: str) -> dict[str, set[str]]:
    """The column names of each ``CREATE TABLE IF NOT EXISTS`` statement in *ddl*."""
    tables: dict[str, set[str]] = {}
    for match in _TABLE.finditer(ddl):
        columns: set[str] = set()
        for part in _split(_body(ddl, match.end())):
            lines = [line.strip() for line in part.splitlines() if line.strip() and not line.strip().startswith("--")]
            text = " ".join(lines)
            if not text or text.upper().startswith(_CONSTRAINTS):
                continue
            columns.add(text.split()[0].strip('"'))
        tables[match.group(1)] = columns
    return tables


def check(ddl: str) -> list[str]:
    """The problems found in *ddl*; empty when the adapter reads it as it is."""
    problems: list[str] = []
    match = _VERSION.search(ddl)
    if match is None:
        problems.append("SCHEMA_VERSION not found")
    elif int(match.group(1)) > TESTED_SCHEMA_VERSION:
        problems.append(
            f"SCHEMA_VERSION {match.group(1)} is newer than the fixtures (v{TESTED_SCHEMA_VERSION}):"
            " check the change, add a fixture and raise TESTED_SCHEMA_VERSION"
        )
    tables = table_columns(ddl)
    for table, (required, optional) in EXPECTED.items():
        found = tables.get(table)
        if found is None:
            problems.append(f"table {table} is gone")
            continue
        for column in sorted(required - found):
            problems.append(f"{table}.{column} is gone (required: the database would be skipped)")
        for column in sorted(optional - found):
            problems.append(f"{table}.{column} is gone (optional: the adapter degrades)")
    return problems


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[0]) / DDL_FILE
    problems = check(path.read_text(encoding="utf-8"))
    for problem in problems:
        print(f"hermes schema: {problem}")
    if not problems:
        print(f"hermes schema: the columns the reporter reads are all there (v{TESTED_SCHEMA_VERSION})")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
