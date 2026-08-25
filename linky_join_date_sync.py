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
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("response_items_invalid")
    # Linky omits both `items` and `total_anchors` for a valid empty exact-ID
    # search. `next_page` is still present and is the response-contract marker.
    if "items" not in payload and "next_page" in payload:
        return []
    if not isinstance(payload.get("items"), list):
        raise ValueError("response_items_invalid")
    if "total_anchors" in payload and (isinstance(payload["total_anchors"], bool)
                                        or not isinstance(payload["total_anchors"], (int, float))):
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
    found_guilds = sorted({guild for guild, _ in found})
    evidence = {"subjectId": subject_id, "guilds": guilds, "foundGuilds": sorted({g for g, _ in found}),
                "dates": dates, "checkedAt": checked_at}
    checksum = "sha256:" + hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if errors:
        return {"status": "SOURCE_STALE", "date": None, "guild": None, "checked": checked_at,
                "checksum": checksum, "error": ",".join(errors)[:500]}
    if len(found_guilds) == 1 and len(dates) == 1:
        return {"status": "FOUND", "date": dates[0], "guild": found_guilds[0], "checked": checked_at,
                "checksum": checksum, "error": None}
    if len(found_guilds) > 1:
        return {"status": "ERROR", "date": None, "guild": None, "checked": checked_at,
                "checksum": checksum, "error": "multiple_official_guilds"}
    if found_guilds:
        return {"status": "ERROR", "date": None, "guild": None, "checked": checked_at,
                "checksum": checksum, "error": "conflicting_official_records"}
    return {"status": "NOT_FOUND", "date": None, "guild": None, "checked": checked_at,
            "checksum": checksum, "error": "official_exact_id_not_found"}


def candidates(connection: Any, limit: int,
               subject_ids: list[str] | None = None) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT d.subject_id
          FROM fan_direct_ownerships d
          LEFT JOIN fan_subject_identities i USING(platform,subject_id)
          LEFT JOIN fan_subject_join_date_lookup_state s USING(platform,subject_id)
          WHERE d.platform='LINKY' AND d.ended_at IS NULL AND i.joined_guild_date IS NULL
            AND d.subject_id ~ '^[1-9][0-9]{7}$'
            AND (%s::text[] IS NULL OR d.subject_id=ANY(%s::text[]))
            AND NOT EXISTS (SELECT 1 FROM fan_invalid_subject_quarantine q
              WHERE q.platform=d.platform AND q.subject_id=d.subject_id AND q.active)
            AND (s.last_checked_at IS NULL OR s.status IN ('ERROR','SOURCE_STALE')
              OR s.last_checked_at < now()-INTERVAL '7 days')
          GROUP BY d.subject_id,s.last_checked_at
          ORDER BY s.last_checked_at NULLS FIRST,d.subject_id
          LIMIT %s""", (subject_ids or None, subject_ids or None, limit))
        return [str(row[0]) for row in cursor.fetchall()]


def guild_mappings(connection: Any, configured: set[str]) -> dict[str, tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT DISTINCT ON (raw_guild) raw_guild,guild_alias,country
          FROM guild_source_dictionary
          WHERE source_key='LINKY' AND active AND raw_guild=ANY(%s::text[])
            AND effective_from<=CURRENT_DATE
            AND (effective_to IS NULL OR effective_to>=CURRENT_DATE)
          ORDER BY raw_guild,effective_from DESC""", (sorted(configured),))
        result = {str(raw): (str(alias), str(country)) for raw, alias, country in cursor.fetchall()
                  if raw and alias and country}
    if set(result) != configured:
        missing = ",".join(sorted(configured - set(result)))
        raise RuntimeError(f"configured_guild_mapping_incomplete:{missing}")
    return result


