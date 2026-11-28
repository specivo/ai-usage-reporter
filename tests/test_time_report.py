"""Grouping and formatting of ``ai-usage time`` (the command itself is tested in test_cli.py)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from ai_usage_reporter.activity import union_seconds
from ai_usage_reporter.time_report import format_duration, group_key, render_table, summarise


def wire(day: str = "2026-10-05", seconds: int | None = None, spans: Any = None, **attribution: str) -> dict[str, Any]:
    out: dict[str, Any] = {"tool": "claude-code", "session_id": "s-1", "usage_date": day}
    if attribution:
        out["attribution"] = attribution
    if seconds is not None:
        out.update({"active_time_version": 1, "active_seconds": seconds, "active_spans": spans or []})
    return out


def test_union_seconds_counts_overlaps_once() -> None:
    assert union_seconds([(0, 10), (5, 15), (20, 25)]) == 20
    assert union_seconds([]) == 0


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 min"), (20, "< 1 min"), (30, "1 min"), (720, "12 min"), (3600, "1 h"), (4320, "1 h 12 min")],
)
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(seconds) == text


def test_group_key_falls_back_from_issue_to_project_to_unassigned() -> None:
    assert group_key(wire(issue_ref="ACME-1", project_key="ACME"), "issue") == ("ACME-1", None)
    assert group_key(wire(project_key="ACME"), "issue") == ("ACME", None)
    assert group_key(wire(), "issue") == ("(unassigned)", None)
    assert group_key(wire(), "session") == ("s-1", "claude-code")
    assert group_key(wire(), "day") == ("2026-10-05", None)
    with pytest.raises(ValueError):
        summarise([], "branch")


def test_measured_zero_is_not_unmeasured_and_days_outside_are_left_out() -> None:
    report = summarise(
        [
            wire(seconds=0, issue_ref="ACME-1"),
            wire(issue_ref="ACME-1"),
            wire(day="2026-10-07", seconds=50, spans=[["2026-10-07T00:00:00Z", "2026-10-07T00:00:50Z"]]),
        ],
        "issue",
        until=date(2026, 10, 6),
    )
    (row,) = report.rows
    assert row.as_dict()["agent_seconds"] == 0 and row.as_dict()["unmeasured_records"] == 1
    assert "0 min" in render_table(report)
