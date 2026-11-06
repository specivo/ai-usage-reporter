"""The conformance kit every adapter passes.

Subclass :class:`AdapterConformance` in a test module (the subclass name must
start with ``Test`` so pytest collects it) and point it at a fake home
directory holding the tool's logs::

    class TestMyToolAdapter(AdapterConformance):
        adapter = MyToolAdapter()
        fixture_home = Path(__file__).parent / "fixtures" / "mytool" / "home"
        golden = Path(__file__).parent / "fixtures" / "mytool" / "golden.json"
        canaries = ("CANARY-PROMPT-1", "CANARY-TOOL-OUTPUT-1", "CANARY-PATH-1")

The inherited tests check that:

- the fixtures produce the golden records (set ``AI_USAGE_UPDATE_GOLDEN=1``
  to write them);
- every record is valid against the vendored contract schema (needs the
  ``jsonschema`` package, a test dependency only);
- parsing again with the state database gives the same records and sends
  nothing new;
- copies of every log file change nothing (cross-file deduplication);
- the canary strings planted in prompts, tool output and paths never reach
  any sink, the state ledger or the console.

Nothing here needs pytest; the tests are plain ``assert`` methods.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any, ClassVar

from ai_usage_reporter.adapters import Adapter, DiscoveryContext, Registry
from ai_usage_reporter.config import Config
from ai_usage_reporter.model import UsageEvent
from ai_usage_reporter.pipeline import Collected, collect, deliver
from ai_usage_reporter.sinks import make_batch
from ai_usage_reporter.sinks.json_sink import JsonSink
from ai_usage_reporter.sinks.specivo import HttpResponse, SpecivoSink
from ai_usage_reporter.state import State

UPDATE_ENV = "AI_USAGE_UPDATE_GOLDEN"


def contract_file(name: str) -> dict[str, Any]:
    """A file of the vendored contract, e.g. ``record.schema.json``."""
    data: dict[str, Any] = json.loads(files("ai_usage_reporter.contract").joinpath("v1", name).read_text("utf-8"))
    return data


def schema_validator(name: str = "batch.schema.json") -> Any:
    """A ``jsonschema`` validator for a contract schema, with the cross-file refs resolved."""
    try:
        import jsonschema
        from referencing import Registry as RefRegistry
        from referencing import Resource
    except ImportError as exc:  # pragma: no cover - only without the test dependency
        raise RuntimeError("schema validation needs the jsonschema package (a test dependency)") from exc
    resources = []
    for schema_name in ("batch.schema.json", "record.schema.json", "result.schema.json", "info.schema.json"):
        schema = contract_file(schema_name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    registry = RefRegistry().with_resources(resources)
    schema = contract_file(name)
    validator_class = jsonschema.validators.validator_for(schema)
    return validator_class(schema, registry=registry, format_checker=validator_class.FORMAT_CHECKER)


def validate_records(records: Sequence[dict[str, Any]]) -> None:
    """Raise if any record (or the batch around up to 500 of them) breaks the contract schema."""
    record_validator = schema_validator("record.schema.json")
    for record in records:
        record_validator.validate(record)
    if records:
        schema_validator("batch.schema.json").validate(make_batch(list(records)[:500]))


def fixed_now(events: Sequence[UsageEvent]) -> datetime:
    latest = max((e.timestamp for e in events), default=datetime(2026, 1, 1, tzinfo=UTC))
    return latest + timedelta(days=1)


class _Fake:
    """A Specivo server that accepts everything and keeps the request bodies."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        if method == "GET":
            info = {"versions": ["specivo.ai-usage.v1"], "max_records": 500, "max_bytes": 1048576,
                    "server_time": "2026-01-01T00:00:00Z", "tools": []}  # fmt: skip
            return HttpResponse(200, {}, json.dumps(info).encode())
        assert body is not None
        self.bodies.append(body)
        records = json.loads(body)["records"]
        results = [
            {"index": i, "idempotency_key": r["idempotency_key"], "status": "created", "entry_id": i + 1,
             "revision": 1, "attribution": {"method": "none"}, "warnings": [], "error": None}
            for i, r in enumerate(records)
        ]  # fmt: skip
        summary = {"received": len(records), "created": len(records), "updated": 0, "unchanged": 0, "rejected": 0}
        answer = {"schema": "specivo.ai-usage.v1", "server_time": "2026-01-01T00:00:00Z", "summary": summary,
                  "results": results, "warnings": []}  # fmt: skip
        return HttpResponse(200, {}, json.dumps(answer).encode())


