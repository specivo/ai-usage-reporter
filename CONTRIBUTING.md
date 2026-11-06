# Contributing

Thank you for helping. Bug reports with a short, made-up log excerpt are the most useful kind:
please never attach real transcripts, they contain your prompts and code.

## Setup

```bash
uv sync
make test
make lint
```

Every change keeps `ruff check`, `ruff format --check`, `mypy --strict` and `pytest` green on
Python 3.11 to 3.13. The runtime stays standard-library only; test tools go in the `dev` group.

## Writing a new adapter

An adapter teaches the reporter one tool's log format. Everything after parsing (deduplication
across files, buckets, enrichment, privacy and sending) is shared, so an adapter is usually one
module and its fixtures. Work through this checklist.

### One tool, one adapter

- [ ] One adapter per product. A vendor's CLI and its IDE extension are two adapters, because
      their logs, versions and parser changes are independent.
- [ ] `name` is the tool slug that Specivo shows: `a-z`, `0-9` and `-`, 2 to 50 characters
      (`gemini-cli`, not `Gemini`).
- [ ] `provider` is the default model provider (`anthropic`, `openai`, `google`, ...).

### The interface

```python
class Adapter(Protocol):
    name: str
    provider: str
    parser_version: str

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]: ...
    def parse(self, src: SourceFile) -> Iterator[UsageEvent]: ...
    def dedup_key(self, event: UsageEvent) -> str | None: ...
    # optional
    def hook(self) -> HookSpec: ...
```

- [ ] `discover` uses `ctx.home`, `ctx.env` and `ctx.roots` only, never `Path.home()` or
      `os.environ`, so tests can point it at a fake home. Honour the tool's own environment
      variable for its data directory, and `ctx.roots` (the `[sources.<name>] roots` setting).
- [ ] When one session can exist in several files (archives, compressed copies), `discover`
      yields the one copy to read.
- [ ] `parse` yields one `UsageEvent` per model request with an aware UTC timestamp.
- [ ] Tokens are in the contract's semantics: `input` excludes cache reads and writes, `output`
      excludes reasoning when the tool reports reasoning apart. Subtract, so that each token is
      in exactly one class.
- [ ] `parse` never raises on bad data. Count unreadable lines in `src.stats.malformed_lines`
      and lines with usage that cannot be used (no timestamp) in `src.stats.skipped_events`;
      `ai-usage doctor` shows both.
- [ ] Unknown models are reported as `unknown`, never guessed.
- [ ] `dedup_key` names the request across all files of the tool, so a copied or resumed log
      counts once. Return `None` when the tool gives nothing to deduplicate on.
- [ ] Repeated records of one request (streaming updates, replays) set `is_replay` and
      `replay_key` when the format marks them; the shared deduplication drops them.
- [ ] Subagent or child sessions are reported under the parent session's id when the format
      links them.
- [ ] Bump `parser_version` whenever parsing changes; the reporter then parses every file of the
      adapter again.

### Privacy

- [ ] `UsageEvent.cwd`, `source_path`, `git_branch` and `repo_url` are the only context an adapter
      passes on. Never put prompt text, tool input or output, or file contents anywhere in an
      event, not even in an id.
- [ ] Session ids should be the tool's opaque ids. Anything else is hashed before sending, but do
      not rely on that.

### Registration

Built-in adapters are listed in `ai_usage_reporter.adapters.builtin_adapters`. A third-party
package registers its adapter class (or instance) as an entry point:

```toml
[project.entry-points."ai_usage_reporter.adapters"]
mytool = "mytool_usage.adapter:MyToolAdapter"
```

A plugin cannot take the name of a built-in adapter.

### Tests

- [ ] Write synthetic fixtures by hand, or with a small generator script like
      `tests/fixtures/make_fixtures.py`. Invent every id, path, prompt and number; never copy
      a real log.
- [ ] Plant canary strings in prompts, tool output, paths and anything else secret-looking.
- [ ] Cover discovery (default directory, the environment variable, archives), every token
      field, a malformed line, a repeated request, and a session crossing midnight UTC.
- [ ] Subclass the conformance kit; it checks the golden records, the contract schema,
      idempotent re-parsing, duplicated files and the canaries:

```python
from ai_usage_reporter.testing import AdapterConformance

class TestMyToolConformance(AdapterConformance):
    adapter = MyToolAdapter()
    fixture_home = FIXTURES / "mytool" / "home"
    golden = FIXTURES / "mytool" / "golden.json"
    canaries = ("CANARY-PROMPT-1", "CANARY-TOOL-OUTPUT-1", "CANARY-PATH-1")
```

Write the golden file once with `AI_USAGE_UPDATE_GOLDEN=1 uv run pytest`, then read it line by
line: every number in it should be one you can explain from the fixture.

### Documentation

- [ ] Describe the log layout and every rule the adapter applies in the module docstring.
- [ ] Add the tool to the "What it reads" table in `docs/index.md`.
- [ ] Before opening a pull request, run `ai-usage push --dry-run` against real logs of the tool
      on your own machine and check the counts. Do not paste its output anywhere: it contains
      your session ids and branch names.

## Sinks

A sink receives wire records (already filtered by the privacy serializer) and returns one outcome
per record. Third-party sinks register under `ai_usage_reporter.sinks` with a callable that takes
a `SinkContext`. They are experimental.

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, ...). No generated co-author trailers.
