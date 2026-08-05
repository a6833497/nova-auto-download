"""Single fail-closed pagination contract for Linky guild statistics APIs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

PAGE_SIZE = 500


def pull_pages(call: Callable[[str], dict[str, Any]], path: str, day: str, value_key: str,
               page_size: int = PAGE_SIZE, max_pages: int = 120,
               require_unique_sid: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read all raw rows. Filtering zero values never controls pagination."""
    positive_by_sid: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    seen_raw_sids: set[str] = set()
    seen_response_checksums: dict[str, int] = {}
    seen_sid_set_checksums: dict[str, int] = {}
    reported_total: int | None = None
    raw_count = 0
    for page in range(1, max_pages + 1):
        query = f"{path}?begin={day}&end={day}&page_num={page}&page_size={page_size}&type=0"
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
        duplicates: set[str] = set()
        for sid in raw_sids:
            if sid in page_sids or sid in seen_raw_sids:
                duplicates.add(sid)
            page_sids.add(sid)
        if duplicates and require_unique_sid:
            raise RuntimeError(f"Linky duplicate raw SID: page={page} sid={sorted(duplicates)[0]}")
        if duplicates:
            raise RuntimeError(f"Linky duplicate raw SID cannot be silently overwritten: page={page} sid={sorted(duplicates)[0]}")
        seen_raw_sids.update(raw_sids)
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
        elif numeric_total != reported_total:
            raise RuntimeError(f"Linky response total changed: page={page}")
        if raw_count > reported_total:
            raise RuntimeError(f"Linky raw rows exceed reported total: page={page}")
        evidence.append({"page": page, "rawCount": len(items), "positiveCount": sum(
            1 for row in items if float(row.get(value_key) or 0) > 0),
            "reportedTotal": numeric_total, "responseChecksum": response_checksum,
            "sidSetChecksum": sid_set_checksum})
        if raw_count == reported_total:
            break
        if len(items) < page_size:
            raise RuntimeError(f"Linky pagination ended before reported total: page={page}")
    else:
        raise RuntimeError(f"Linky pagination exceeded safety cap: {max_pages}")
    return list(positive_by_sid.values()), evidence
