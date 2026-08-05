#!/usr/bin/env python3
"""Read-only Linky voice-room BI versus realtime-ledger reconciliation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from linke_ledger_pull import atomic_json, database_url_from_environment

SCHEMA_VERSION = 2
ALLOWED_STATUS = {"WAITING_BI", "BI_VERIFIED", "BI_MISMATCH"}
VOICE_REPORT = "语音房主播行为数据"


def source_bi_name(source_guild: str) -> str:
    suffix = "-Indonesia"
    if not source_guild.endswith(suffix):
        raise ValueError(f"unsupported Linky voice source guild: {source_guild}")
    return source_guild[:-len(suffix)]


def normalized_checksum(rows: dict[str, float], include_amount: bool) -> str:
    value = sorted((key, amount if include_amount else None) for key, amount in rows.items())
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def validate_payload(payload: dict[str, Any], day: str, source_guilds: list[str]) -> tuple[dict[str, float], str]:
    meta = payload.get("meta") or {}
    if meta.get("tabName") != VOICE_REPORT:
        raise ValueError("BI report is not 语音房主播行为数据")
    headers = payload.get("headers") or []
    required = {"active_date(day)", "guild_name", "sid", "diamond_amount"}
    if not required.issubset(set(headers)):
        raise ValueError("BI voice report is missing required columns")
    rows = payload.get("rows")
    if not isinstance(rows, list) or int(meta.get("rowCount", -1)) != len(rows):
        raise ValueError("BI voice report is incomplete")
    ymd = day.replace("-", "")
    expected = {source_bi_name(source): source for source in source_guilds}
    result: dict[str, float] = {}
    present_guilds: set[str] = set()
    for row in rows:
        if str(row.get("active_date(day)")) != ymd:
            continue
        bi_guild = str(row.get("guild_name") or "").strip()
        if bi_guild not in expected:
            continue
        present_guilds.add(bi_guild)
        sid = str(row.get("sid") or "").strip()
        if not sid:
            raise ValueError("BI voice row has no SID")
        identity = f"{expected[bi_guild]}|{sid}"
        if identity in result:
            raise ValueError(f"duplicate BI voice identity: {identity}")
        result[identity] = float(row.get("diamond_amount") or 0)
    if not result:
        raise ValueError("BI report contains no rows for requested business date")
    if present_guilds != set(expected):
        raise ValueError("BI report does not contain every mapped source guild")
    return result, day


def read_ledger(database_url: str, source_guilds: list[str], day: str) -> dict[str, float]:
    import psycopg2
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT guild,sid::text,room_diamonds::float8 FROM linke_streamer_daily
            WHERE guild=ANY(%s) AND stat_date=%s""", (source_guilds, day))
        rows: dict[str, float] = {}
        for guild, sid, amount in cursor.fetchall():
            identity = f"{guild}|{sid}"
            if identity in rows:
                raise ValueError(f"duplicate ledger voice identity: {identity}")
            rows[identity] = float(amount or 0)
        return rows


def reconcile(ledger: dict[str, float], bi: dict[str, float]) -> dict[str, Any]:
    identities = sorted(set(ledger) | set(bi))
    changed = [{"identity": key, "ledger": ledger.get(key, 0), "bi": bi.get(key, 0),
                "delta": bi.get(key, 0) - ledger.get(key, 0)}
               for key in identities if ledger.get(key, 0) != bi.get(key, 0)]
    added = sum(1 for key in bi if key not in ledger)
    missing = sum(1 for key in ledger if key not in bi)
    amount_changed = sum(1 for key in set(ledger) & set(bi) if ledger[key] != bi[key])
    return {
        "status": "BI_VERIFIED" if not changed else "BI_MISMATCH",
        "ledgerSidCount": len(ledger), "biSidCount": len(bi), "changedSidCount": len(changed),
        "addedSidCount": added, "missingSidCount": missing, "amountChangedSidCount": amount_changed,
        "ledgerAmount": sum(ledger.values()), "biAmount": sum(bi.values()),
        "amountDelta": sum(bi.values()) - sum(ledger.values()), "changedItems": changed,
        "sidChecksum": hashlib.sha256(json.dumps({"ledger": normalized_checksum(ledger, False),
            "bi": normalized_checksum(bi, False)}, sort_keys=True).encode()).hexdigest(),
        "amountChecksum": hashlib.sha256(json.dumps({"ledger": normalized_checksum(ledger, True),
            "bi": normalized_checksum(bi, True)}, sort_keys=True).encode()).hexdigest(),
    }


def waiting_result(day: str, country: str, source_guilds: list[str], formal_guild: str,
                   reason_code: str, reason: str) -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "scanComplete": True, "status": "WAITING_BI",
        "businessDate": day, "country": country, "sourceGuilds": source_guilds,
        "formalGuild": formal_guild, "reasonCode": reason_code, "reason": reason,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat()}


def audit(day: str, country: str, source_guilds: list[str], formal_guild: str,
          bi_file: Path | None, database_url: str | None) -> dict[str, Any]:
    if bi_file is None or not bi_file.is_file():
        return waiting_result(day, country, source_guilds, formal_guild, "BI_FILE_MISSING", "BI file is unavailable")
    try:
        raw = bi_file.read_bytes()
        payload = json.loads(raw)
        bi, source_day = validate_payload(payload, day, source_guilds)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return waiting_result(day, country, source_guilds, formal_guild, "BI_FILE_INCOMPLETE", str(error))
    if not database_url:
        raise ValueError("DATABASE_URL is required for ledger reconciliation")
    ledger = read_ledger(database_url, source_guilds, day)
    return {"schemaVersion": SCHEMA_VERSION, "scanComplete": True, "businessDate": day,
        "country": country, "sourceGuilds": source_guilds, "formalGuild": formal_guild,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sourceFileChecksum": hashlib.sha256(raw).hexdigest(),
        "sourceFileBusinessDate": source_day, "reportName": payload["meta"].get("reportName"),
        **reconcile(ledger, bi)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="UTC business date YYYY-MM-DD")
    parser.add_argument("--country", required=True)
    parser.add_argument("--source-guild", action="append", required=True)
    parser.add_argument("--formal-guild", required=True)
    parser.add_argument("--bi-file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit(args.date, args.country, args.source_guild, args.formal_guild,
                   Path(args.bi_file) if args.bi_file else None, database_url_from_environment())
    atomic_json(Path(args.output), result)
    print(json.dumps({key: value for key, value in result.items() if key != "changedItems"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
