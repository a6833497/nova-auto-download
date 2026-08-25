#!/usr/bin/env python3
"""Build fail-closed evidence for missing Linky IDs across every configured guild."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from linky_fetch import _authenticated_call


TOKENS = Path("/home/ubuntu/.config/nova/linky-guild-tokens.json")
BJ = dt.timezone(dt.timedelta(hours=8))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _joined_date(value: Any) -> str:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("created_at_missing")
    number = float(value)
    if number > 10_000_000_000:
        number /= 1000
    return dt.datetime.fromtimestamp(number, dt.timezone.utc).astimezone(BJ).date().isoformat()


def scan_guild(
    guild: str,
    target_ids: set[str],
    call: Callable[[str], Any],
    page_size: int,
) -> dict[str, Any]:
    page = 1
    expected_total: int | None = None
    roster: dict[str, str] = {}
    matches: list[dict[str, str]] = []
    while True:
        payload = call(f"/api/guild/search_anchors?page={page}&page_size={page_size}")
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("items"), list):
            raise RuntimeError(f"{guild}:invalid_roster_page:{page}")
        total = payload.get("total_anchors")
        if isinstance(total, bool) or not isinstance(total, (int, float)) or int(total) < 0:
            raise RuntimeError(f"{guild}:invalid_total:{page}")
        total = int(total)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError(f"{guild}:total_drift:{expected_total}:{total}")
        rows = [row for row in payload["items"] if isinstance(row, dict)]
        if len(rows) != len(payload["items"]):
            raise RuntimeError(f"{guild}:invalid_row:{page}")
        for row in rows:
            subject_id = str(row.get("sid") or row.get("user_id") or "").strip()
            if not subject_id or subject_id in roster:
                raise RuntimeError(f"{guild}:missing_or_duplicate_subject:{page}")
            created_at = str(row.get("created_at") if row.get("created_at") is not None else "")
            roster[subject_id] = created_at
            if subject_id in target_ids:
                matches.append({
                    "subjectId": subject_id,
                    "rawGuild": guild,
                    "joinedGuildDate": _joined_date(row.get("created_at")),
                })
        if len(roster) == expected_total:
            break
        if len(roster) > expected_total or not rows:
            raise RuntimeError(f"{guild}:roster_count_mismatch:{len(roster)}:{expected_total}")
        next_page = payload.get("next_page")
        if not isinstance(next_page, int) or isinstance(next_page, bool) or next_page != page + 1:
            raise RuntimeError(f"{guild}:unexpected_next_page:{next_page}")
        page = next_page
        if page > (expected_total // page_size) + 2:
            raise RuntimeError(f"{guild}:page_bound_exceeded:{page}")
    checksum_rows = [[subject_id, roster[subject_id]] for subject_id in sorted(roster)]
    return {
        "rawGuild": guild,
        "totalAnchors": expected_total,
        "rowCount": len(roster),
        "pages": page,
        "rosterChecksum": _canonical_hash(checksum_rows),
        "matches": sorted(matches, key=lambda row: row["subjectId"]),
    }


def build_evidence(
    target_ids: list[str],
    guilds: list[str],
    call_factory: Callable[[str], Callable[[str], Any]],
    *,
    page_size: int = 1000,
    workers: int = 2,
) -> dict[str, Any]:
    normalized_targets = sorted(set(target_ids))
    if len(normalized_targets) != len(target_ids) or any(not subject.isdigit() for subject in normalized_targets):
        raise ValueError("target_ids_invalid")
    normalized_guilds = sorted(set(guilds))
    if len(normalized_guilds) != len(guilds) or not normalized_guilds:
        raise ValueError("guilds_invalid")
    scanned: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_by_guild = {
            pool.submit(scan_guild, guild, set(normalized_targets), call_factory(guild), page_size): guild
            for guild in normalized_guilds
        }
        for future in as_completed(future_by_guild):
            scanned.append(future.result())
    scanned.sort(key=lambda row: row["rawGuild"])
    by_subject: dict[str, list[dict[str, str]]] = {subject_id: [] for subject_id in normalized_targets}
    for guild in scanned:
        for match in guild["matches"]:
            by_subject[match["subjectId"]].append(match)
    results = []
    for subject_id in normalized_targets:
        matches = sorted(by_subject[subject_id], key=lambda row: row["rawGuild"])
        if len(matches) == 1:
            status = "FOUND"
        elif not matches:
            status = "NOT_FOUND"
        else:
            status = "AMBIGUOUS"
        results.append({"subjectId": subject_id, "status": status, "matches": matches})
    summary = {status: sum(row["status"] == status for row in results)
               for status in ("FOUND", "NOT_FOUND", "AMBIGUOUS")}
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence = {
        "schemaVersion": 1,
        "sourceScope": "linky_official_all_configured_guild_rosters",
        "generatedAt": generated_at,
        "targetCount": len(normalized_targets),
        "targetChecksum": _canonical_hash(normalized_targets),
        "configuredGuildCount": len(normalized_guilds),
        "configuredGuildChecksum": _canonical_hash(normalized_guilds),
        "summary": summary,
        "guilds": scanned,
        "results": results,
    }
    evidence["evidenceChecksum"] = _canonical_hash(evidence)
    return evidence


def _load_targets_and_guilds(database_url: str, tokens: Path) -> tuple[list[str], list[str]]:
    import psycopg2

    guilds = sorted(set(json.loads(tokens.read_text(encoding="utf-8"))["guilds"]))
    with psycopg2.connect(database_url) as connection:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("""
              SELECT d.subject_id
              FROM fan_direct_ownerships d
              LEFT JOIN fan_subject_identities i USING(platform,subject_id)
              WHERE d.platform='LINKY' AND d.ended_at IS NULL
                AND i.joined_guild_date IS NULL
                AND d.subject_id ~ '^[1-9][0-9]{7}$'
                AND NOT EXISTS (
                  SELECT 1 FROM fan_invalid_subject_quarantine q
                  WHERE q.platform=d.platform AND q.subject_id=d.subject_id AND q.active)
              ORDER BY d.subject_id""")
            target_ids = [str(row[0]) for row in cursor.fetchall()]
    return target_ids, guilds


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokens", type=Path, default=Path(os.getenv("LINKE_GUILD_TOKENS", str(TOKENS))))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if not 100 <= args.page_size <= 2000:
        raise SystemExit("page-size must be 100..2000")
    if not 1 <= args.workers <= 4:
        raise SystemExit("workers must be 1..4")
    target_ids, guilds = _load_targets_and_guilds(os.environ["DATABASE_URL"], args.tokens)
    evidence = build_evidence(
        target_ids,
        guilds,
        lambda guild: _authenticated_call(guild, str(args.tokens)),
        page_size=args.page_size,
        workers=args.workers,
    )
    _atomic_json(args.output, evidence)
    print(json.dumps({
        "output": str(args.output),
        "targetCount": evidence["targetCount"],
        "configuredGuildCount": evidence["configuredGuildCount"],
        "summary": evidence["summary"],
        "evidenceChecksum": evidence["evidenceChecksum"],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if evidence["summary"]["AMBIGUOUS"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
