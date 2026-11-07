"""The ``specivo`` sink: ``POST /api/v1/ai-usage/ingest`` (contract ``specivo.ai-usage.v1``).

- Asks the server once (``GET``) which versions and limits it has, and splits
  records into batches under ``max_records`` and ``max_bytes``.
- Every record gets its own outcome from the response's ``results``.
- ``429``: waits ``Retry-After`` seconds (up to a cap) and sends the batch again.
- ``5xx`` and network errors: retried with backoff, then left ``pending`` for
  the next run (the retry queue).
- ``413``: the batch is split in half.
- ``401``/``403``, ``400``: the whole push stops with :class:`SinkError`.
- Redirects are never followed: following one would send the API key to
  wherever the ``Location`` points. Any ``3xx`` stops the push with an error
  that names the target, so the configured URL can be corrected.
- The URL must be ``https://``, except for this machine (see
  :func:`ai_usage_reporter.config.check_server_url`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from ai_usage_reporter import CONTRACT_VERSION, __version__
from ai_usage_reporter.config import ConfigError, check_server_url
from ai_usage_reporter.sinks import PENDING, Outcome, make_batch

DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_BYTES = 1_048_576
TIMEOUT_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 300.0
SERVER_ERROR_ATTEMPTS = 3
RATE_LIMIT_ATTEMPTS = 5


class SinkError(RuntimeError):
    """The push cannot go on (bad key, refused contract version, malformed envelope)."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx answer comes back as an HTTPError instead."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            return HttpResponse(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b"")


def endpoint(url: str) -> str:
    base = url.rstrip("/")
    for suffix in ("/api/v1/ai-usage/ingest", "/api/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + "/api/v1/ai-usage/ingest"


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def redirect_error(response: HttpResponse) -> SinkError:
    location = _header(response.headers, "Location") or ""
    target = urllib.parse.urlsplit(location)
    where = f"{target.scheme}://{target.hostname}" if target.scheme and target.hostname else "another address"
    return SinkError(
        f"the server redirected ({response.status}) to {where}; redirects are not followed so the API key"
        " cannot leak; set url to the server's final address"
    )


def _error_code(body: bytes) -> str:
    try:
        data = json.loads(body)
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("code") or "error")
    except ValueError:
        pass
    return "error"


@dataclass
class ServerInfo:
    versions: list[str]
    max_records: int
    max_bytes: int
    max_age_days: int | None
    server_time: str | None
    rate_limit: dict[str, Any] | None
    currencies: list[str]
    tools: list[str]
    #: The issue_ref_source values the server knows, when it says (newer servers do); else ``None``.
    issue_ref_sources: list[str] | None = None


