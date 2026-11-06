"""Line-by-line JSON reading shared by the adapters, with optional zstd support."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from ai_usage_reporter.model import ParseStats


def zstd_available() -> bool:
    return _zstd_opener() is not None


def _zstd_opener() -> Any:
    try:  # Python 3.14+
        from compression import zstd

        return zstd
    except ImportError:
        pass
    try:  # the optional "zstd" extra
        import zstandard

        return zstandard
    except ImportError:
        return None


def _open_binary(path: Path) -> IO[bytes]:
    if path.name.endswith(".zst"):
        module = _zstd_opener()
        if module is None:
            raise OSError("zstd support is not installed")
        if getattr(module, "__name__", "") == "zstandard":
            raw = path.open("rb")
            reader: IO[bytes] = module.ZstdDecompressor().stream_reader(raw, closefd=True)
            return reader
        opened: IO[bytes] = module.open(path, "rb")
        return opened
    return path.open("rb")


def iter_json_lines(path: Path, stats: ParseStats) -> Iterator[dict[str, Any]]:
    """Yield each line of a JSONL file that is a JSON object.

    Blank lines are skipped; any other line that is not a JSON object is counted
    as malformed (``ai-usage doctor`` shows the count) and skipped. A file that
    cannot be opened yields nothing and counts as one malformed line.
    """
    try:
        handle = _open_binary(path)
    except OSError:
        stats.malformed_lines += 1
        return
    with handle, io.TextIOWrapper(handle, encoding="utf-8", errors="replace") as text:
        try:
            for line in text:
                stripped = line.strip()
                if not stripped:
                    continue
                stats.lines += 1
                try:
                    value = json.loads(stripped)
                except ValueError:
                    stats.malformed_lines += 1
                    continue
                if not isinstance(value, dict):
                    stats.malformed_lines += 1
                    continue
                yield value
        except (OSError, EOFError, ValueError):
            # A truncated or corrupt compressed stream: keep what was read.
            stats.malformed_lines += 1


def get_str(mapping: object, *keys: str) -> str | None:
    """A non-empty string at a nested key path, else ``None``."""
    value: object = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def get_dict(mapping: object, *keys: str) -> dict[str, Any] | None:
    value: object = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, dict) else None
