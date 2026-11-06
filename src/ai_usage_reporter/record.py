"""The contract record and the only serializer that produces wire data.

**Privacy is enforced here.** :class:`ContractRecord` has exactly the fields
of a ``specivo.ai-usage.v1`` record and nothing else: ids, counts, the model,
the tool, timestamps, the branch, a repository hint (``host/owner/repo``,
never a path), commit SHAs, cost hints and the ``on_behalf_of`` login. The
internal :class:`~ai_usage_reporter.model.UsageEvent` (which knows the
working directory and the source file) cannot be serialised at all.

:meth:`ContractRecord.to_wire` builds the JSON object from an explicit
allowlist, field by field, and checks every value against the contract's
format. An optional value that does not fit is left out rather than sent; a
required value that does not fit raises :class:`RecordError`. Every sink and
every command that prints a payload goes through :func:`to_wire`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ai_usage_reporter.model import TOKEN_CLASSES, TokenCounts

COST_TYPES = ("subscription", "api", "local", "free", "unknown")
MAX_COUNT = 10**12
MAX_REQUESTS = 100_000_000

_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")
_TOOL = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")
_ISSUE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}-[1-9][0-9]{0,8}$")
_PROJECT = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
_AMOUNT = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,8})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
# A session id is sent as is only when it looks like an opaque token.
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# host/owner/repo: no scheme, no credentials, no local path.
_REPO = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~-]+){2,}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RecordError(ValueError):
    """A required field cannot be expressed in the contract."""


@dataclass(frozen=True, slots=True)
class Money:
    amount: str
    currency: str


@dataclass(frozen=True, slots=True)
class Attribution:
    issue_ref: str | None = None
    issue_ref_source: str | None = None
    project_key: str | None = None
    git_branch: str | None = None
    repo: str | None = None
    commits: tuple[str, ...] = ()

    def without_explicit(self) -> Attribution:
        """The inferred hints only (after an ``attribution_conflict``)."""
        return replace(self, issue_ref=None, issue_ref_source=None, project_key=None)


@dataclass(frozen=True, slots=True)
class ContractRecord:
    idempotency_key: str
    tool: str
    model: str
    usage_date: date
    tokens: TokenCounts
    tool_version: str | None = None
    provider: str | None = None
    session_id: str | None = None
    timezone: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    request_count: int = 0
    long_context_tokens: TokenCounts | None = None
    reported_cost: Money | None = None
    cost_type: str | None = None
    subscription_hint: str | None = None
    on_behalf_of: str | None = None
    attribution: Attribution | None = None

    def to_wire(self) -> dict[str, Any]:
        return to_wire(self)

    def payload_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.to_wire()).encode()).hexdigest()


def format_amount(value: Decimal) -> str:
    """A non-negative amount in the contract's form: at most 8 decimals, no exponent, no trailing zeros."""
    rounded = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = f"{rounded:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def idempotency_key(tool: str, session_id: str, model: str, usage_date: date) -> str:
    """The contract's recommended key: SHA-256 of ``v1|tool|session|model|date``."""
    text = f"v1|{tool}|{session_id}|{model}|{usage_date.isoformat()}"
    return hashlib.sha256(text.encode()).hexdigest()