class SpecivoSink:
    name = "specivo"
    records_state = True

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        allow_insecure_http: bool = False,
    ) -> None:
        try:
            check_server_url(url, allow_insecure_http=allow_insecure_http)
        except ConfigError as exc:
            raise SinkError(str(exc)) from None
        self.url = endpoint(url)
        self._api_key = api_key
        self.transport = transport
        self.sleep = sleep
        self._info: ServerInfo | None = None
        self.batches_sent = 0

    def __repr__(self) -> str:
        return f"SpecivoSink(url={self.url!r})"

    def _headers(self, json_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": f"ai-usage-reporter/{__version__}",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def info(self) -> ServerInfo:
        if self._info is not None:
            return self._info
        try:
            response = self.transport("GET", self.url, self._headers(False), None)
        except (OSError, urllib.error.URLError) as exc:
            raise SinkError(f"cannot reach the server: {type(exc).__name__}") from None
        if 300 <= response.status < 400:
            raise redirect_error(response)
        if response.status in (401, 403):
            raise SinkError(f"the server refused the API key ({response.status} {_error_code(response.body)})")
        if response.status == 404:
            raise SinkError("the server has no AI usage ingest endpoint (Specivo 0.13.0 or later is needed)")
        if response.status != 200:
            raise SinkError(f"the server answered {response.status} to GET")
        try:
            data = json.loads(response.body)
        except ValueError:
            raise SinkError("the server's answer is not JSON") from None
        self._info = ServerInfo(
            versions=[str(v) for v in data.get("versions", [])],
            max_records=int(data.get("max_records") or DEFAULT_MAX_RECORDS),
            max_bytes=int(data.get("max_bytes") or DEFAULT_MAX_BYTES),
            max_age_days=int(data["max_age_days"]) if data.get("max_age_days") else None,
            server_time=data.get("server_time"),
            rate_limit=data.get("rate_limit"),
            currencies=[str(c) for c in data.get("currencies", [])],
            tools=[str(t) for t in data.get("tools", [])],
            issue_ref_sources=[str(v) for v in data["issue_ref_sources"]]
            if isinstance(data.get("issue_ref_sources"), list)
            else None,
        )
        if CONTRACT_VERSION not in self._info.versions:
            raise SinkError(f"the server does not accept {CONTRACT_VERSION} (it accepts {self._info.versions})")
        return self._info

    def supports_issue_ref_source(self, source: str) -> bool | None:
        """Whether the server knows this ``issue_ref_source``; ``None`` when it does not say."""
        known = self.info().issue_ref_sources
        return None if known is None else source in known

    def _batches(self, records: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        info = self.info()
        envelope = len(json.dumps(make_batch([])).encode())
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        size = envelope
        for record in records:
            record_size = len(json.dumps(record, separators=(",", ":")).encode()) + 1
            if current and (len(current) >= info.max_records or size + record_size > info.max_bytes):
                batches.append(current)
                current, size = [], envelope
            current.append(record)
            size += record_size
        if current:
            batches.append(current)
        return batches

    def send(self, records: Sequence[dict[str, Any]]) -> list[Outcome]:
        outcomes: list[Outcome] = []
        if not records:
            return outcomes
        queue = self._batches(records)
        stop = False
        while queue:
            batch = queue.pop(0)
            if stop:
                outcomes.extend(Outcome(r["idempotency_key"], PENDING, "not_sent") for r in batch)
                continue
            result = self._post(batch)
            if isinstance(result, list):
                outcomes.extend(result)
                continue
            if result == "split":
                if len(batch) == 1:
                    outcomes.append(
                        Outcome(batch[0]["idempotency_key"], "rejected", "batch_too_large", "record too large")
                    )
                else:
                    half = len(batch) // 2
                    queue[:0] = [batch[:half], batch[half:]]
                continue
            if result == "later":
                stop = True
                outcomes.extend(Outcome(r["idempotency_key"], PENDING, "server_unavailable") for r in batch)
        return outcomes

    def _post(self, batch: list[dict[str, Any]]) -> list[Outcome] | str:
        body = json.dumps(make_batch(batch), separators=(",", ":"), ensure_ascii=False).encode()
        server_errors = 0
        rate_limited = 0
        while True:
            try:
                response = self.transport("POST", self.url, self._headers(True), body)
            except (OSError, urllib.error.URLError):
                response = HttpResponse(0, {}, b"")
            self.batches_sent += 1
            if response.status == 200:
                return self._outcomes(batch, response.body)
            if response.status == 429:
                rate_limited += 1
                wait = retry_after_seconds(_header(response.headers, "Retry-After"))
                wait = 60.0 if wait is None else wait
                if rate_limited >= RATE_LIMIT_ATTEMPTS or wait > MAX_RETRY_AFTER_SECONDS:
                    return "later"
                self.sleep(wait)
                continue
            if response.status == 413:
                return "split"
            if 300 <= response.status < 400:
                raise redirect_error(response)
            if response.status in (401, 403):
                raise SinkError(f"the server refused the API key ({response.status} {_error_code(response.body)})")
            if 400 <= response.status < 500:
                raise SinkError(f"the server refused the batch ({response.status} {_error_code(response.body)})")
            server_errors += 1  # 5xx or no answer at all
            if server_errors >= SERVER_ERROR_ATTEMPTS:
                return "later"
            self.sleep(2.0 ** (server_errors - 1))

    @staticmethod
    def _outcomes(batch: list[dict[str, Any]], body: bytes) -> list[Outcome]:
        try:
            data = json.loads(body)
            results = data["results"]
        except (ValueError, KeyError, TypeError):
            return [Outcome(r["idempotency_key"], PENDING, "bad_response") for r in batch]
        by_index: dict[int, dict[str, Any]] = {}
        for item in results if isinstance(results, list) else []:
            if isinstance(item, dict) and isinstance(item.get("index"), int):
                by_index[item["index"]] = item
        outcomes: list[Outcome] = []
        for index, record in enumerate(batch):
            item = by_index.get(index)
            if item is None:
                outcomes.append(Outcome(record["idempotency_key"], PENDING, "no_result"))
                continue
            status = str(item.get("status"))
            warnings = [str(w.get("code")) for w in item.get("warnings") or [] if isinstance(w, dict)]
            error = item.get("error") if isinstance(item.get("error"), dict) else None
            if status not in ("created", "updated", "unchanged"):
                # Unknown statuses count as rejections (contract: treat unknown like the generic case).
                status = "rejected"
            entry_id = item.get("entry_id") if isinstance(item.get("entry_id"), int) else None
            outcomes.append(
                Outcome(
                    idempotency_key=record["idempotency_key"],
                    status=status,
                    error_code=str(error.get("code")) if error else ("unknown" if status == "rejected" else None),
                    error_message=str(error.get("message")) if error and error.get("message") else None,
                    error_field=str(error.get("field")) if error and error.get("field") else None,
                    warnings=warnings,
                    entry_id=entry_id,
                )
            )
        return outcomes
