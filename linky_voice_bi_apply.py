#!/usr/bin/env python3
"""Apply verified Linky voice-room BI differences to the existing daily ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from linky_runtime import atomic_json, database_url_from_environment

LOCK_PATH = Path("/tmp/nova-data-write.lock")
SCHEMA_VERSION = 1


def decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def canonical_checksum(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_evidence(evidence_dir: Path, day: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for candidate in sorted(evidence_dir.glob(f"{day}-*.json")):
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != 2 or payload.get("scanComplete") is not True:
            raise ValueError(f"invalid voice evidence: {candidate}")
        if payload.get("businessDate") != day or payload.get("status") not in {"WAITING_BI", "BI_VERIFIED", "BI_MISMATCH"}:
            raise ValueError(f"voice evidence is not final: {candidate}")
        sources = payload.get("sourceGuilds")
        changed = payload.get("changedItems")
        if not isinstance(sources, list) or not sources or len(sources) != len(set(map(str, sources))):
            raise ValueError(f"invalid source guild scope: {candidate}")
        if payload.get("status") == "WAITING_BI":
            payload["changedItems"] = []
            payload["_path"] = str(candidate)
            payload["_checksum"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            evidence.append(payload)
            continue
        if not isinstance(changed, list) or int(payload.get("changedSidCount", -1)) != len(changed):
            raise ValueError(f"incomplete changed items: {candidate}")
        if decimal(payload.get("biAmount")) - decimal(payload.get("ledgerAmount")) != decimal(payload.get("amountDelta")):
            raise ValueError(f"invalid amount delta: {candidate}")
        raw = candidate.read_bytes()
        payload["_path"] = str(candidate)
        payload["_checksum"] = hashlib.sha256(raw).hexdigest()
        evidence.append(payload)
    if not evidence:
        raise ValueError(f"no final voice evidence for {day}")
    return evidence


def targets_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: dict[tuple[str, str], dict[str, Any]] = {}
    for item in evidence:
        day = str(item["businessDate"])
        allowed = set(map(str, item["sourceGuilds"]))
        for change in item["changedItems"]:
            identity = str(change.get("identity") or "")
            if "|" not in identity:
                raise ValueError(f"invalid voice identity: {identity}")
            guild, sid = identity.split("|", 1)
            if guild not in allowed or not sid.isdigit():
                raise ValueError(f"voice identity outside evidence scope: {identity}")
            before, after = decimal(change.get("ledger")), decimal(change.get("bi"))
            if after < 0 or after - before != decimal(change.get("delta")):
                raise ValueError(f"invalid voice amount: {identity}")
            key = (day, sid)
            target = {"date": day, "guild": guild, "sid": sid, "before": before, "after": after}
            previous = targets.get(key)
            if previous and (previous["guild"] != guild or previous["after"] != after):
                raise ValueError(f"one SID/date maps to multiple BI voice facts: {day}|{sid}")
            targets[key] = target
    return sorted(targets.values(), key=lambda row: (row["date"], row["guild"], int(row["sid"])))


def fetch_rows(cursor: Any, day: str, sids: list[str]) -> dict[str, dict[str, Any]]:
    if not sids:
        return {}
    cursor.execute("""SELECT sid::text,guild,COALESCE(room_diamonds,0)::text,settled,fetched_at
        FROM linke_streamer_daily WHERE stat_date=%s AND sid::text=ANY(%s)""", (day, sids))
    return {sid: {"sid": sid, "guild": guild, "roomDiamonds": amount,
        "settled": bool(settled), "fetchedAt": fetched_at.isoformat() if fetched_at else None}
        for sid, guild, amount, settled, fetched_at in cursor.fetchall()}


def build_plan(cursor: Any, day: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    targets = targets_from_evidence(evidence)
    current = fetch_rows(cursor, day, [row["sid"] for row in targets])
    updates, inserts = [], []
    for target in targets:
        row = current.get(target["sid"])
        if row:
            if row["guild"] != target["guild"]:
                raise ValueError(f"existing SID/date belongs to another guild: {day}|{target['sid']}")
            if decimal(row["roomDiamonds"]) != target["before"]:
                raise ValueError(f"stale voice evidence: {day}|{target['guild']}|{target['sid']}")
            updates.append({**target, "existed": True, "oldSettled": row["settled"], "oldFetchedAt": row["fetchedAt"]})
        else:
            if target["before"] != 0:
                raise ValueError(f"missing non-zero ledger row: {day}|{target['guild']}|{target['sid']}")
            inserts.append({**target, "existed": False})
    serializable = [{**row, "before": str(row["before"]), "after": str(row["after"])}
                    for row in updates + inserts]
    return {"schemaVersion": SCHEMA_VERSION, "businessDate": day,
        "evidence": [{"path": item["_path"], "checksum": item["_checksum"],
            "formalGuild": item["formalGuild"], "sourceGuilds": item["sourceGuilds"],
            "ledgerAmount": item.get("ledgerAmount"), "biAmount": item.get("biAmount")} for item in evidence],
        "updateCount": len(updates), "insertCount": len(inserts), "changes": serializable,
        "checksum": canonical_checksum(serializable)}


def apply_plan(connection: Any, plan: dict[str, Any], snapshot_dir: Path) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot = snapshot_dir / f"{plan['businessDate']}-{stamp}.json"
    if snapshot.exists():
        raise ValueError(f"snapshot already exists: {snapshot}")
    material = {**plan, "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "PREPARED"}
    atomic_json(snapshot, material)
    if len(json.loads(snapshot.read_text(encoding="utf-8"))["changes"]) != len(plan["changes"]):
        raise ValueError("snapshot row count validation failed")
    cursor = connection.cursor()
    for row in plan["changes"]:
        if row["existed"]:
            cursor.execute("""UPDATE linke_streamer_daily SET room_diamonds=%s,settled=TRUE
                WHERE stat_date=%s AND sid::text=%s AND guild=%s
                  AND COALESCE(room_diamonds,0)=%s RETURNING sid""",
                (row["after"], row["date"], row["sid"], row["guild"], row["before"]))
        else:
            cursor.execute("""INSERT INTO linke_streamer_daily(guild,sid,stat_date,room_diamonds,settled)
                VALUES(%s,%s,%s,%s,TRUE) ON CONFLICT DO NOTHING RETURNING sid""",
                (row["guild"], row["sid"], row["date"], row["after"]))
        if cursor.rowcount != 1:
            raise ValueError(f"guarded write changed {cursor.rowcount} rows: {row['date']}|{row['sid']}")
    for item in plan["evidence"]:
        if item.get("biAmount") is None:
            continue
        cursor.execute("""SELECT COALESCE(SUM(room_diamonds),0)::text FROM linke_streamer_daily
            WHERE stat_date=%s AND guild=ANY(%s)""",
            (plan["businessDate"], item["sourceGuilds"]))
        total = decimal(cursor.fetchone()[0])
        if total != decimal(item["biAmount"]):
            raise ValueError(f"post-write BI total mismatch: {item['formalGuild']}")
    connection.commit()
    material["status"] = "APPLIED"
    atomic_json(snapshot, material)
    return snapshot


def rollback(connection: Any, snapshot: Path) -> int:
    material = json.loads(snapshot.read_text(encoding="utf-8"))
    changes = material.get("changes")
    if material.get("schemaVersion") != SCHEMA_VERSION or not isinstance(changes, list):
        raise ValueError("invalid rollback snapshot")
    if canonical_checksum(changes) != material.get("checksum"):
        raise ValueError("rollback snapshot checksum mismatch")
    cursor = connection.cursor()
    for row in reversed(changes):
        if row["existed"]:
            cursor.execute("""UPDATE linke_streamer_daily SET room_diamonds=%s,settled=%s
                WHERE stat_date=%s AND sid::text=%s AND guild=%s
                  AND COALESCE(room_diamonds,0)=%s RETURNING sid""",
                (row["before"], row["oldSettled"], row["date"], row["sid"], row["guild"], row["after"]))
        else:
            cursor.execute("""DELETE FROM linke_streamer_daily WHERE stat_date=%s AND sid::text=%s
                AND guild=%s AND COALESCE(room_diamonds,0)=%s RETURNING sid""",
                (row["date"], row["sid"], row["guild"], row["after"]))
        if cursor.rowcount != 1:
            raise ValueError(f"guarded rollback changed {cursor.rowcount} rows: {row['date']}|{row['sid']}")
    return len(changes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--evidence-dir", default="/home/ubuntu/nova-auto-download/state/linky-voice-audit")
    parser.add_argument("--snapshot-dir", default="/home/ubuntu/nova-auto-download/state/linky-voice-repair")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-changes", type=int, default=5000)
    parser.add_argument("--rollback-snapshot")
    args = parser.parse_args()
    if bool(args.rollback_snapshot) == bool(args.date):
        raise SystemExit("provide exactly one of --date or --rollback-snapshot")
    database_url = database_url_from_environment()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    import psycopg2
    LOCK_PATH.touch(mode=0o600, exist_ok=True)
    with LOCK_PATH.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("data write lock is busy") from error
        with psycopg2.connect(database_url) as connection:
            connection.set_session(isolation_level="SERIALIZABLE", readonly=not args.apply)
            if args.rollback_snapshot:
                if not args.apply:
                    raise SystemExit("rollback requires --apply")
                count = rollback(connection, Path(args.rollback_snapshot))
                print(json.dumps({"mode": "rollback", "rows": count}, ensure_ascii=False))
                return 0
            evidence = load_evidence(Path(args.evidence_dir), args.date)
            with connection.cursor() as cursor:
                plan = build_plan(cursor, args.date, evidence)
            count = len(plan["changes"])
            if count > args.max_changes:
                raise ValueError(f"planned changes exceed limit: {count}>{args.max_changes}")
            if not args.apply or count == 0:
                print(json.dumps({"mode": "dry-run", "businessDate": args.date,
                    "updates": plan["updateCount"], "inserts": plan["insertCount"],
                    "changes": count, "checksum": plan["checksum"]}, ensure_ascii=False))
                return 0
            snapshot = apply_plan(connection, plan, Path(args.snapshot_dir))
            print(json.dumps({"mode": "apply", "businessDate": args.date,
                "updates": plan["updateCount"], "inserts": plan["insertCount"],
                "changes": count, "snapshot": str(snapshot), "checksum": plan["checksum"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