def opaque_session_id(raw: str) -> str:
    """The session id to send: as is when it is an opaque token, else a hash of it."""
    if _OPAQUE.match(raw):
        return raw
    return "h-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def normalise_repo(url: str | None) -> str | None:
    """``host/owner/repo`` from a git remote URL, or ``None`` for anything else.

    Credentials, ports' schemes and ``.git`` are removed. Local remotes (a path
    or ``file://``) give ``None``: a path never leaves the machine.
    """
    if not url:
        return None
    text = url.strip()
    if not text or text.startswith(("/", "~", ".", "file:")) or "\\" in text or re.match(r"^[A-Za-z]:", text):
        return None
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(.*)$", text)
    if match:
        rest = match.group(1)
        rest = rest.rsplit("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0]
    elif re.match(r"^[^/@\s]+@[^/:\s]+:", text):  # scp-like git@host:owner/repo
        host_part, _, path = text.partition(":")
        host = host_part.rsplit("@", 1)[-1]
    else:
        return None
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    candidate = f"{host.lower()}/{path}"
    if len(candidate) > 255 or not _REPO.match(candidate):
        return None
    return candidate


def _instant(value: datetime) -> str:
    value = value.astimezone(UTC)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += f".{value.microsecond:06d}".rstrip("0")
    return text + "Z"


def _text(value: str | None, max_length: int, *, min_length: int = 1) -> str | None:
    if value is None:
        return None
    if _CONTROL.search(value) or not min_length <= len(value) <= max_length:
        return None
    return value


def _tokens(value: TokenCounts) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in TOKEN_CLASSES:
        number = getattr(value, name)
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= MAX_COUNT:
            raise RecordError(f"token count {name} out of range")
        if number:
            out[name] = number
    return out


def _attribution(value: Attribution | None) -> dict[str, Any] | None:
    if value is None:
        return None
    out: dict[str, Any] = {}
    if value.issue_ref and _ISSUE.match(value.issue_ref):
        out["issue_ref"] = value.issue_ref
        if value.issue_ref_source in ("explicit", "env"):
            out["issue_ref_source"] = value.issue_ref_source
    if value.project_key and _PROJECT.match(value.project_key):
        out["project_key"] = value.project_key
    branch = _text(value.git_branch, 255)
    if branch:
        out["git_branch"] = branch
    repo = normalise_repo(value.repo) if value.repo and not _REPO.match(value.repo) else value.repo
    if repo and _REPO.match(repo):
        out["repo"] = repo
    commits = sorted({c.lower() for c in value.commits if _SHA.match(c)})[:100]
    if commits:
        out["commits"] = commits
    return out or None


def to_wire(record: ContractRecord) -> dict[str, Any]:
    """The JSON object of one record, built from the allowlist below and nothing else."""
    if not _KEY.match(record.idempotency_key):
        raise RecordError("idempotency_key is not printable ASCII of 1..128 characters")
    if not _TOOL.match(record.tool):
        raise RecordError("tool is not a valid slug")
    model = _text(record.model, 200)
    if model is None:
        raise RecordError("model is empty or too long")
    wire: dict[str, Any] = {
        "idempotency_key": record.idempotency_key,
        "tool": record.tool,
        "model": model,
        "usage_date": record.usage_date.isoformat(),
        "tokens": _tokens(record.tokens),
    }
    optional: dict[str, Any] = {
        "tool_version": _text(record.tool_version, 50),
        "provider": _text(record.provider, 50),
        "session_id": opaque_session_id(record.session_id) if record.session_id else None,
        "timezone": _text(record.timezone, 64),
        "started_at": _instant(record.started_at) if record.started_at else None,
        "ended_at": _instant(record.ended_at) if record.ended_at else None,
        "cost_type": record.cost_type if record.cost_type in COST_TYPES else None,
        "subscription_hint": _text(record.subscription_hint, 100),
        "on_behalf_of": _text(record.on_behalf_of, 255),
    }
    for name, value in optional.items():
        if value is not None:
            wire[name] = value
    if record.request_count:
        if not 0 <= record.request_count <= MAX_REQUESTS:
            raise RecordError("request_count out of range")
        wire["request_count"] = record.request_count
    if record.long_context_tokens is not None and not record.long_context_tokens.is_zero():
        wire["long_context_tokens"] = _tokens(record.long_context_tokens)
        # The buckets are disjoint and the usage is their sum, which has the same bound.
        if any(v > MAX_COUNT for v in (record.tokens + record.long_context_tokens).as_dict().values()):
            raise RecordError("token count out of range in tokens + long_context_tokens")
    cost = record.reported_cost
    if cost is not None and _AMOUNT.match(cost.amount) and _CURRENCY.match(cost.currency):
        wire["reported_cost"] = {"amount": cost.amount, "currency": cost.currency}
    attribution = _attribution(record.attribution)
    if attribution:
        wire["attribution"] = attribution
    return wire


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