class AdapterConformance:
    adapter: ClassVar[Adapter]
    fixture_home: ClassVar[Path]
    golden: ClassVar[Path]
    #: Strings planted in prompts, tool output and paths of the fixtures.
    canaries: ClassVar[tuple[str, ...]] = ()
    #: Environment for discovery; ``{home}`` in a value is the fake home directory.
    env: ClassVar[Mapping[str, str]] = {}
    config: ClassVar[Config] = Config()

    # -- helpers -------------------------------------------------------------------

    def _context(self, home: Path) -> DiscoveryContext:
        env = {k: v.replace("{home}", str(home)) for k, v in self.env.items()}
        return DiscoveryContext(home=home, env=env)

    def collect(self, home: Path, state: State | None = None) -> Collected:
        registry = Registry(adapters={self.adapter.name: self.adapter})
        events: list[UsageEvent] = []
        for src in self.adapter.discover(self._context(home)):
            events.extend(self.adapter.parse(src))
        return collect(
            self.config,
            registry,
            state=state,
            context_for=lambda adapter, roots: DiscoveryContext(home=home, env=self._context(home).env, roots=roots),
            git=None,
            now=fixed_now(events),
        )

    def records(self, home: Path, state: State | None = None) -> list[dict[str, Any]]:
        return [r.to_wire() for r in self.collect(home, state).records]

    def copy_home(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name) / "home"
        shutil.copytree(self.fixture_home, home)
        return tmp, home

    def duplicate(self, home: Path) -> None:
        """Copy every discovered log file into a ``copy`` directory beside it."""
        for src in list(self.adapter.discover(self._context(home))):
            target = src.path.parent / "copy" / src.path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src.path, target)

    # -- the tests -----------------------------------------------------------------

    def test_golden(self) -> None:
        tmp, home = self.copy_home()
        with tmp:
            records = self.records(home)
        assert records, "the fixtures produce no records"
        text = json.dumps(records, indent=2, sort_keys=True) + "\n"
        if os.environ.get(UPDATE_ENV):
            self.golden.write_text(text, encoding="utf-8")
        assert self.golden.exists(), f"no golden file; run with {UPDATE_ENV}=1 to write it"
        assert json.loads(self.golden.read_text("utf-8")) == json.loads(text)

    def test_records_match_contract_schema(self) -> None:
        tmp, home = self.copy_home()
        with tmp:
            validate_records(self.records(home))

    def test_idempotent_reparse(self) -> None:
        tmp, home = self.copy_home()
        with tmp, State.in_memory() as state:
            first = self.collect(home, state)
            fake = _Fake()
            sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=fake, sleep=lambda s: None)
            sent = deliver(first.records, sink, state)
            assert sent.sent == len(first.records)
            second = self.collect(home, state)
            assert [r.to_wire() for r in second.records] == [r.to_wire() for r in first.records]
            assert all(report.parsed == 0 for report in second.reports.values()), "unchanged files were parsed again"
            again = deliver(second.records, sink, state)
            assert again.sent == 0 and again.skipped_unchanged == len(first.records)

    def test_duplicate_files_change_nothing(self) -> None:
        tmp, home = self.copy_home()
        with tmp:
            before = self.records(home)
            self.duplicate(home)
            after = self.records(home)
        assert after == before

    def test_canaries_never_leave(self) -> None:
        assert self.canaries, "set canaries: strings planted in prompts, tool output and paths"
        tmp, home = self.copy_home()
        with tmp:
            # The canaries must really be in the fixtures, or this test proves nothing.
            raw = "".join(p.read_text("utf-8", errors="replace") for p in home.rglob("*") if p.is_file())
            raw += "".join(str(p) for p in home.rglob("*"))
            for canary in self.canaries:
                assert canary in raw, f"canary {canary!r} is not planted in the fixtures"
            console = io.StringIO()
            json_out = io.StringIO()
            fake = _Fake()
            with State.in_memory() as state, contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                collected = self.collect(home, state)
                wires = [r.to_wire() for r in collected.records]
                JsonSink(stream=json_out).send(wires)
                sink = SpecivoSink("https://specivo.invalid", "spv_test", transport=fake, sleep=lambda s: None)
                deliver(collected.records, sink, state)
                ledger = "".join(str(tuple(row)) for row in state.db.execute("SELECT * FROM buckets"))
                reprs = "".join(repr(r) for r in collected.records)
            outputs = {
                "json sink": json_out.getvalue(),
                "http bodies": b"".join(fake.bodies).decode("utf-8", errors="replace"),
                "console": console.getvalue(),
                "state ledger": ledger,
                "records": reprs,
            }
            for canary in self.canaries:
                for where, text in outputs.items():
                    assert canary not in text, f"canary {canary!r} leaked into the {where}"
