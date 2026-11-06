"""Long-context thresholds per model.

The reporter cannot see the server's price table, so it learns which models
have a long-context tier from the ``[long_context]`` table of the config file:
a model name or a shell-style pattern mapped to the threshold in tokens. A
request whose context (input plus cache reads and writes) is above the
threshold is reported in ``long_context_tokens``. No thresholds are built in:
without configuration every request is reported at base prices, which is
exact for every model without a long-context tier.
"""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase


class Thresholds:
    def __init__(self, table: Mapping[str, int] | None = None) -> None:
        self._exact: dict[str, int] = {}
        self._patterns: list[tuple[str, int]] = []
        for pattern, value in (table or {}).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"long_context threshold for {pattern!r} must be a positive integer")
            key = pattern.lower()
            if any(ch in key for ch in "*?["):
                self._patterns.append((key, value))
            else:
                self._exact[key] = value

    def __call__(self, model: str) -> int | None:
        name = model.lower()
        if name in self._exact:
            return self._exact[name]
        for pattern, value in self._patterns:
            if fnmatchcase(name, pattern):
                return value
        return None
