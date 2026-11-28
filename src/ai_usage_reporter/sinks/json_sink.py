"""The ``json`` sink: one batch envelope, written to stdout or a file.

Used by ``push --dry-run`` and in tests. Its output is exactly what the
``specivo`` sink would send (in one envelope, without the 500-record split).
Work activity records go in an envelope of their own: after the usage one on the
stream, or, with an output file ``usage.json``, in ``usage.activity.json``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from ai_usage_reporter.activity_record import ACTIVITY_SCHEMA
from ai_usage_reporter.sinks import WRITTEN, Outcome, make_batch


class JsonSink:
    name = "json"
    records_state = False

    def __init__(self, output: Path | None = None, stream: TextIO | None = None) -> None:
        self.output = output
        self.stream = stream

    def send(self, records: Sequence[dict[str, Any]]) -> list[Outcome]:
        self._write(make_batch(records), self.output)
        return [Outcome(idempotency_key=r["idempotency_key"], status=WRITTEN) for r in records]

    def send_activity(self, records: Sequence[dict[str, Any]]) -> list[Outcome]:
        target = (
            self.output.with_name(f"{self.output.stem}.activity{self.output.suffix or '.json'}")
            if self.output
            else None
        )
        self._write(make_batch(records, ACTIVITY_SCHEMA), target)
        return [Outcome(idempotency_key=r["idempotency_key"], status=WRITTEN) for r in records]

    def _write(self, batch: dict[str, Any], target: Path | None) -> None:
        text = json.dumps(batch, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        else:
            (self.stream or sys.stdout).write(text)
