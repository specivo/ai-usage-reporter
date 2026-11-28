"""Work activity: what an agent did on its machine, as counts and durations by category (version 1).

This module is pure and does no I/O. Adapters give each tool call of a
:class:`~ai_usage_reporter.model.ThreadEvent` pair a **category** and a little
local detail (failed or not, lines its patch added and removed, hashes naming
the files it edited); everything else is computed here, from the same thread
events and the same active set as active time (:mod:`ai_usage_reporter.activity`).

**Categories** (fixed, as in the ``specivo.agent-activity.v1`` contract):
``read``, ``search``, ``edit``, ``shell``, ``test``, ``git``, ``web``,
``subagent``, ``mcp``, ``other``.

**Shell commands** (:func:`classify_command`, :func:`classify_argv`): the
command line is split into simple commands at ``;``, ``&&``, ``||``, ``|``,
``&`` and line breaks (quotes respected). In each, leading environment
assignments and wrappers (``sudo``, ``env``, ``time``, ``nice``, ``nohup``,
``timeout``, ``command``, ``exec``, ``uv run``, ``poetry run``, ``npx`` and the
like, ``python -m``) are skipped and the program's base name is taken; a shell
started with ``-c`` (``zsh -lc "..."``) is looked into. ``git`` gives ``git``; a
fixed list of test runners (``pytest``, ``tox``, ``jest``, ``vitest``,
``go test``, ``cargo test``, ``npm test``, ``make test`` and the like) gives
``test``; anything else is ``shell``. A command line counts once, under the most
specific category found: ``test``, then ``git``, then ``shell``. The command
line is read in memory and dropped; only the category is kept.

**Tool runs.** A run is a ``tool_start`` and its ``tool_end``, paired by call id
as active time pairs them. Its **seconds** are the part of its interval (from
the start, at most :data:`~ai_usage_reporter.activity.TOOL_CAP` seconds, cut at
the end of its turn) that lies in the thread's active set: a person answering
counts nothing, a call that waits for a subagent counts nothing (the subagent
counts its own work), and parallel runs each count their own seconds. A call
without a result is no run. A run belongs to the request that owns its start
instant (the owner rule of active time), so it lands on that request's UTC day
and issue segment.

**Timeline** (:func:`build_timeline`): the active pieces a group's requests own
(exactly those of active time) are labelled by the tool run covering them (the
most specific category by :data:`LABEL_ORDER` where runs overlap; a wait for a
subagent shows as ``subagent``); time between
runs (the model working) takes the label of the run before it in the same
stretch of activity, else of the run after it, else ``other``. Neighbours with
the same label are merged, a segment shorter than :data:`MIN_SEGMENT` seconds
that touches another is folded into its longer neighbour, times are rounded to
whole seconds, and beyond :data:`MAX_TIMELINE` segments the closest ones are
joined.

Times inside this module are POSIX seconds (floats).
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import math
import re
import shlex
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ai_usage_reporter.activity import (
    TOOL_CAP,
    Span,
    ThreadKey,
    active_set,
    attribute,
    owner,
    round_seconds,
    timed_events,
)
from ai_usage_reporter.model import PROMPT, SPAWN, TOOL_END, TOOL_START, TURN_END, ThreadEvent, UsageEvent

#: The version of this definition; sent as ``activity_version``.
ACTIVITY_VERSION = 1
#: The contract's categories, in its order.
CATEGORIES: tuple[str, ...] = ("read", "search", "edit", "shell", "test", "git", "web", "subagent", "mcp", "other")
#: Where runs of several categories overlap, the timeline shows the first of these.
LABEL_ORDER: tuple[str, ...] = ("test", "git", "edit", "shell", "mcp", "web", "subagent", "search", "read", "other")
#: Timeline segments shorter than this (seconds) are folded into a neighbour they touch.
MIN_SEGMENT = 30
#: The contract's limit of timeline segments per record.
MAX_TIMELINE = 300

#: Test runners known by their program name alone.
TEST_PROGRAMS = frozenset(
    {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "ava", "rspec", "phpunit", "pest", "ctest",
     "nosetests", "behave", "karma", "unittest"}
)  # fmt: skip
#: Programs whose ``test`` subcommand runs the tests (``go test``, ``cargo test``).
TEST_SUBCOMMAND_PROGRAMS = frozenset(
    {"go", "cargo", "dotnet", "deno", "bun", "mix", "swift", "zig", "mvn", "gradle", "gradlew", "sbt", "lein",
     "stack", "cabal", "flutter", "dart", "playwright", "npm", "yarn", "pnpm", "ng", "nx", "turbo"}
)  # fmt: skip
#: Package managers whose ``run test...`` script runs the tests.
SCRIPT_RUNNERS = frozenset({"npm", "yarn", "pnpm", "bun"})
#: Task runners whose ``test`` target runs the tests (``make test``, ``make test-unit``).
TASK_RUNNERS = frozenset({"make", "gmake", "just", "task", "rake", "invoke", "nmake"})
#: Wrappers skipped before the program, with their options.
WRAPPERS = frozenset(
    {"sudo", "doas", "env", "time", "nice", "nohup", "command", "exec", "builtin", "stdbuf", "caffeinate", "xargs",
     "npx", "bunx", "pnpx", "dotenv"}
)  # fmt: skip
#: ``<tool> run`` (or ``exec``) wrappers: the program follows.
RUN_WRAPPERS = {"uv": ("run",), "poetry": ("run",), "pdm": ("run",), "hatch": ("run",), "rye": ("run",),
                "pipenv": ("run",), "pnpm": ("exec", "dlx"), "yarn": ("exec", "dlx"), "bundle": ("exec",),
                "conda": ("run",), "mamba": ("run",), "pixi": ("run",)}  # fmt: skip
#: Options of wrappers that take a value (the value is skipped too).
VALUE_OPTIONS = frozenset(
    {"-u", "-g", "-p", "--with", "--python", "--project", "--directory", "--extra", "--group", "--package",
     "--env-file", "--from", "--user", "--signal", "--kill-after"}
)  # fmt: skip
PYTHONS = re.compile(r"^(python|pypy)[0-9.]*$")
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "ash"})
#: Words that separate simple commands, as the tokenizer yields them.
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";;", "\n", "(", ")", "{", "}"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SPLIT = re.compile(r"\|\||&&|[;|&\n()]")
#: The most specific category of a command line wins: test, then git, then shell.
_SHELL_RANK = {"test": 3, "git": 2, "shell": 1}
_MAX_DEPTH = 3


# -- classifying commands ---------------------------------------------------------------------------


def classify_command(command: str) -> str:
    """The category of a shell command line: ``test``, ``git`` or ``shell`` (see the module docs)."""
    return _classify_line(command, 0)


def classify_argv(argv: Sequence[str]) -> str:
    """The category of a command given as an argument vector (``["zsh", "-lc", "pytest -q"]``)."""
    return _classify_words([str(w) for w in argv], 0)


def _classify_line(command: str, depth: int) -> str:
    best = "shell"
    for words in _simple_commands(command):
        found = _classify_words(words, depth)
        if _SHELL_RANK[found] > _SHELL_RANK[best]:
            best = found
    return best


def _simple_commands(command: str) -> list[list[str]]:
    """The words of each simple command of *command*; quotes respected where they balance."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: split crudely
        return [part.split() for part in _SPLIT.split(command)]
    out: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [words for words in out if words]


