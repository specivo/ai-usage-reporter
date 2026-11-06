"""Read the local logs of AI coding tools and report their token usage to Specivo.

The reporter speaks the Specivo AI usage ingest contract ``specivo.ai-usage.v1``.
Only counts, ids, model names, timestamps and a few attribution hints ever leave
the machine; see :mod:`ai_usage_reporter.record` for the allowlist.
"""

from __future__ import annotations

__all__ = ["CLIENT_NAME", "CONTRACT_VERSION", "__version__"]

__version__ = "0.2.0"

#: Sent as ``client.name`` in every batch.
CLIENT_NAME = "ai-usage-reporter"

#: The contract version this reporter speaks.
CONTRACT_VERSION = "specivo.ai-usage.v1"