def persist(connection: Any, subject_id: str, guilds: list[str],
            mappings: dict[str, tuple[str, str]], result: dict[str, Any]) -> bool:
    with connection.cursor() as cursor:
        if result["status"] == "FOUND":
            raw_guild = str(result["guild"])
            guild_name, country = mappings[raw_guild]
            cursor.execute("""
              INSERT INTO fan_subject_identities(platform,subject_id,guild_id,guild_name,joined_guild_date,newcomer_revision)
              VALUES('LINKY',%s,%s,%s,%s::date,1)
              ON CONFLICT(platform,subject_id) DO UPDATE SET
                guild_id=EXCLUDED.guild_id,guild_name=EXCLUDED.guild_name,
                joined_guild_date=EXCLUDED.joined_guild_date,updated_at=now()
              WHERE fan_subject_identities.guild_id IS DISTINCT FROM EXCLUDED.guild_id
                OR fan_subject_identities.guild_name IS DISTINCT FROM EXCLUDED.guild_name
                OR fan_subject_identities.joined_guild_date IS DISTINCT FROM EXCLUDED.joined_guild_date""",
              (subject_id, raw_guild, guild_name, result["date"]))
            cursor.execute("""
              WITH changed AS (
                UPDATE fan_direct_ownerships
                SET country=%s,guild_name=%s,
                    source_snapshot=coalesce(source_snapshot,'{}'::jsonb) || jsonb_build_object(
                      'formalGuildLookup',jsonb_build_object(
                        'source','LINKY_OFFICIAL_ALL_GUILD_SEARCH','rawGuild',%s::text,
                        'guildName',%s::text,'country',%s::text,'joinedGuildDate',%s::text,
                        'checkedAt',%s::text,'sourceChecksum',%s::text))
                WHERE platform='LINKY' AND subject_id=%s AND ended_at IS NULL
                  AND (country IS DISTINCT FROM %s OR guild_name IS DISTINCT FROM %s)
                RETURNING id,platform,subject_id,agent_id,country,guild_name
              )
              INSERT INTO fan_direct_audit_events(platform,subject_id,event_type,payload_json)
              SELECT platform,subject_id,'SOURCE_BACKFILLED',jsonb_build_object(
                'ownershipId',id,'agentId',agent_id,'country',country,'guildName',guild_name,
                'sourceAction','ALL_GUILD_PROFILE_SYNCED','sourceRawGuild',%s::text,
                'sourceChecksum',%s::text)
              FROM changed""",
              (country, guild_name, raw_guild, guild_name, country, result["date"],
               result["checked"], result["checksum"], subject_id, country, guild_name,
               raw_guild, result["checksum"]))
        cursor.execute("""
          INSERT INTO fan_subject_join_date_lookup_state(
            platform,subject_id,status,source_scope,source_guild_id,source_snapshot_at,
            source_generation,source_checksum,error_code,last_checked_at)
          VALUES('LINKY',%s,%s,'linky_official_all_guild_search',%s,%s::timestamptz,%s,%s,%s,now())
          ON CONFLICT(platform,subject_id) DO UPDATE SET status=EXCLUDED.status,
            source_scope=EXCLUDED.source_scope,source_guild_id=EXCLUDED.source_guild_id,
            source_snapshot_at=EXCLUDED.source_snapshot_at,source_generation=EXCLUDED.source_generation,
            source_checksum=EXCLUDED.source_checksum,error_code=EXCLUDED.error_code,
            last_checked_at=now(),updated_at=now()""",
          (subject_id, result["status"], result.get("guild") or ",".join(guilds), result["checked"],
           "linky-search-all-guild-v2", result["checksum"], result["error"]))
    connection.commit()
    return result["status"] == "FOUND"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=int(os.getenv("LINKY_JOIN_DATE_LIMIT", "20")))
    parser.add_argument("--tokens", type=Path, default=Path(os.getenv("LINKE_GUILD_TOKENS", str(TOKENS))))
    parser.add_argument("--subject-id", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.limit <= 2000:
        raise SystemExit("limit must be 1..2000")
    safe_limit = int(os.getenv("LINKY_JOIN_DATE_ALL_GUILD_SAFE_LIMIT", "20"))
    if not 1 <= safe_limit <= 2000:
        raise SystemExit("LINKY_JOIN_DATE_ALL_GUILD_SAFE_LIMIT must be 1..2000")
    database_url = os.environ["DATABASE_URL"]
    import psycopg2
    configured = set(json.loads(args.tokens.read_text(encoding="utf-8"))["guilds"])
    guilds = sorted(configured)
    summary = {"requested": 0, "found": 0, "notFound": 0, "sourceStale": 0, "errors": 0, "unmapped": 0}
    with psycopg2.connect(database_url) as connection:
        mappings = guild_mappings(connection, configured)
        for subject_id in candidates(connection, min(args.limit, safe_limit), args.subject_id):
            summary["requested"] += 1
            result = lookup(subject_id, guilds, args.tokens)
            persist(connection, subject_id, guilds, mappings, result)
            key = {"FOUND": "found", "NOT_FOUND": "notFound", "SOURCE_STALE": "sourceStale", "ERROR": "errors"}[result["status"]]
            summary[key] += 1
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0 if not summary["sourceStale"] and not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
