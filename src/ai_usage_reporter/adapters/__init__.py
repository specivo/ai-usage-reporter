"""The adapter interface and the adapter registry.

An adapter knows one AI tool's local logs: where they are (:meth:`Adapter.discover`)
and how to read usage from them (:meth:`Adapter.parse`). Everything after that,
deduplication across files, buckets, enrichment, privacy and sending, is shared.

Built-in adapters are registered here. Third-party adapters are found through
the entry-point group ``ai_usage_reporter.adapters``; an entry point names a
class (instantiated without arguments) or an adapter instance. See
CONTRIBUTING.md for the checklist a new adapter follows.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_usage_reporter.model import SourceFile, UsageEvent

ENTRY_POINT_GROUP = "ai_usage_reporter.adapters"


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """What :meth:`Adapter.discover` may look at.

    ``home`` and ``env`` are injected so tests never touch the real home
    directory. ``roots`` overrides an adapter's default data directories (from
    the ``[sources.<name>] roots`` setting).
    """

    home: Path
    env: Mapping[str, str]
    roots: tuple[Path, ...] = ()

    @classmethod
    def from_environment(cls, roots: Iterable[Path] = ()) -> DiscoveryContext:
        return cls(home=Path.home(), env=dict(os.environ), roots=tuple(roots))

    def expand(self, raw: str) -> Path:
        """``~`` expanded against :attr:`home`, not the real home."""
        if raw == "~" or raw.startswith(("~/", "~\\")):
            return self.home / raw[2:] if len(raw) > 1 else self.home
        return Path(raw)


@dataclass(frozen=True, slots=True)
class HookSpec:
    """How a tool can call the reporter after a session (``ai-usage hooks install``).

    ``kind`` names the mechanism (``claude-settings-hook``, ``codex-notify``,
    ``gemini-settings-hook``, ``opencode-plugin``), ``events`` the tool events
    that are hooked.
    """

    kind: str
    events: tuple[str, ...] = ()
    notes: str = ""


@runtime_checkable
class Adapter(Protocol):
    """One AI tool's log reader.

    - ``name`` is the tool slug sent as ``tool`` (``a-z``, ``0-9``, ``-``).
    - ``provider`` is the default model provider (``anthropic``, ``openai``).
    - ``parser_version`` changes whenever parsing changes; a new value makes
      the reporter parse every file of the adapter again.
    - :meth:`parse` yields one :class:`UsageEvent` per model request, in the
      contract's token semantics (input excludes cache; output excludes
      reasoning reported apart). It must never raise on bad input: it counts
      malformed lines in ``src.stats`` and goes on.
    - :meth:`dedup_key` names the request across all files of the adapter
      (``None``: never deduplicated).
    """

    name: str
    provider: str
    parser_version: str

    def discover(self, ctx: DiscoveryContext) -> Iterable[SourceFile]: ...

    def parse(self, src: SourceFile) -> Iterator[UsageEvent]: ...

    def dedup_key(self, event: UsageEvent) -> str | None: ...


def adapter_hook(adapter: Adapter) -> HookSpec | None:
    """The adapter's optional ``hook()`` metadata."""
    hook = getattr(adapter, "hook", None)
    if callable(hook):
        result = hook()
        return result if isinstance(result, HookSpec) else None
    return None


def builtin_adapters() -> list[Adapter]:
    from ai_usage_reporter.adapters.claude_code import ClaudeCodeAdapter
    from ai_usage_reporter.adapters.codex import CodexAdapter
    from ai_usage_reporter.adapters.gemini_cli import GeminiCliAdapter
    from ai_usage_reporter.adapters.opencode import OpenCodeAdapter

    return [ClaudeCodeAdapter(), CodexAdapter(), GeminiCliAdapter(), OpenCodeAdapter()]


@dataclass(slots=True)
class Registry:
    adapters: dict[str, Adapter] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def get(self, name: str) -> Adapter | None:
        return self.adapters.get(name)


def load_adapters(*, include_plugins: bool = True) -> Registry:
    """Built-in adapters, then plugins. A plugin cannot replace a built-in name."""
    registry = Registry()
    for adapter in builtin_adapters():
        registry.adapters[adapter.name] = adapter
    if not include_plugins:
        return registry
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
            adapter = loaded() if isinstance(loaded, type) else loaded
        except Exception as exc:  # a broken plugin must not stop the reporter
            registry.errors.append(f"adapter plugin {ep.name!r} failed to load: {type(exc).__name__}")
            continue
        if not isinstance(adapter, Adapter):
            registry.errors.append(f"adapter plugin {ep.name!r} does not implement the Adapter interface")
            continue
        if adapter.name in registry.adapters:
            registry.errors.append(f"adapter plugin {ep.name!r} uses the taken name {adapter.name!r}")
            continue
        registry.adapters[adapter.name] = adapter
    return registry


def platform_is_windows() -> bool:
    return sys.platform.startswith("win")
