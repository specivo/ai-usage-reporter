"""The ``json`` sink: one batch envelope, written to stdout or a file.

Used by ``push --dry-run`` and in tests. Its output is exactly what the
``specivo`` sink would send (in one envelope, without the 500-record split).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from ai_usage_reporter.sinks import WRITTEN, Outcome, make_batch


class JsonSink:
    name = "json"
    records_state = False

    def __init__(self, output: Path | None = None, stream: TextIO | None = None) -> None:
        self.output = output
        self.stream = stream

    def send(self, records: Sequence[dict[str, Any]]) -> list[Outcome]:
        text = json.dumps(make_batch(records), indent=2, sort_keys=False, ensure_ascii=False) + "\n"
        if self.output is not None:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_text(text, encoding="utf-8")
        else:
            (self.stream or sys.stdout).write(text)
        return [Outcome(idempotency_key=r["idempotency_key"], status=WRITTEN) for r in records]
