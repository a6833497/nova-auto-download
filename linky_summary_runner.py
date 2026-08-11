#!/usr/bin/env python3
"""Publish a lightweight, fail-closed Linky guild summary every 15 minutes."""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Callable

from linky_fetch import _authenticated_call
from linky_runtime import atomic_json, database_url_from_environment
DEFAULT_STATE = "/home/ubuntu/nova-auto-download/state"
DEFAULT_TOKENS = "/home/ubuntu/.config/nova/linky-guild-tokens.json"
BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def valid_batch_id(value: str) -> bool:
    return bool(BATCH_ID_PATTERN.fullmatch(value))


def load_guilds(tokens_path: Path, database_url: str, business_date: dt.date) -> list[str]:
    value = json.loads(tokens_path.read_text(encoding="utf-8"))
    configured = set(value["guilds"].keys())
    import psycopg2
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT raw_guild FROM guild_source_dictionary
          WHERE active AND source_key='LINKY' AND effective_from<=%s
            AND (effective_to IS NULL OR effective_to>=%s)
          ORDER BY display_order,id""", (business_date, business_date))
        guilds = [str(row[0]) for row in cursor.fetchall() if str(row[0]) in configured]
    if not guilds:
        raise RuntimeError("no active Linky source guilds matched guild_source_dictionary")
    return list(dict.fromkeys(guilds))


SUMMARY_ENDPOINTS = (
    ("/api/guild/streamer_stat", "total_earns", "chatIncome"),
    ("/api/guild/live_room_stat", "receive_diamonds", "roomIncome"),
)


def decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RuntimeError("Linky guild summary amount is invalid") from error
    if not number.is_finite() or number < 0:
        raise RuntimeError("Linky guild summary amount is invalid")
    return str(number)


def fetch_guild_summary(guild: str, business_date: str, *,
                        call: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    api_call = call or _authenticated_call(guild)
    values: dict[str, Any] = {}
    endpoint_evidence = []
    for endpoint, value_key, output_key in SUMMARY_ENDPOINTS:
        path = f"{endpoint}?begin={business_date}&end={business_date}&page_num=1&page_size=1&type=0"
        payload = api_call(path)
        total_item = payload.get("total_item")
        if not isinstance(total_item, dict) or value_key not in total_item:
            raise RuntimeError(f"Linky guild summary has no {value_key}")
        values[output_key] = decimal_text(total_item[value_key])
        reported_total = payload.get("total")
        try:
            reported_total = int(reported_total)
        except (TypeError, ValueError):
            reported_total = None
        endpoint_evidence.append({"endpoint": endpoint, "reportedTotal": reported_total,
            "httpStatus": getattr(api_call, "last_http_status", None)})
    return {"sourceGuild": guild, "businessDate": business_date,
        "status": "PROVISIONAL", "freshness": "FRESH", **values,
        "observedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "endpoints": endpoint_evidence}


def publish_summary(state_root: Path, guilds: list[str], business_date: str, batch_id: str,
                    *, fetcher: Callable[[str, str], dict[str, Any]] = fetch_guild_summary) -> tuple[dict[str, Any], bool]:
    latest_path = state_root / "linky-guild-summary" / "latest.json"
    try:
        previous = json.loads(latest_path.read_text(encoding="utf-8"))
        previous_by_guild = {row["sourceGuild"]: row for row in previous.get("guilds", [])
            if isinstance(row, dict) and row.get("sourceGuild")}
    except (OSError, ValueError, TypeError):
        previous_by_guild = {}
    rows = []
    all_fresh = True
    for guild in guilds:
        try:
            rows.append(fetcher(guild, business_date))
        except Exception as error:
            all_fresh = False
            prior = previous_by_guild.get(guild)
            if prior and prior.get("businessDate") == business_date:
                rows.append({**prior, "status": "PROVISIONAL", "freshness": "STALE",
                    "lastAttemptAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "lastErrorType": type(error).__name__})
            else:
                rows.append({"sourceGuild": guild, "businessDate": business_date,
                    "status": "UNAVAILABLE", "freshness": "MISSING", "chatIncome": None,
                    "roomIncome": None, "observedAt": None,
                    "lastAttemptAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "lastErrorType": type(error).__name__})
    document = {"schemaVersion": 1, "batchId": batch_id, "businessDate": business_date,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "COMPLETE" if all_fresh else "PARTIAL", "guilds": rows}
    atomic_json(latest_path, document)
    atomic_json(state_root / "linky-guild-summary" / "runs" / f"{batch_id}.json", document)
    return document, all_fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id")
    parser.add_argument("--guild", action="append")
    parser.add_argument("--utc-date")
    parser.add_argument("--tokens", default=os.getenv("LINKE_GUILD_TOKENS", DEFAULT_TOKENS))
    parser.add_argument("--state-root", default=os.getenv("LINKE_STATE_ROOT", DEFAULT_STATE))
    parser.add_argument("--lock-file", default=os.getenv("LINKE_SUMMARY_LOCK", "/tmp/linky-summary.lock"))
    args = parser.parse_args(argv)
    batch_id = args.batch_id or f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-summary-{uuid.uuid4().hex[:8]}"
    if not valid_batch_id(batch_id):
        parser.error("batch id must be a safe opaque identifier")
    today = dt.date.fromisoformat(args.utc_date) if args.utc_date else dt.datetime.now(dt.timezone.utc).date()
    database_url = database_url_from_environment()
    guilds = args.guild or load_guilds(Path(args.tokens), str(database_url), today)
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        _, complete = publish_summary(Path(args.state_root), guilds, today.strftime("%Y%m%d"), batch_id,
            fetcher=lambda guild, day: fetch_guild_summary(guild, day,
                call=_authenticated_call(guild, args.tokens)))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
