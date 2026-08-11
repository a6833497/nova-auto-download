"""Single fail-closed pagination contract for Linky guild statistics APIs."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

DEFAULT_PAGE_SIZE = 5000
MAX_PAGES = 1000


def configured_page_size(explicit: int | None = None) -> int:
    """Resolve the single authoritative page size for core Linky scans."""
    value = explicit if explicit is not None else os.getenv("LINKY_PAGE_SIZE", str(DEFAULT_PAGE_SIZE))
    try:
        page_size = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("LINKY_PAGE_SIZE must be an integer") from error
    if page_size <= 0 or page_size > 5000:
        raise ValueError("LINKY_PAGE_SIZE must be between 1 and 5000")
    return page_size


def _amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation as error:
        raise RuntimeError(f"Linky amount is invalid: {value!r}") from error


def _canonical_checksum(values: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps(sorted(values), separators=(",", ":")).encode()).hexdigest()


def pull_pages(call: Callable[[str], dict[str, Any]], path: str, day: str, value_key: str,
               page_size: int | None = None, max_pages: int = MAX_PAGES,
               require_unique_sid: bool = True,
               require_summary: bool = False,
               allow_mutable_summary_reconciliation: bool = False,
               _mutable_seed_rows: dict[str, dict[str, Any]] | None = None,
               _mutable_reconciliation_pass_count: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read all raw rows. Filtering zero values never controls pagination."""
    resolved_page_size = configured_page_size(page_size)
    positive_by_sid: dict[str, dict[str, Any]] = dict(_mutable_seed_rows or {})
    evidence: list[dict[str, Any]] = []
    seen_raw_sids: set[str] = set()
    raw_rows_by_sid: dict[str, dict[str, Any]] = {}
    seen_response_checksums: dict[str, int] = {}
    seen_sid_set_checksums: dict[str, int] = {}
    reported_total: int | None = None
    total_item: dict[str, Any] | None = None
    duplicate_sid_count = 0
    total_item_change_count = 0
    raw_count = 0
    for page in range(1, max_pages + 1):
        query = f"{path}?begin={day}&end={day}&page_num={page}&page_size={resolved_page_size}&type=0"
        payload = call(query)
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Linky response items is not a list: page={page}")
        raw_sids = [str(row.get("sid") or "").strip() for row in items]
        if any(not sid for sid in raw_sids):
            raise RuntimeError(f"Linky raw row has no SID: page={page}")
        response_checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        sid_set_checksum = hashlib.sha256(
            json.dumps(sorted(set(raw_sids)), separators=(",", ":")).encode()).hexdigest()
        if response_checksum in seen_response_checksums:
            raise RuntimeError(f"Linky repeated response page: page={page} repeats={seen_response_checksums[response_checksum]}")
        if sid_set_checksum in seen_sid_set_checksums:
            raise RuntimeError(f"Linky repeated SID set: page={page} repeats={seen_sid_set_checksums[sid_set_checksum]}")
        page_sids: set[str] = set()
        intra_page_duplicates: set[str] = set()
        cross_page_duplicates: set[str] = set()
        for sid in raw_sids:
            if sid in page_sids:
                intra_page_duplicates.add(sid)
            elif sid in seen_raw_sids:
                cross_page_duplicates.add(sid)
            page_sids.add(sid)
        if intra_page_duplicates:
            raise RuntimeError(f"Linky duplicate raw SID within page: page={page} sid={sorted(intra_page_duplicates)[0]}")
        if cross_page_duplicates and (require_unique_sid and not allow_mutable_summary_reconciliation):
            raise RuntimeError(f"Linky duplicate raw SID across pages: page={page} sid={sorted(cross_page_duplicates)[0]}")
        if cross_page_duplicates and not allow_mutable_summary_reconciliation:
            raise RuntimeError(f"Linky duplicate raw SID cannot be silently overwritten: page={page} sid={sorted(cross_page_duplicates)[0]}")
        rows_for_page = {str(row["sid"]).strip(): row for row in items}
        for sid in cross_page_duplicates:
            if raw_rows_by_sid[sid] != rows_for_page[sid]:
                raise RuntimeError(f"Linky duplicate raw SID changed across pages: page={page} sid={sid}")
        duplicate_sid_count += len(cross_page_duplicates)
        seen_raw_sids.update(raw_sids)
        raw_rows_by_sid.update(rows_for_page)
        seen_response_checksums[response_checksum] = page
        seen_sid_set_checksums[sid_set_checksum] = page
        raw_count += len(items)
        for row in items:
            if float(row.get(value_key) or 0) > 0:
                positive_by_sid[str(row["sid"]).strip()] = row
        try:
            numeric_total = int(payload.get("total"))
        except (TypeError, ValueError):
            raise RuntimeError(f"Linky response total is invalid: page={page}")
        if reported_total is None:
            reported_total = numeric_total
            candidate = payload.get("total_item")
            if candidate is not None and not isinstance(candidate, dict):
                raise RuntimeError("Linky response total_item is not an object: page=1")
            total_item = candidate
            if require_summary and (total_item is None or value_key not in total_item):
                raise RuntimeError(f"Linky response total_item has no {value_key}: page=1")
        elif numeric_total != reported_total:
            raise RuntimeError(f"Linky response total changed: page={page}")
        elif payload.get("total_item") not in (None, {}) and payload.get("total_item") != total_item:
            if not allow_mutable_summary_reconciliation:
                raise RuntimeError(f"Linky response total_item changed: page={page}")
            candidate = payload.get("total_item")
            if not isinstance(candidate, dict) or value_key not in candidate:
                raise RuntimeError(f"Linky mutable response total_item has no {value_key}: page={page}")
            total_item = candidate
            total_item_change_count += 1
        if raw_count > reported_total:
            raise RuntimeError(f"Linky raw rows exceed reported total: page={page}")
        evidence.append({"page": page, "rawCount": len(items), "positiveCount": sum(
            1 for row in items if float(row.get(value_key) or 0) > 0),
            "reportedTotal": numeric_total, "responseChecksum": response_checksum,
            "sidSetChecksum": sid_set_checksum})
        if raw_count == reported_total:
            break
        if len(items) < resolved_page_size:
            raise RuntimeError(f"Linky pagination ended before reported total: page={page}")
    else:
        raise RuntimeError(f"Linky pagination exceeded safety cap: {max_pages}")
    rows = list(positive_by_sid.values())
    detail_amount = sum((_amount(row.get(value_key)) for row in rows), Decimal(0))
    summary_amount = _amount(total_item.get(value_key)) if total_item is not None and value_key in total_item else None
    if require_summary and detail_amount != summary_amount:
        raise RuntimeError(f"Linky detail amount differs from total_item: detail={detail_amount} summary={summary_amount}")
    if allow_mutable_summary_reconciliation and (duplicate_sid_count or total_item_change_count):
        if summary_amount is None:
            raise RuntimeError("Linky mutable pagination drift has no reconcilable total_item")
        if detail_amount != summary_amount:
            if _mutable_reconciliation_pass_count == 0:
                return pull_pages(call, path, day, value_key, page_size=resolved_page_size,
                    max_pages=max_pages, require_unique_sid=require_unique_sid,
                    require_summary=require_summary,
                    allow_mutable_summary_reconciliation=True,
                    _mutable_seed_rows=positive_by_sid,
                    _mutable_reconciliation_pass_count=1)
            raise RuntimeError(
                f"Linky mutable pagination drift did not reconcile after merge: "
                f"detail={detail_amount} summary={summary_amount}")
    final_count = evidence[-1]["rawCount"] if evidence else 0
    expected_final = (reported_total or 0) % resolved_page_size or (
        resolved_page_size if (reported_total or 0) else 0)
    if final_count != expected_final:
        raise RuntimeError(f"Linky final page size is invalid: actual={final_count} expected={expected_final}")
    if evidence:
        evidence[-1]["scanSummary"] = {
            "requestedPageSize": resolved_page_size,
            "uniqueSidCount": len(seen_raw_sids),
            "duplicateSidCount": duplicate_sid_count,
            "totalChangeCount": total_item_change_count,
            "reconciliationPassCount": _mutable_reconciliation_pass_count,
            "repeatedPageCount": 0,
            "detailAmount": str(detail_amount),
            "totalItemAmount": str(summary_amount) if summary_amount is not None else None,
            "canonicalSidChecksum": hashlib.sha256(
                json.dumps(sorted(seen_raw_sids), separators=(",", ":")).encode()).hexdigest(),
            "canonicalAmountChecksum": _canonical_checksum([
                (str(row["sid"]).strip(), str(_amount(row.get(value_key)))) for row in rows]),
            "finalPageRowCount": final_count,
            "expectedFinalPageRowCount": expected_final,
        }
    return rows, evidence
