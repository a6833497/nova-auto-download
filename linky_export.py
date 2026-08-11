"""Official Linky streamer export adapter with fail-closed validation.

The exporter is an optional read-only candidate source.  No caller may publish
its rows unless :func:`validate_streamer_export` returns successfully.
"""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


EXPORT_PATH = "/api/guild/export_streamer_stat"
MAX_EXPORT_BYTES = 50 * 1024 * 1024
TRUSTED_EXPORT_HOST_SUFFIXES = (".aliyuncs.com", ".linke.ai")


@dataclass(frozen=True)
class ExportEvidence:
    business_date: str
    detail_row_count: int
    expected_row_count: int
    unique_sid_count: int
    detail_amount: str
    summary_row_amount: str
    expected_amount: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "businessDate": self.business_date,
            "detailRowCount": self.detail_row_count,
            "expectedRowCount": self.expected_row_count,
            "uniqueSidCount": self.unique_sid_count,
            "detailAmount": self.detail_amount,
            "summaryRowAmount": self.summary_row_amount,
            "expectedAmount": self.expected_amount,
            "status": self.status,
        }


class ExportValidationError(RuntimeError):
    def __init__(self, code: str, evidence: ExportEvidence | None = None):
        super().__init__(f"Linky streamer export rejected: {code}")
        self.code = code
        self.evidence = evidence


def export_request_body(business_date: str, *, req_type: int = 0,
                        sid: int | None = None) -> dict[str, int | None]:
    """Build the numeric request body used by Linky's current official UI."""
    if len(business_date) != 8 or not business_date.isdigit():
        raise ValueError("business_date must be YYYYMMDD")
    if req_type not in {0, 1}:
        raise ValueError("req_type must be 0 or 1")
    if sid is not None and (isinstance(sid, bool) or int(sid) <= 0):
        raise ValueError("sid must be a positive integer or None")
    return {"begin": int(business_date), "end": int(business_date),
        "req_type": req_type, "sid": int(sid) if sid is not None else None}


def _credentials(guild: str, tokens_path: str | None) -> dict[str, str]:
    path = Path(tokens_path or os.getenv("LINKE_GUILD_TOKENS", "guild-tokens.json"))
    value = json.loads(path.read_text(encoding="utf-8"))["guilds"][guild]
    return {"oauth_token": str(value["oauth_token"]),
        "oauth_token_secret": str(value["oauth_token_secret"])}


def _signed_headers(path: str, credentials: dict[str, str]) -> dict[str, str]:
    stamp = str(int(time.time() * 1000))
    signature = base64.b64encode(hmac.new(
        credentials["oauth_token_secret"].encode(),
        (path + "&" + stamp).encode(), hashlib.sha1).digest()).decode()
    return {"X-Auth-Token": credentials["oauth_token"],
        "X-Auth-Timestamp": stamp, "X-Auth-Signature": signature,
        "X-App-Language": "en", "Country": "US", "User-Agent": "Mozilla/5.0"}


def request_export_url(guild: str, business_date: str, *,
                       tokens_path: str | None = None,
                       urlopen: Callable[..., Any] = urllib.request.urlopen) -> str:
    body = json.dumps(export_request_body(business_date),
        separators=(",", ":")).encode("utf-8")
    credentials = _credentials(guild, tokens_path)
    headers = {**_signed_headers(EXPORT_PATH, credentials),
        "Content-Type": "application/json"}
    request = urllib.request.Request("https://api.linke.ai" + EXPORT_PATH,
        data=body, headers=headers, method="POST")
    try:
        response = json.loads(urlopen(request, timeout=30).read())
    except (OSError, ValueError, TypeError) as error:
        raise ExportValidationError("EXPORT_REQUEST_FAILED") from error
    candidate: Any = response
    if isinstance(candidate, dict):
        candidate = candidate.get("file_url",
            candidate.get("url", candidate.get("data")))
    if isinstance(candidate, dict):
        candidate = candidate.get("url")
    if not isinstance(candidate, str):
        raise ExportValidationError("MISSING_DOWNLOAD_URL")
    if not _trusted_download_url(candidate):
        raise ExportValidationError("UNTRUSTED_DOWNLOAD_URL")
    return candidate


def _trusted_download_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host.endswith(suffix)
        for suffix in TRUSTED_EXPORT_HOST_SUFFIXES)


def download_export(url: str, *, max_bytes: int = MAX_EXPORT_BYTES,
                    urlopen: Callable[..., Any] = urllib.request.urlopen) -> bytes:
    if not _trusted_download_url(url):
        raise ExportValidationError("UNTRUSTED_DOWNLOAD_URL")
    try:
        response = urlopen(urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0"}), timeout=60)
        value = response.read(max_bytes + 1)
    except OSError as error:
        # Do not let a temporary signed download URL escape through a traceback.
        raise ExportValidationError("DOWNLOAD_FAILED") from error
    if len(value) > max_bytes:
        raise ExportValidationError("FILE_TOO_LARGE")
    if not value:
        raise ExportValidationError("EMPTY_FILE")
    return value


def _amount(value: Any) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ExportValidationError("INVALID_AMOUNT") from error
    if not result.is_finite() or result < 0:
        raise ExportValidationError("INVALID_AMOUNT")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_streamer_export(raw: bytes, *, business_date: str,
                             expected_row_count: int,
                             expected_amount: Any) -> tuple[tuple[dict[str, str], ...], ExportEvidence]:
    """Return rows only when count, SID uniqueness and amounts all reconcile."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ExportValidationError("INVALID_ENCODING") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"date", "sid", "Total income"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ExportValidationError("INVALID_COLUMNS")
    rows = tuple(reader)
    totals = [row for row in rows if str(row.get("date") or "").strip() == "Total"]
    details = tuple(row for row in rows if str(row.get("date") or "").strip() != "Total")
    if len(totals) != 1:
        raise ExportValidationError("INVALID_TOTAL_ROW")
    expected_day = f"{business_date[:4]}/{business_date[4:6]}/{business_date[6:]}"
    if any(str(row.get("date") or "").strip() != expected_day for row in details):
        raise ExportValidationError("WRONG_BUSINESS_DATE")
    sids = [str(row.get("sid") or "").strip() for row in details]
    if any(not sid for sid in sids):
        raise ExportValidationError("MISSING_SID")
    if len(set(sids)) != len(sids):
        raise ExportValidationError("DUPLICATE_SID")
    detail_amount = sum((_amount(row["Total income"]) for row in details), Decimal(0))
    summary_amount = _amount(totals[0]["Total income"])
    wanted_amount = _amount(expected_amount)
    evidence = ExportEvidence(business_date=business_date,
        detail_row_count=len(details), expected_row_count=int(expected_row_count),
        unique_sid_count=len(set(sids)), detail_amount=_decimal_text(detail_amount),
        summary_row_amount=_decimal_text(summary_amount),
        expected_amount=_decimal_text(wanted_amount), status="REJECTED")
    if len(details) != int(expected_row_count):
        raise ExportValidationError("TRUNCATED_OR_EXTRA_ROWS", evidence)
    if summary_amount != wanted_amount:
        raise ExportValidationError("SUMMARY_MISMATCH", evidence)
    if detail_amount != wanted_amount:
        raise ExportValidationError("DETAIL_MISMATCH", evidence)
    return details, ExportEvidence(**{**evidence.__dict__, "status": "PASSED"})
