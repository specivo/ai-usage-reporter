"""Both built-in adapters pass the conformance kit."""

from __future__ import annotations

from pathlib import Path

from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
from ai_usage_reporter.adapters.codex import CodexAdapter
from ai_usage_reporter.testing import AdapterConformance

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
