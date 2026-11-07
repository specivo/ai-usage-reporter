from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_usage_reporter.state import FileIdentity, State


class _BigStat:
    """A stat result shaped like the one Windows gives for some files."""

    def __init__(self, real: os.stat_result) -> None:
        self.st_size = real.st_size
        self.st_mtime_ns = real.st_mtime_ns
        self.st_ino = (1 << 64) - 5  # beyond SQLite's signed 64-bit range
        self.st_dev = (1 << 127) + 3  # a 128-bit ReFS-style value


def test_large_windows_file_ids_are_stored_and_recognised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    real_stat = Path.stat

    def fake_stat(self: Path, **kw: object) -> object:
        st = real_stat(self, **kw)  # type: ignore[arg-type]
        return _BigStat(st) if self == log else st

    monkeypatch.setattr(Path, "stat", fake_stat)

    identity = FileIdentity.of(log)
    for value in (identity.inode, identity.device):
        assert -(1 << 63) <= value < 1 << 63

    with State(tmp_path / "state.sqlite") as state:
        state.store_events(log, "claude-code", identity, "1", [], lines=1, malformed_lines=0, skipped_events=0)
        assert state.cached_events(log, FileIdentity.of(log), "1") == []
