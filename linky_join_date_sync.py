#!/usr/bin/env python3
"""Fill missing Linky guild-join dates from the official exact-ID endpoint."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
from typing import Any

from linky_fetch import _authenticated_call


TOKENS = Path("/home/ubuntu/.config/nova/linky-guild-tokens.json")
BJ = dt.timezone(dt.timedelta(hours=8))


def _joined_date(value: Any) -> str:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError("created_at_missing")
    number = float(value)
    if number > 10_000_000_000:
        number /= 1000
    return dt.datetime.fromtimestamp(number, dt.timezone.utc).astimezone(BJ).date().isoformat()


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("response_items_invalid")
    if "total" in payload and (isinstance(payload["total"], bool) or not isinstance(payload["total"], (int, float))):
        raise ValueError("response_total_invalid")
    return [row for row in payload["items"] if isinstance(row, dict)]


def lookup(subject_id: str, guilds: list[str], tokens: Path = TOKENS) -> dict[str, Any]:
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat()
    found: list[tuple[str, str]] = []
    errors: list[str] = []
    for guild in guilds:
        try:
            call = _authenticated_call(guild, str(tokens))
            path = "/api/guild/search_anchors?id=" + urllib.parse.quote(subject_id) + "&page=1&page_size=100"
            rows = _items(call(path))
            exact = [row for row in rows if str(row.get("sid") or row.get("user_id") or "").strip() == subject_id]
            for row in exact:
                found.append((guild, _joined_date(row.get("created_at"))))
        except Exception as exc:  # fail closed per guild; never infer a date
            errors.append(f"{guild}:{type(exc).__name__}")
        time.sleep(0.12)
    dates = sorted({value for _, value in found})
    evidence = {"subjectId": subject_id, "guilds": guilds, "foundGuilds": sorted({g for g, _ in found}),
                "dates": dates, "checkedAt": checked_at}
    checksum = "sha256:" + hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if len(dates) == 1:
        return {"status": "FOUND", "date": dates[0], "guild": found[0][0], "checked": checked_at,
                "checksum": checksum, "error": None}
    if len(dates) > 1:
        return {"status": "ERROR", "date": None, "guild": None, "checked": checked_at,
                "checksum": checksum, "error": "conflicting_official_dates"}
    if errors:
        return {"status": "SOURCE_STALE", "date": None, "guild": None, "checked": checked_at,
                "checksum": checksum, "error": ",".join(errors)[:500]}
    return {"status": "NOT_FOUND", "date": None, "guild": None, "checked": checked_at,
            "checksum": checksum, "error": "official_exact_id_not_found"}


def candidates(connection: Any, configured: set[str], limit: int) -> list[tuple[str, str, list[str]]]:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT d.subject_id,d.guild_name,
                 array_agg(DISTINCT g.raw_guild ORDER BY g.raw_guild) FILTER (WHERE g.raw_guild IS NOT NULL)
          FROM fan_direct_ownerships d
          LEFT JOIN fan_subject_identities i USING(platform,subject_id)
          LEFT JOIN fan_subject_join_date_lookup_state s USING(platform,subject_id)
          LEFT JOIN guild_source_dictionary g ON g.source_key='LINKY' AND g.active
            AND g.guild_alias=d.guild_name AND g.effective_from<=CURRENT_DATE
            AND (g.effective_to IS NULL OR g.effective_to>=CURRENT_DATE)
          WHERE d.platform='LINKY' AND d.ended_at IS NULL AND i.joined_guild_date IS NULL
            AND NOT EXISTS (SELECT 1 FROM fan_invalid_subject_quarantine q
              WHERE q.platform=d.platform AND q.subject_id=d.subject_id AND q.active)
            AND (s.last_checked_at IS NULL OR s.status IN ('ERROR','SOURCE_STALE')
              OR s.last_checked_at < now()-INTERVAL '7 days')
          GROUP BY d.subject_id,d.guild_name,s.last_checked_at
          ORDER BY s.last_checked_at NULLS FIRST,d.subject_id
          LIMIT %s""", (limit,))
        result = []
        for subject_id, guild_name, guilds in cursor.fetchall():
            valid = [str(guild) for guild in (guilds or []) if str(guild) in configured]
            result.append((str(subject_id), str(guild_name or ""), valid))
        return result


def persist(connection: Any, subject_id: str, guild_name: str, guilds: list[str], result: dict[str, Any]) -> bool:
    with connection.cursor() as cursor:
        if result["status"] == "FOUND":
            cursor.execute("""
              INSERT INTO fan_subject_identities(platform,subject_id,guild_id,guild_name,joined_guild_date,newcomer_revision)
              VALUES('LINKY',%s,%s,%s,%s::date,1)
              ON CONFLICT(platform,subject_id) DO UPDATE SET joined_guild_date=EXCLUDED.joined_guild_date,updated_at=now()
              WHERE fan_subject_identities.joined_guild_date IS NULL""",
              (subject_id, result["guild"] or guilds[0], guild_name or result["guild"], result["date"]))
        cursor.execute("""
          INSERT INTO fan_subject_join_date_lookup_state(
            platform,subject_id,status,source_scope,source_guild_id,source_snapshot_at,
            source_generation,source_checksum,error_code,last_checked_at)
          VALUES('LINKY',%s,%s,'linky_official_search_anchors',%s,%s::timestamptz,%s,%s,%s,now())
          ON CONFLICT(platform,subject_id) DO UPDATE SET status=EXCLUDED.status,
            source_scope=EXCLUDED.source_scope,source_guild_id=EXCLUDED.source_guild_id,
            source_snapshot_at=EXCLUDED.source_snapshot_at,source_generation=EXCLUDED.source_generation,
            source_checksum=EXCLUDED.source_checksum,error_code=EXCLUDED.error_code,
            last_checked_at=now(),updated_at=now()""",
          (subject_id, result["status"], ",".join(guilds) or None, result["checked"],
           "linky-search-v1", result["checksum"], result["error"]))
    connection.commit()
    return result["status"] == "FOUND"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=int(os.getenv("LINKY_JOIN_DATE_LIMIT", "200")))
    parser.add_argument("--tokens", type=Path, default=Path(os.getenv("LINKE_GUILD_TOKENS", str(TOKENS))))
    args = parser.parse_args()
    if not 1 <= args.limit <= 2000:
        raise SystemExit("limit must be 1..2000")
    database_url = os.environ["DATABASE_URL"]
    import psycopg2
    configured = set(json.loads(args.tokens.read_text(encoding="utf-8"))["guilds"])
    summary = {"requested": 0, "found": 0, "notFound": 0, "sourceStale": 0, "errors": 0, "unmapped": 0}
    with psycopg2.connect(database_url) as connection:
        for subject_id, guild_name, guilds in candidates(connection, configured, args.limit):
            summary["requested"] += 1
            if not guilds:
                result = {"status": "ERROR", "date": None, "guild": None,
                          "checked": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "checksum": None, "error": "guild_mapping_unresolved"}
                summary["unmapped"] += 1
            else:
                result = lookup(subject_id, guilds, args.tokens)
            persist(connection, subject_id, guild_name, guilds, result)
            key = {"FOUND": "found", "NOT_FOUND": "notFound", "SOURCE_STALE": "sourceStale", "ERROR": "errors"}[result["status"]]
            summary[key] += 1
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0 if not summary["sourceStale"] and not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
