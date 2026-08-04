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


def voice_rows(payload: dict[str, Any], day: str) -> dict[str, float]:
    ymd = day.replace("-", "")
    result: dict[str, float] = {}
    for row in payload.get("rows") or []:
        if str(row.get("active_date(day)")) != ymd:
            continue
        sid = str(row.get("sid") or "").strip()
        if not sid:
            raise ValueError("BI voice row has no SID")
        if sid in result:
            raise ValueError(f"duplicate BI voice SID: {sid}")
        result[sid] = float(row.get("diamond_amount") or 0)
    return result


def reconcile(ledger: dict[str, float], bi: dict[str, float]) -> dict[str, Any]:
    ids = sorted(set(ledger) | set(bi))
    changed = [{"sid": sid, "ledger": ledger.get(sid, 0), "bi": bi.get(sid, 0),
                "delta": bi.get(sid, 0) - ledger.get(sid, 0)}
               for sid in ids if ledger.get(sid, 0) != bi.get(sid, 0)]
    return {
        "status": "MATCH" if not changed else "MISMATCH",
        "ledgerSidCount": len(ledger), "biSidCount": len(bi), "changedSidCount": len(changed),
        "ledgerAmount": sum(ledger.values()), "biAmount": sum(bi.values()),
        "amountDelta": sum(bi.values()) - sum(ledger.values()),
        "changedItems": changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="UTC business date YYYY-MM-DD")
    parser.add_argument("--guild", required=True, help="Realtime ledger guild, e.g. Nova-Indonesia")
    parser.add_argument("--bi-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.bi_file).read_text(encoding="utf-8"))
    bi = voice_rows(payload, args.date)
    if not bi:
        result = {"schemaVersion": 1, "status": "UNAVAILABLE", "businessDateUtc": args.date,
                  "guild": args.guild, "reason": "BI report contains no rows for the requested business date"}
    else:
        database_url = database_url_from_environment()
        if not database_url:
            raise SystemExit("DATABASE_URL is required")
        import psycopg2
        with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT sid::text,room_diamonds::float8 FROM linke_streamer_daily WHERE guild=%s AND stat_date=%s", (args.guild, args.date))
            ledger = {sid: float(amount or 0) for sid, amount in cursor.fetchall()}
        result = {"schemaVersion": 1, "businessDateUtc": args.date, "guild": args.guild,
                  "detectedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "biFileChecksum": hashlib.sha256(Path(args.bi_file).read_bytes()).hexdigest(),
                  **reconcile(ledger, bi)}
    atomic_json(Path(args.output), result)
    print(json.dumps({k: v for k, v in result.items() if k != "changedItems"}, ensure_ascii=False))
    return 0 if result["status"] == "MATCH" else 2


if __name__ == "__main__":
    raise SystemExit(main())
