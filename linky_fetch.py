"""One complete Linky fetch per guild/day/request scope.

This module owns network reads only.  Database transformations and writes remain
the responsibility of the existing consumers, which may share a FetchBundle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, MutableMapping, Tuple
import urllib.request
import urllib.error

from linky_api_pagination import MutableSnapshotIncomplete, configured_page_size, pull_pages


LinkyCall = Callable[[str], Dict[str, Any]]
RequestScope = MutableMapping[Tuple[str, str, str], "FetchBundle"]


def _json_value_type(value: Any, present: bool) -> str:
    if not present:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _integer_like(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        int(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


class FetchScanError(RuntimeError):
    def __init__(self, message: str, observation: dict[str, Any], *,
                 cache_rows_by_endpoint: dict[str, tuple[dict[str, Any], ...]] | None = None):
        super().__init__(message)
        self.observation = observation
        self.cache_rows_by_endpoint = cache_rows_by_endpoint or {}


class BatchDeadlineExceeded(RuntimeError):
    pass


class GuildDeadlineExceeded(RuntimeError):
    pass


class EndpointDeadlineExceeded(RuntimeError):
    pass


def _positive_seconds(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _fallback_page_size(primary: int) -> int:
    value = configured_page_size(int(os.getenv("LINKY_FALLBACK_PAGE_SIZE", "4096")))
    if value == primary:
        value = 4999 if primary != 4999 else 4096
    return value


@dataclass(frozen=True)
class EndpointScan:
    endpoint: str
    page_count: int
    raw_row_count: int
    positive_row_count: int
    request_count: int
    retry_count: int
    api_elapsed_seconds: float
    scan_complete: bool
    reported_total: int
    requested_page_size: int
    unique_sid_count: int
    duplicate_sid_count: int
    total_change_count: int
    reconciliation_pass_count: int
    repeated_page_count: int
    detail_amount: str
    total_item_amount: str
    canonical_sid_checksum: str
    canonical_amount_checksum: str
    final_page_row_count: int
    expected_final_page_row_count: int
    pages: tuple[dict[str, Any], ...]

    def observation(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "pageCount": self.page_count,
            "rawRowCount": self.raw_row_count,
            "positiveRowCount": self.positive_row_count,
            "requestCount": self.request_count,
            "retryCount": self.retry_count,
            "apiElapsedSeconds": self.api_elapsed_seconds,
            "scanComplete": self.scan_complete,
            "reportedTotal": self.reported_total,
            "requestedPageSize": self.requested_page_size,
            "uniqueSidCount": self.unique_sid_count,
            "duplicateSidCount": self.duplicate_sid_count,
            "totalChangeCount": self.total_change_count,
            "reconciliationPassCount": self.reconciliation_pass_count,
            "repeatedPageCount": self.repeated_page_count,
            "detailAmount": self.detail_amount,
            "totalItemAmount": self.total_item_amount,
            "canonicalSidChecksum": self.canonical_sid_checksum,
            "canonicalAmountChecksum": self.canonical_amount_checksum,
            "finalPageRowCount": self.final_page_row_count,
            "expectedFinalPageRowCount": self.expected_final_page_row_count,
        }


@dataclass(frozen=True)
class FetchBundle:
    source_guild: str
    business_date: str
    streamer_rows: tuple[dict[str, Any], ...]
    voice_room_rows: tuple[dict[str, Any], ...]
    streamer_scan: EndpointScan
    voice_room_scan: EndpointScan
    online_anchor_sids: frozenset[int]
    online_scan: EndpointScan | None
    bundle_reused: bool = False

    @property
    def scan_complete(self) -> bool:
        return self.streamer_scan.scan_complete and self.voice_room_scan.scan_complete

    def observations(self) -> tuple[dict[str, Any], ...]:
        """Return endpoint records ready for the runner to enrich with job/batch."""
        scans = [self.streamer_scan, self.voice_room_scan]
        if self.online_scan is not None:
            scans.append(self.online_scan)
        return tuple({
            "businessDate": self.business_date,
            "sourceGuild": self.source_guild,
            "bundleReused": self.bundle_reused,
            **scan.observation(),
        } for scan in scans)


def new_request_scope() -> RequestScope:
    """Return an explicitly batch-local memo; no cache survives this object."""
    return {}


def _scan(call: LinkyCall, endpoint: str, day: str, value_key: str,
          page_size: int, require_summary: bool = True,
          allow_mutable_summary_reconciliation: bool = False,
          fallback_page_size: int | None = None,
          mutable_seed_rows: tuple[dict[str, Any], ...] = ()) -> tuple[tuple[dict[str, Any], ...], EndpointScan]:
    started = time.monotonic()
    requests = 0
    starting_attempts = int(getattr(call, "attempt_count", 0))
    starting_retries = int(getattr(call, "retry_count", 0))
    observed_raw_count = 0
    observed_positive_count = 0
    observed_total: Any = None
    observed_total_present = False
    def observed_call(path: str) -> dict[str, Any]:
        nonlocal requests, observed_raw_count, observed_positive_count, observed_total, observed_total_present
        requests += 1
        payload = call(path)
        items = payload.get("items") or []
        if isinstance(items, list):
            observed_raw_count += len(items)
            observed_positive_count += sum(1 for row in items
                if isinstance(row, dict) and float(row.get(value_key) or 0) > 0)
        observed_total_present = "total" in payload
        observed_total = payload.get("total")
        return payload
    try:
        rows, pages = pull_pages(observed_call, endpoint, day, value_key,
            page_size=page_size, require_unique_sid=True, require_summary=require_summary,
            allow_mutable_summary_reconciliation=allow_mutable_summary_reconciliation,
            fallback_page_size=fallback_page_size,
            _mutable_seed_rows={str(row["sid"]): row for row in mutable_seed_rows})
    except Exception as error:
        actual_requests = int(getattr(call, "attempt_count", starting_attempts + requests)) - starting_attempts
        retries = int(getattr(call, "retry_count", starting_retries)) - starting_retries
        safe_reported_total = observed_total if isinstance(observed_total, (str, int, float, bool)) else None
        observation = {"endpoint": endpoint, "pageCount": requests,
            "rawRowCount": observed_raw_count,
            "positiveRowCount": observed_positive_count,
            "requestCount": actual_requests, "retryCount": retries,
            "apiElapsedSeconds": time.monotonic() - started, "scanComplete": False,
            "httpStatus": getattr(call, "last_http_status", None),
            "reportedTotal": safe_reported_total,
            "reportedTotalPresent": observed_total_present,
            "reportedTotalType": _json_value_type(observed_total, observed_total_present),
            "reportedTotalIntegerLike": _integer_like(observed_total),
            "requestedPageSize": page_size}
        cache_rows = {endpoint: error.rows} if isinstance(error, MutableSnapshotIncomplete) else None
        if isinstance(error, MutableSnapshotIncomplete):
            observation["detailAmount"] = str(error.detail_amount)
            observation["totalItemAmount"] = str(error.summary_amount)
        raise FetchScanError(str(error), observation,
            cache_rows_by_endpoint=cache_rows) from error
    elapsed = time.monotonic() - started
    actual_requests = int(getattr(call, "attempt_count", starting_attempts + requests)) - starting_attempts
    retries = int(getattr(call, "retry_count", starting_retries)) - starting_retries
    summary = pages[-1]["scanSummary"]
    scan = EndpointScan(
        endpoint=endpoint,
        page_count=len(pages),
        raw_row_count=sum(int(page["rawCount"]) for page in pages),
        positive_row_count=sum(int(page["positiveCount"]) for page in pages),
        request_count=actual_requests,
        retry_count=retries,
        api_elapsed_seconds=elapsed,
        scan_complete=True,
        reported_total=int(pages[-1]["reportedTotal"]) if pages else 0,
        requested_page_size=summary["requestedPageSize"],
        unique_sid_count=summary["uniqueSidCount"],
        duplicate_sid_count=summary["duplicateSidCount"],
        total_change_count=summary["totalChangeCount"],
        reconciliation_pass_count=summary["reconciliationPassCount"],
        repeated_page_count=summary["repeatedPageCount"],
        detail_amount=summary["detailAmount"],
        total_item_amount=summary["totalItemAmount"],
        canonical_sid_checksum=summary["canonicalSidChecksum"],
        canonical_amount_checksum=summary["canonicalAmountChecksum"],
        final_page_row_count=summary["finalPageRowCount"],
        expected_final_page_row_count=summary["expectedFinalPageRowCount"],
        pages=tuple(pages),
    )
    return tuple(rows), scan


def _online_anchors(call: LinkyCall, page_size: int, max_pages: int = 40) -> tuple[frozenset[int], EndpointScan]:
    endpoint = "/api/guild/online_anchors"
    started = time.monotonic()
    sids: set[int] = set()
    pages: list[dict[str, Any]] = []
    requests = 0
    starting_attempts = int(getattr(call, "attempt_count", 0))
    starting_retries = int(getattr(call, "retry_count", 0))
    complete = False
    try:
        for page_number in range(1, max_pages + 1):
            requests += 1
            payload = call(f"{endpoint}?page={page_number}&page_size={page_size}")
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise RuntimeError("Linky online anchors items is not a list")
            sids.update(int(row["sid"]) for row in items)
            pages.append({"page": page_number, "rawCount": len(items), "positiveCount": len(items)})
            if not payload.get("next_page") or not items:
                complete = True
                break
    except Exception:
        # Online presence is best-effort, but is never restarted from page one.
        complete = False
    actual_requests = int(getattr(call, "attempt_count", starting_attempts + requests)) - starting_attempts
    retries = int(getattr(call, "retry_count", starting_retries)) - starting_retries
    return frozenset(sids), EndpointScan(
        endpoint=endpoint, page_count=len(pages),
        raw_row_count=sum(page["rawCount"] for page in pages),
        positive_row_count=len(sids), request_count=actual_requests, retry_count=retries,
        api_elapsed_seconds=time.monotonic() - started, scan_complete=complete,
        reported_total=sum(page["rawCount"] for page in pages),
        requested_page_size=page_size, unique_sid_count=len(sids), duplicate_sid_count=0,
        total_change_count=0, reconciliation_pass_count=0, repeated_page_count=0, detail_amount="0",
        total_item_amount="0", canonical_sid_checksum="", canonical_amount_checksum="",
        final_page_row_count=pages[-1]["rawCount"] if pages else 0,
        expected_final_page_row_count=pages[-1]["rawCount"] if pages else 0,
        pages=tuple(pages),
    )


def _authenticated_call(guild: str, tokens_path: str | None = None) -> LinkyCall:
    config_path = Path(tokens_path or os.getenv("LINKE_GUILD_TOKENS", "guild-tokens.json"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    credentials = config["guilds"][guild]

    max_retries = int(os.getenv("LINKY_REQUEST_RETRIES", "2"))
    if max_retries < 0 or max_retries > 3:
        raise ValueError("LINKY_REQUEST_RETRIES must be between 0 and 3")
    def call(path: str) -> dict[str, Any]:
        for attempt in range(max_retries + 1):
            call.attempt_count += 1
            stamp = str(int(time.time() * 1000))
            signature = base64.b64encode(hmac.new(credentials["oauth_token_secret"].encode(),
                (path + "&" + stamp).encode(), hashlib.sha1).digest()).decode()
            request = urllib.request.Request("https://api.linke.ai" + path, headers={
                "X-Auth-Token": credentials["oauth_token"], "X-Auth-Timestamp": stamp,
                "X-Auth-Signature": signature, "X-App-Language": "en", "Country": "US",
                "User-Agent": "Mozilla/5.0",
            })
            try:
                response = urllib.request.urlopen(request, timeout=30)
                call.last_http_status = getattr(response, "status", None)
                return json.loads(response.read())
            except urllib.error.HTTPError as error:
                call.last_http_status = error.code
                if error.code not in {502, 503, 504} or attempt == max_retries:
                    raise
                call.retry_count += 1
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")
    call.attempt_count = 0
    call.retry_count = 0
    call.last_http_status = None
    return call


def fetch_guild_day(
    guild: str,
    business_date: str,
    *,
    call: LinkyCall | None = None,
    request_scope: RequestScope | None = None,
    utc_today: dt.date | None = None,
    tokens_path: str | None = None,
    deadline_monotonic: float | None = None,
    page_size: int | None = None,
    mutable_seed_rows_by_endpoint: dict[str, tuple[dict[str, Any], ...]] | None = None,
) -> FetchBundle:
    """Fetch both core endpoints completely before returning a reusable bundle.

    ``business_date`` is YYYYMMDD.  A caller-owned ``request_scope`` guarantees
    identical reads are performed at most once inside that scheduling cycle.
    """
    parsed_date = dt.datetime.strptime(business_date, "%Y%m%d").date()
    effective_today = utc_today or dt.datetime.now(dt.timezone.utc).date()
    # A current-day detail snapshot is publishable only when it reconciles with
    # the response summary. Empty or malformed summaries remain fail-closed.
    require_summary = True
    resolved_page_size = configured_page_size(page_size)
    fallback_page_size = _fallback_page_size(resolved_page_size) if parsed_date == effective_today else None
    api_call = call or _authenticated_call(guild, tokens_path)
    batch_deadline = deadline_monotonic
    guild_deadline = time.monotonic() + _positive_seconds("LINKY_GUILD_MAX_SECONDS", "480")
    endpoint_deadline = guild_deadline
    def bounded_call(path: str) -> dict[str, Any]:
        now = time.monotonic()
        if batch_deadline is not None and now >= batch_deadline:
            raise BatchDeadlineExceeded("Linky batch deadline reached before next request")
        if now >= guild_deadline:
            raise GuildDeadlineExceeded("Linky guild deadline reached before next request")
        if now >= endpoint_deadline:
            raise EndpointDeadlineExceeded("Linky endpoint deadline reached before next request")
        if not hasattr(api_call, "attempt_count"):
            bounded_call.attempt_count += 1
        try:
            return api_call(path)
        finally:
            if hasattr(api_call, "attempt_count"):
                bounded_call.attempt_count = int(api_call.attempt_count)
                bounded_call.retry_count = int(api_call.retry_count)
            bounded_call.last_http_status = getattr(api_call, "last_http_status", None)
    bounded_call.attempt_count = int(getattr(api_call, "attempt_count", 0))
    bounded_call.retry_count = int(getattr(api_call, "retry_count", 0))
    bounded_call.last_http_status = getattr(api_call, "last_http_status", None)
    cache_key = (guild, business_date, f"type=0,page_size={resolved_page_size}")
    if request_scope is not None and cache_key in request_scope:
        return replace(request_scope[cache_key], bundle_reused=True)

    endpoint_deadline = min(guild_deadline,
        time.monotonic() + _positive_seconds("LINKY_ENDPOINT_MAX_SECONDS", "240"))
    streamer_rows, streamer_scan = _scan(
        bounded_call, "/api/guild/streamer_stat", business_date, "total_earns",
        resolved_page_size, require_summary=require_summary,
        allow_mutable_summary_reconciliation=parsed_date == effective_today,
        fallback_page_size=fallback_page_size,
        mutable_seed_rows=(mutable_seed_rows_by_endpoint or {}).get(
            "/api/guild/streamer_stat", ()))
    try:
        endpoint_deadline = min(guild_deadline,
            time.monotonic() + _positive_seconds("LINKY_ENDPOINT_MAX_SECONDS", "240"))
        room_rows, room_scan = _scan(
            bounded_call, "/api/guild/live_room_stat", business_date, "receive_diamonds",
            resolved_page_size, require_summary=require_summary,
            allow_mutable_summary_reconciliation=parsed_date == effective_today,
            fallback_page_size=fallback_page_size,
            mutable_seed_rows=(mutable_seed_rows_by_endpoint or {}).get(
                "/api/guild/live_room_stat", ()))
    except FetchScanError as error:
        if parsed_date == effective_today:
            error.cache_rows_by_endpoint.setdefault(
                "/api/guild/streamer_stat", streamer_rows)
        raise

    online_sids: frozenset[int] = frozenset()
    online_scan: EndpointScan | None = None
    if parsed_date == effective_today:
        endpoint_deadline = min(guild_deadline,
            time.monotonic() + _positive_seconds("LINKY_ENDPOINT_MAX_SECONDS", "240"))
        online_sids, online_scan = _online_anchors(bounded_call, resolved_page_size)

    bundle = FetchBundle(
        source_guild=guild, business_date=business_date,
        streamer_rows=streamer_rows, voice_room_rows=room_rows,
        streamer_scan=streamer_scan, voice_room_scan=room_scan,
        online_anchor_sids=online_sids, online_scan=online_scan,
    )
    if request_scope is not None:
        request_scope[cache_key] = bundle
    return bundle
