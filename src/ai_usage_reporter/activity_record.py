"""The work activity record (``specivo.agent-activity.v1``) and its only serializer.

**Privacy is enforced here**, as for usage records (:mod:`ai_usage_reporter.record`).
:class:`ActivityRecord` has exactly the contract's fields of the levels this
reporter builds (any level and ``counts``): keys, the tool, the session id
(opaque), the UTC day, a hashed thread id, counts, seconds and a labelled
timeline. There is no field that could hold a name, a command, a path or a
commit: the ``names`` and ``commits`` properties of the contract are not built
at all, so a record can never carry a key above its level (the server rejects
such a record, even for a ``null`` value).

:meth:`ActivityRecord.to_wire` builds the JSON object field by field and checks
every value against the contract: categories from the fixed list, counts within
0 to 10^9, ``failed <= count``, timeline segments sorted, not overlapping,
within 24 hours of the day and not in the future (segments that break a rule are
left out). A required value that does not fit raises :class:`ActivityRecordError`.

**Levels** (:data:`LEVELS`): ``off`` (nothing is sent), ``counts``, ``names``,
``commits``. This reporter builds ``counts`` records; a higher configured level
is accepted and sends ``counts``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ai_usage_reporter.record import SPAN_DAY_MARGIN, canonical_json, opaque_session_id
from ai_usage_reporter.work_activity import ACTIVITY_VERSION, CATEGORIES, MAX_TIMELINE

#: The contract version of activity batches.
ACTIVITY_SCHEMA = "specivo.agent-activity.v1"
#: The privacy levels of the config, from the least to the most revealing.
LEVELS: tuple[str, ...] = ("off", "counts", "names", "commits")
OFF = "off"
COUNTS = "counts"
#: The highest level this reporter builds; a higher configured level sends this.
BUILT_LEVEL = COUNTS
DEFAULT_LEVEL = COUNTS
THREAD_KINDS = ("main", "subagent")
MAX_COUNT = 10**9

_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")
_TOOL = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")
_THREAD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ActivityRecordError(ValueError):
    """A required field cannot be expressed in the contract."""


def lowest_level(*levels: str) -> str:
    """The least revealing of *levels*."""
    return min(levels, key=LEVELS.index)


def thread_hash(tool: str, session_id: str, thread: str) -> str:
    """The opaque id sent for a thread (a subagent): never its name, only a hash of it."""
    return hashlib.sha256(f"v1|thread|{tool}|{session_id}|{thread}".encode()).hexdigest()[:32]


def activity_key(tool: str, session_id: str, thread: str | None, usage_date: date, issue: str | None) -> str:
    """The contract's recommended key: SHA-256 of ``v1|activity|<tool>|<session>|<thread or ->|<day>|<issue or ->``."""
    text = f"v1|activity|{tool}|{session_id}|{thread or '-'}|{usage_date.isoformat()}|{issue.upper() if issue else '-'}"
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """One ``counts`` level activity record: the complete summary of one session, thread, UTC day and issue segment.

    ``session_id`` is the opaque id sent for usage, and ``thread`` already the
    hashed thread id (``None`` for the main thread).
    """

    idempotency_key: str
    usage_key: str
    tool: str
    session_id: str
    usage_date: date
    level: str = COUNTS
    activity_version: int = ACTIVITY_VERSION
    #: (category, runs, seconds).
    categories: tuple[tuple[str, int, int], ...] = ()
    thread: str | None = None
    thread_kind: str | None = None
    files_changed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    subagents_spawned: int | None = None
    #: (runs, failed).
    test_runs: tuple[int, int] | None = None
    #: (start, end, category) in whole POSIX seconds.
    timeline: tuple[tuple[int, int, str], ...] | None = None
    on_behalf_of: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return to_wire(self)

    def payload_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_wire()).encode()).hexdigest()


def _count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise ActivityRecordError(f"{name} out of range")
    return value


def _second(value: int) -> str:
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timeline(record: ActivityRecord, now: datetime) -> list[list[str]]:
    day = datetime(record.usage_date.year, record.usage_date.month, record.usage_date.day, tzinfo=UTC)
    low = (day - SPAN_DAY_MARGIN).timestamp()
    high = min(day + timedelta(days=1) + SPAN_DAY_MARGIN, now).timestamp()
    out: list[list[str]] = []
    last_end: int | None = None
    for start, end, category in record.timeline or ():
        if category not in CATEGORIES or not isinstance(start, int) or not isinstance(end, int):
            continue
        if not (low <= start < end <= high) or (last_end is not None and start < last_end):
            continue
        out.append([_second(start), _second(end), category])
        last_end = end
        if len(out) == MAX_TIMELINE:
            break
    return out


def to_wire(record: ActivityRecord, now: datetime | None = None) -> dict[str, Any]:
    """The JSON object of one activity record, built from the allow-list below and nothing else."""
    now = now or datetime.now(UTC)
    if not _KEY.match(record.idempotency_key):
        raise ActivityRecordError("idempotency_key is not printable ASCII of 1..128 characters")
    if not _KEY.match(record.usage_key):
        raise ActivityRecordError("usage_key is not printable ASCII of 1..128 characters")
    if not _TOOL.match(record.tool):
        raise ActivityRecordError("tool is not a valid slug")
    session = opaque_session_id(record.session_id) if record.session_id else ""
    if not 1 <= len(session) <= 200:
        raise ActivityRecordError("session_id is empty or too long")
    if record.level != COUNTS:
        raise ActivityRecordError("this reporter builds counts records only")
    if isinstance(record.activity_version, bool) or record.activity_version != ACTIVITY_VERSION:
        raise ActivityRecordError("activity_version must be 1")
    categories: dict[str, dict[str, int]] = {}
    for name, runs, seconds in record.categories:
        if name not in CATEGORIES or name in categories:
            raise ActivityRecordError("unknown or repeated category")
        categories[name] = {"count": _count(runs, "count"), "seconds": _count(seconds, "seconds")}
    wire: dict[str, Any] = {
        "idempotency_key": record.idempotency_key,
        "usage_key": record.usage_key,
        "tool": record.tool,
        "session_id": session,
        "usage_date": record.usage_date.isoformat(),
        "level": record.level,
        "activity_version": record.activity_version,
        "categories": {name: categories[name] for name in CATEGORIES if name in categories},
    }
    if record.thread is not None:
        if not _THREAD.match(record.thread):
            raise ActivityRecordError("thread is not an opaque id")
        wire["thread"] = record.thread
    if record.thread_kind is not None:
        if record.thread_kind not in THREAD_KINDS:
            raise ActivityRecordError("unknown thread_kind")
        wire["thread_kind"] = record.thread_kind
    for name in ("files_changed", "lines_added", "lines_removed", "subagents_spawned"):
        value = getattr(record, name)
        if value is not None:
            wire[name] = _count(value, name)
    if record.test_runs is not None:
        runs, failed = record.test_runs
        if _count(failed, "test_runs.failed") > _count(runs, "test_runs.count"):
            raise ActivityRecordError("test_runs.failed is more than test_runs.count")
        wire["test_runs"] = {"count": runs, "failed": failed}
    if record.timeline is not None:
        wire["timeline"] = _timeline(record, now)
    behalf = record.on_behalf_of
    if behalf is not None and not _CONTROL.search(behalf) and 1 <= len(behalf) <= 255:
        wire["on_behalf_of"] = behalf
    return wire
