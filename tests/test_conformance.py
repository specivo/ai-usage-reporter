"""Every built-in adapter passes the conformance kit."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.adapters.gemini_cli import GeminiCliAdapter
from ai_usage_reporter.adapters.hermes import HermesAdapter
from ai_usage_reporter.adapters.opencode import OpenCodeAdapter
from ai_usage_reporter.testing import AdapterConformance
from tests.fixtures import active_time_home, agent_log_home, codex_time_home, hermes_home
from tests.fixtures.opencode_home import build

FIXTURES = Path(__file__).parent / "fixtures"
CANARIES = ("CANARY-PROMPT-5c1e", "CANARY-TOOL-OUTPUT-9d07", "CANARY-PATH-7f3a")


class TestClaudeCodeConformance(AdapterConformance):
    adapter = ClaudeCodeAdapter()
    fixture_home = FIXTURES / "claude_code" / "home"
    golden = FIXTURES / "claude_code" / "golden.json"
    canaries = CANARIES


class TestCodexConformance(AdapterConformance):
    adapter = CodexAdapter()
    fixture_home = FIXTURES / "codex" / "home"
    golden = FIXTURES / "codex" / "golden.json"
    canaries = (*CANARIES, "CANARY-SECRET-b2e4")


class TestGeminiCliConformance(AdapterConformance):
    adapter = GeminiCliAdapter()
    fixture_home = FIXTURES / "gemini_cli" / "home"
    golden = FIXTURES / "gemini_cli" / "golden.json"
    canaries = CANARIES


def _opencode_home() -> Path:
    """The OpenCode fixtures are built once per test run (see tests/fixtures/opencode_home.py)."""
    folder = Path(tempfile.mkdtemp(prefix="opencode-fixture-"))
    atexit.register(shutil.rmtree, folder, True)
    build(folder / "home")
    return folder / "home"


class TestOpenCodeConformance(AdapterConformance):
    adapter = OpenCodeAdapter()
    fixture_home = _opencode_home()
    golden = FIXTURES / "opencode" / "golden.json"
    canaries = CANARIES


def _agent_log_home() -> Path:
    """Claude Code transcripts in which the agent marks issues (see tests/fixtures/agent_log_home.py)."""
    folder = Path(tempfile.mkdtemp(prefix="agent-log-fixture-"))
    atexit.register(shutil.rmtree, folder, True)
    return agent_log_home.build(folder / "home")


class TestClaudeCodeIssueSegmentsConformance(AdapterConformance):
    adapter = ClaudeCodeAdapter()
    fixture_home = _agent_log_home()
    golden = FIXTURES / "claude_code" / "golden-segments.json"
    canaries = CANARIES


def _active_time_home() -> Path:
    """A Claude Code session with two subagents, for active time (see tests/fixtures/active_time_home.py)."""
    folder = Path(tempfile.mkdtemp(prefix="active-time-fixture-"))
    atexit.register(shutil.rmtree, folder, True)
    return active_time_home.build(folder / "home")


class TestClaudeCodeActiveTimeConformance(AdapterConformance):
    adapter = ClaudeCodeAdapter()
    fixture_home = _active_time_home()
    golden = FIXTURES / "claude_code" / "golden-active-time.json"
    canaries = CANARIES


def _codex_time_home() -> Path:
    """Codex rollouts of a session with two subagents and an older rollout (tests/fixtures/codex_time_home.py)."""
    folder = Path(tempfile.mkdtemp(prefix="codex-time-fixture-"))
    atexit.register(shutil.rmtree, folder, True)
    return codex_time_home.build(folder / "home")


class TestCodexActiveTimeConformance(AdapterConformance):
    adapter = CodexAdapter()
    fixture_home = _codex_time_home()
    golden = FIXTURES / "codex" / "golden-active-time.json"
    canaries = (*CANARIES, codex_time_home.CANARY_COMMAND, codex_time_home.CANARY_MESSAGE)


def _hermes_home() -> Path:
    """A Hermes home with two profiles, built once per test run (see tests/fixtures/hermes_home.py)."""
    folder = Path(tempfile.mkdtemp(prefix="hermes-fixture-"))
    atexit.register(shutil.rmtree, folder, True)
    hermes_home.build(folder / "home")
    return folder / "home"


class TestHermesConformance(AdapterConformance):
    adapter = HermesAdapter()
    fixture_home = _hermes_home()
    golden = FIXTURES / "hermes" / "golden.json"
    canaries = CANARIES