def _base(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _skip_options(words: Sequence[str], index: int) -> int:
    """The index after the options (and their values) and assignments starting at *index*."""
    while index < len(words):
        word = words[index]
        if word == "--":
            return index + 1
        if word.startswith("-") and len(word) > 1:
            index += 2 if word in VALUE_OPTIONS else 1
        elif _ASSIGNMENT.match(word):
            index += 1
        else:
            break
    return index


def _classify_words(words: Sequence[str], depth: int) -> str:
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGNMENT.match(word):
            index += 1
            continue
        base = _base(word)
        rest = words[index + 1 :]
        if base in WRAPPERS:
            index = _skip_options(words, index + 1)
            continue
        if base == "timeout":  # timeout [options] <duration> <program>
            index = _skip_options(words, index + 1) + 1
            continue
        if base in RUN_WRAPPERS and rest and rest[0] in RUN_WRAPPERS[base]:
            index = _skip_options(words, index + 2)
            continue
        if PYTHONS.match(base) and len(rest) >= 2 and rest[0] == "-m":
            index += 2  # python -m pytest: the module is the program
            continue
        if base in SHELLS and depth < _MAX_DEPTH:
            script = _shell_script(rest)
            if script is not None:
                return _classify_line(script, depth + 1)
        return _program_category(base, [w for w in rest if not w.startswith("-")])
    return "shell"


def _shell_script(arguments: Sequence[str]) -> str | None:
    """The script of ``sh -c <script>`` (also ``-lc``, ``-ic``, ``-c -l``), else ``None``."""
    for position, word in enumerate(arguments):
        if not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word[1:]:
            return arguments[position + 1] if position + 1 < len(arguments) else None
    return None


def _program_category(program: str, arguments: Sequence[str]) -> str:
    if program == "git":
        return "git"
    if program in TEST_PROGRAMS:
        return "test"
    first = arguments[0] if arguments else ""
    if program in TEST_SUBCOMMAND_PROGRAMS and first in ("test", "t"):
        return "test"
    if program in SCRIPT_RUNNERS and first == "run" and len(arguments) > 1 and _is_test_target(arguments[1]):
        return "test"
    if program in TASK_RUNNERS and any(_is_test_target(a) for a in arguments):
        return "test"
    return "shell"


def _is_test_target(name: str) -> bool:
    name = name.lower()
    return name in ("test", "tests", "check-tests") or name.startswith(("test-", "test_", "test:"))


# -- local detail ------------------------------------------------------------------------------------


def file_identity(path: str) -> str:
    """A local name for an edited file, so distinct files can be counted without keeping the path."""
    return hashlib.sha256(f"work-activity|file|{path}".encode()).hexdigest()[:20]


def patch_lines(lines: Iterable[object]) -> tuple[int, int]:
    """Added and removed lines of one hunk's ``lines`` (``+``/``-`` prefixed strings)."""
    added = removed = 0
    for line in lines:
        if isinstance(line, str):
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def unified_diff_lines(diff: str) -> tuple[int, int]:
    """Added and removed lines of a unified diff, counting hunk lines only (not the ``---``/``+++`` headers)."""
    added = removed = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith("diff "):
            in_hunk = False
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return added, removed


def text_lines(text: str) -> int:
    """The number of lines of a new file's content."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# -- runs and pieces ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One tool call of a thread, reduced to what the summary needs."""

    start: float
    #: The end as active time counts it: at most TOOL_CAP after the start, cut at the end of its turn.
    stop: float
    category: str
    failed: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    files: tuple[str, ...] = ()
    spawns: bool = False
    #: The part of [start, stop] in the thread's active set.
    seconds: float = 0.0


@dataclass(slots=True)
class ThreadWork:
    """One thread's work: its runs, its labelled active pieces and its spawns, each with the request owning it.

    Owners are indices into the usage times given to :func:`thread_work`.
    """

    runs: list[tuple[int, ToolRun]] = field(default_factory=list)
    #: (owner, start, end, label): the active pieces, cut where the covering run changes; ``None`` is
    #: time no run covers.
    pieces: list[tuple[int, float, float, str | None]] = field(default_factory=list)
    spawns: list[int] = field(default_factory=list)
    #: Runs no request owns (counted for doctor only).
    unplaced_runs: int = 0


def _overlap(span: Span, active: Sequence[Span], starts: Sequence[float]) -> float:
    start, stop = span
    total = 0.0
    index = max(0, bisect_right(starts, start) - 1)
    while index < len(active) and active[index][0] < stop:
        low, high = max(start, active[index][0]), min(stop, active[index][1])
        if high > low:
            total += high - low
        index += 1
    return total


def tool_runs(events: Sequence[ThreadEvent]) -> list[ToolRun]:
    """The tool runs of one thread (see the module docs), sorted by start."""
    ordered = sorted(timed_events(events), key=lambda e: e.timestamp)
    times = [e.timestamp.timestamp() for e in ordered]
    boundaries = [t for e, t in zip(ordered, times, strict=True) if e.kind in (PROMPT, TURN_END)]
    active = active_set(ordered)
    active_starts = [s for s, _ in active]
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for index, event in enumerate(ordered):
        if event.call_id is None:
            continue
        if event.kind == TOOL_START:
            starts.setdefault(event.call_id, index)
        elif event.kind == TOOL_END:
            ends.setdefault(event.call_id, index)
    runs: list[ToolRun] = []
    for call_id, first in starts.items():
        last = ends.get(call_id)
        if last is None:
            continue  # no result: no run
        begin, end = ordered[first], ordered[last]
        start, finish = times[first], times[last]
        if finish < start:
            continue
        stop = min(finish, start + TOOL_CAP)
        boundary = bisect_right(boundaries, start)
        if boundary < len(boundaries):
            stop = min(stop, boundaries[boundary])
        stop = max(stop, start)
        category = begin.category or end.category or "other"
        waits = begin.subagent_wait or end.subagent_wait
        runs.append(
            ToolRun(
                start=start,
                stop=stop,
                category=category if category in CATEGORIES else "other",
                failed=begin.failed or end.failed,
                lines_added=begin.lines_added + end.lines_added,
                lines_removed=begin.lines_removed + end.lines_removed,
                files=tuple(dict.fromkeys((*begin.files, *end.files))),
                spawns=begin.spawns or end.spawns,
                # A wait for a subagent counts nothing (the subagent counts its own work); a person
                # answering is outside the active set already.
                seconds=0.0 if waits else _overlap((start, stop), active, active_starts),
            )
        )
    runs.sort(key=lambda r: (r.start, r.stop))
    return runs


def label_spans(runs: Sequence[ToolRun]) -> list[tuple[float, float, str]]:
    """The time covered by *runs*, cut where the covering set changes, each part labelled by :data:`LABEL_ORDER`."""
    rank = {name: position for position, name in enumerate(LABEL_ORDER)}
    edges: list[tuple[float, int, str]] = []
    for run in runs:
        if run.stop > run.start:
            edges.append((run.start, 1, run.category))
            edges.append((run.stop, -1, run.category))
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    active: Counter[str] = Counter()
    out: list[tuple[float, float, str]] = []
    previous: float | None = None
    for when, step, category in edges:
        if previous is not None and when > previous and +active:
            label = min((name for name, n in active.items() if n > 0), key=lambda name: rank[name])
            if out and out[-1][1] == previous and out[-1][2] == label:
                out[-1] = (out[-1][0], when, label)
            else:
                out.append((previous, when, label))
        active[category] += step
        previous = when
    return out


def _label_piece(
    piece: Span, labels: Sequence[tuple[float, float, str]], label_starts: Sequence[float]
) -> list[tuple[float, float, str | None]]:
    start, end = piece
    out: list[tuple[float, float, str | None]] = []
    current = start
    index = max(0, bisect_right(label_starts, start) - 1)
    while index < len(labels) and labels[index][0] < end:
        low, high, label = labels[index]
        low, high = max(low, start), min(high, end)
        if high > low:
            if low > current:
                out.append((current, low, None))
            out.append((low, high, label))
            current = high
        index += 1
    if current < end:
        out.append((current, end, None))
    return out


def thread_work(events: Sequence[ThreadEvent], usage_times: Sequence[float]) -> ThreadWork:
    """The work of one thread: each run, piece and spawn with the request owning it (an index into *usage_times*)."""
    work = ThreadWork()
    timed = timed_events(events)
    if not events:
        return work
    ordered = sorted(timed, key=lambda e: e.timestamp)
    boundaries = [e.timestamp.timestamp() for e in ordered if e.kind in (PROMPT, TURN_END)]
    order = sorted(range(len(usage_times)), key=lambda i: usage_times[i])
    sorted_usage = [usage_times[i] for i in order]

    def owner_of(instant: float) -> int | None:
        found = owner((instant, instant), sorted_usage, boundaries)
        return None if found is None else order[found]

    runs = tool_runs(timed)
    for run in runs:
        found = owner_of(run.start)
        if found is None:
            work.unplaced_runs += 1
        else:
            work.runs.append((found, run))
    for event in events:
        if event.kind == SPAWN:
            found = owner_of(event.timestamp.timestamp())
            if found is not None:
                work.spawns.append(found)
    labels = label_spans(runs)
    label_starts = [s for s, _, _ in labels]
    for index, piece in attribute(timed, usage_times).owned:
        for start, end, label in _label_piece(piece, labels, label_starts):
            work.pieces.append((index, start, end, label))
    return work


def work_by_thread(
    thread_events: Iterable[ThreadEvent], usage: Sequence[UsageEvent]
) -> list[tuple[ThreadKey, ThreadWork]]:
    """The work of every thread in *thread_events*; owners are indices into *usage* (requests of the same thread)."""
    by_thread: dict[ThreadKey, list[ThreadEvent]] = defaultdict(list)
    for event in thread_events:
        by_thread[(event.tool, event.session_id, event.thread_id)].append(event)
    requests: dict[ThreadKey, list[int]] = defaultdict(list)
    for index, request in enumerate(usage):
        key = (request.tool, request.session_id, request.thread_id)
        if key in by_thread:
            requests[key].append(index)
    out: list[tuple[ThreadKey, ThreadWork]] = []
    for key, events in by_thread.items():
        indices = requests.get(key, [])
        local = thread_work(events, [usage[i].timestamp.timestamp() for i in indices])
        out.append(
            (
                key,
                ThreadWork(
                    runs=[(indices[i], run) for i, run in local.runs],
                    pieces=[(indices[i], s, e, label) for i, s, e, label in local.pieces],
                    spawns=[indices[i] for i in local.spawns],
                    unplaced_runs=local.unplaced_runs,
                ),
            )
        )
    return out


# -- the summary of a group --------------------------------------------------------------------------


@dataclass(slots=True)
class GroupWork:
    """What one group (session, thread, UTC day, issue segment) collected: runs, labelled pieces, spawns."""

    runs: list[ToolRun] = field(default_factory=list)
    pieces: list[tuple[float, float, str | None]] = field(default_factory=list)
    spawns: int = 0


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    """The ``counts`` level summary of one group."""

    #: (category, runs, whole seconds), in the order of :data:`CATEGORIES`; categories without runs left out.
    categories: tuple[tuple[str, int, int], ...]
    files_changed: int
    lines_added: int
    lines_removed: int
    subagents_spawned: int
    test_runs: int
    test_failed: int
    #: (start, end, category) in whole POSIX seconds.
    timeline: tuple[tuple[int, int, str], ...]


def summarise(group: GroupWork, window: tuple[float, float]) -> ActivitySummary:
    """The summary of *group*; its timeline is clipped to *window* (POSIX seconds)."""
    counts: Counter[str] = Counter()
    seconds: defaultdict[str, float] = defaultdict(float)
    files: set[str] = set()
    added = removed = tests = failed_tests = spawned = 0
    for run in group.runs:
        counts[run.category] += 1
        seconds[run.category] += run.seconds
        added += run.lines_added
        removed += run.lines_removed
        if run.category == "edit" and not run.failed:
            files.update(run.files)
        if run.category == "test":
            tests += 1
            failed_tests += int(run.failed)
        spawned += int(run.spawns)
    return ActivitySummary(
        categories=tuple((name, counts[name], round_seconds(seconds[name])) for name in CATEGORIES if counts[name]),
        files_changed=len(files),
        lines_added=added,
        lines_removed=removed,
        subagents_spawned=spawned + group.spawns,
        test_runs=tests,
        test_failed=failed_tests,
        timeline=tuple(build_timeline(group.pieces, window)),
    )


@dataclass(slots=True)
class _Node:
    start: float
    end: float
    label: str
    prev: _Node | None = None
    next: _Node | None = None
    alive: bool = True

    @property
    def length(self) -> float:
        return self.end - self.start


def _fill(pieces: Sequence[tuple[float, float, str | None]]) -> list[list[tuple[float, float, str]]]:
    """Touching chains of *pieces* (sorted), unlabelled time taking the label before it, else after, else other."""
    chains: list[list[tuple[float, float, str | None]]] = []
    for start, end, label in sorted((p for p in pieces if p[1] > p[0]), key=lambda p: (p[0], p[1])):
        if chains and start <= chains[-1][-1][1]:
            start = max(start, chains[-1][-1][1])
            if end > start:
                chains[-1].append((start, end, label))
        else:
            chains.append([(start, end, label)])
    out: list[list[tuple[float, float, str]]] = []
    for chain in chains:
        labels = [label for _, _, label in chain]
        last: str | None = None
        for i, label in enumerate(labels):
            if label is None:
                labels[i] = last
            else:
                last = label
        following: str | None = None
        for i in range(len(labels) - 1, -1, -1):
            if labels[i] is None:
                labels[i] = following
            else:
                following = labels[i]
        out.append([(s, e, labels[i] or "other") for i, (s, e, _) in enumerate(chain)])
    return out


def _fold_short(chain: Sequence[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """One touching chain with equal neighbours merged and segments under MIN_SEGMENT folded into a neighbour."""
    nodes: list[_Node] = []
    for start, end, label in chain:
        if nodes and nodes[-1].label == label:
            nodes[-1].end = end
            continue
        node = _Node(start, end, label, prev=nodes[-1] if nodes else None)
        if nodes:
            nodes[-1].next = node
        nodes.append(node)
    order = itertools.count()
    heap = [(node.length, next(order), node) for node in nodes]
    heapq.heapify(heap)
    while heap:
        length, _, node = heapq.heappop(heap)
        if not node.alive or length != node.length:
            continue
        if length >= MIN_SEGMENT:
            break
        neighbours = [n for n in (node.prev, node.next) if n is not None]
        if not neighbours:
            continue
        target = max(neighbours, key=lambda n: n.length)
        # The short node joins its longer neighbour, and anything with the same label beyond it.
        target.start, target.end = min(target.start, node.start), max(target.end, node.end)
        node.alive = False
        if target is node.next:
            target.prev = node.prev
            if node.prev is not None:
                node.prev.next = target
        else:
            target.next = node.next
            if node.next is not None:
                node.next.prev = target
        for other in (target.prev, target.next):
            if other is not None and other.label == target.label:
                target.start, target.end = min(target.start, other.start), max(target.end, other.end)
                other.alive = False
                if other is target.prev:
                    target.prev = other.prev
                    if other.prev is not None:
                        other.prev.next = target
                else:
                    target.next = other.next
                    if other.next is not None:
                        other.next.prev = target
        heapq.heappush(heap, (target.length, next(order), target))
    return [(n.start, n.end, n.label) for n in nodes if n.alive]


def build_timeline(
    pieces: Sequence[tuple[float, float, str | None]], window: tuple[float, float], limit: int = MAX_TIMELINE
) -> list[tuple[int, int, str]]:
    """The timeline of a group's labelled active pieces (see the module docs), in whole seconds inside *window*."""
    low, high = window
    segments: list[tuple[float, float, str]] = []
    for chain in _fill(pieces):
        segments.extend(_fold_short(chain))
    whole: list[tuple[int, int, str]] = []
    for start, end, label in segments:
        first = max(round_seconds(start), math.ceil(low))
        last = min(round_seconds(end), math.floor(high))
        if last <= first:
            continue
        if whole and first < whole[-1][1]:
            first = whole[-1][1]
            if last <= first:
                continue
        if whole and whole[-1][1] == first and whole[-1][2] == label:
            whole[-1] = (whole[-1][0], last, label)
        else:
            whole.append((first, last, label))
    return limit_timeline(whole, limit)


def limit_timeline(segments: Sequence[tuple[int, int, str]], limit: int = MAX_TIMELINE) -> list[tuple[int, int, str]]:
    """At most *limit* segments: the closest neighbours are joined (the longer one's label wins)."""
    segments = list(segments)
    if len(segments) <= limit:
        return segments
    gaps = sorted(range(len(segments) - 1), key=lambda i: (segments[i + 1][0] - segments[i][1], i))
    joined = set(gaps[: len(segments) - limit])
    out: list[tuple[int, int, str]] = []
    group: list[tuple[int, int, str]] = []
    for index, segment in enumerate(segments):
        group.append(segment)
        if index not in joined:
            longest = max(group, key=lambda s: s[1] - s[0])
            out.append((group[0][0], group[-1][1], longest[2]))
            group = []
    return out
