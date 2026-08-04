#!/usr/bin/env python3
"""Pull one Linky guild/day with complete pagination and auditable evidence."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
import urllib.request


PAGE_SIZE = 500


def database_url_from_environment() -> str | None:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env_path = Path(os.getenv("NOVA_API_ENV", "/home/ubuntu/nova-backend-current/api/.env"))
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def pull_pages(call: Callable[[str], dict[str, Any]], path: str, day: str, value_key: str,
               page_size: int = PAGE_SIZE, max_pages: int = 120) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every raw page; zero-value rows must never terminate pagination."""
    positive: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        query = f"{path}?begin={day}&end={day}&page_num={page}&page_size={page_size}&type=0"
        payload = call(query)
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Linky response items is not a list: page={page}")
        kept = [row for row in items if float(row.get(value_key) or 0) > 0]
        for row in kept:
            sid = str(row.get("sid") or "").strip()
            if not sid:
                raise RuntimeError(f"Linky positive row has no SID: page={page}")
            if sid in seen_ids:
                raise RuntimeError(f"Linky duplicate positive SID across pages: {sid}")
            seen_ids.add(sid)
        positive.extend(kept)
        total = payload.get("total")
        evidence.append({"page": page, "rawCount": len(items), "positiveCount": len(kept), "reportedTotal": total})
        if len(items) < page_size:
            break
        if isinstance(total, (int, float)) and page * page_size >= int(total):
            break
    else:
        raise RuntimeError(f"Linky pagination exceeded safety cap: {max_pages}")
    return positive, evidence


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def prune_evidence(directory: Path, retention_days: int, today: dt.date | None = None) -> list[str]:
    if retention_days < 1:
        raise ValueError("evidence retention must be at least one day")
    cutoff = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=retention_days)
    removed: list[str] = []
    if not directory.is_dir():
        return removed
    for candidate in directory.iterdir():
        prefix = candidate.name.split("-", 1)[0]
        if not candidate.is_file() or len(prefix) != 8 or not prefix.isdigit() or candidate.suffix != ".json":
            continue
        if dt.datetime.strptime(prefix, "%Y%m%d").date() < cutoff:
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def checksum(rows: list[dict[str, Any]], value_key: str) -> str:
    normalized = sorted((str(r.get("sid") or ""), str(r.get(value_key) or 0)) for r in rows)
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def write_ledger(database_url: str, guild: str, day: str, streamer: list[dict[str, Any]],
                 room: list[dict[str, Any]]) -> int:
    import psycopg2
    from psycopg2.extras import execute_values

    stat_date = dt.datetime.strptime(day, "%Y%m%d").date()
    settled = stat_date < dt.datetime.now(dt.timezone.utc).date()
    chat_by_sid = {int(row["sid"]): row for row in streamer}
    room_by_sid = {int(row["sid"]): row for row in room}
    values = []
    for sid in sorted(set(chat_by_sid) | set(room_by_sid)):
        chat = chat_by_sid.get(sid, {})
        voice = room_by_sid.get(sid, {})
        values.append((guild, sid, stat_date,
            chat.get("chat_earns") or 0, chat.get("voice_call_earns") or 0,
            chat.get("text_earns") or 0, chat.get("unlock_image_earns") or 0,
            chat.get("task_earns") or 0, chat.get("other_earns") or 0,
            voice.get("receive_diamonds") or 0, chat.get("online_time") or 0,
            float(voice.get("on_mic_time") or 0), voice.get("new_fans") or 0,
            chat.get("ten_minutes_reply_ratio") or 0, chat.get("new_level4_num") or 0, settled))
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM linke_streamer_daily WHERE stat_date=%s AND guild=%s AND settled=true LIMIT 1", (stat_date, guild))
            locked = cursor.fetchone() is not None and (dt.datetime.now(dt.timezone.utc).date() - stat_date).days > 2
            if locked:
                raise RuntimeError(f"historical ledger is write-protected: {guild} {stat_date}")
            execute_values(cursor, """INSERT INTO linke_streamer_daily
              (guild,sid,stat_date,chat_earns,voice_call_earns,text_earns,unlock_image_earns,task_earns,other_earns,
               room_diamonds,online_time,on_mic_time,new_fans,ten_min_reply,new_level4,settled) VALUES %s
              ON CONFLICT (sid,stat_date) DO UPDATE SET
               guild=EXCLUDED.guild,chat_earns=EXCLUDED.chat_earns,voice_call_earns=EXCLUDED.voice_call_earns,
               text_earns=EXCLUDED.text_earns,unlock_image_earns=EXCLUDED.unlock_image_earns,
               task_earns=EXCLUDED.task_earns,other_earns=EXCLUDED.other_earns,
               room_diamonds=EXCLUDED.room_diamonds,online_time=EXCLUDED.online_time,
               on_mic_time=EXCLUDED.on_mic_time,new_fans=EXCLUDED.new_fans,
               ten_min_reply=EXCLUDED.ten_min_reply,new_level4=EXCLUDED.new_level4,
               settled=EXCLUDED.settled,fetched_at=now()""", values)
    return len(values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("guild")
    parser.add_argument("day", help="UTC business date as YYYYMMDD")
    parser.add_argument("--tokens", default=os.getenv("LINKE_GUILD_TOKENS", "guild-tokens.json"))
    parser.add_argument("--evidence-dir", default=os.getenv("LINKE_EVIDENCE_DIR", "evidence/linky-ledger"))
    parser.add_argument("--evidence-retention-days", type=int, default=int(os.getenv("LINKE_EVIDENCE_RETENTION_DAYS", "14")))
    parser.add_argument("--dry-run", action="store_true", help="fetch and archive only; do not write the ledger")
    args = parser.parse_args()
    config = json.loads(Path(args.tokens).read_text(encoding="utf-8"))
    guild = config["guilds"][args.guild]

    raw_payloads: dict[str, list[dict[str, Any]]] = {}
    def call(path: str) -> dict[str, Any]:
        stamp = str(int(time.time() * 1000))
        signature = base64.b64encode(hmac.new(guild["oauth_token_secret"].encode(), (path + "&" + stamp).encode(), hashlib.sha1).digest()).decode()
        request = urllib.request.Request("https://api.linke.ai" + path, headers={
            "X-Auth-Token": guild["oauth_token"], "X-Auth-Timestamp": stamp,
            "X-Auth-Signature": signature, "X-App-Language": "en", "Country": "US",
        })
        payload = json.loads(urllib.request.urlopen(request, timeout=30).read())
        raw_payloads.setdefault(path.split("?")[0], []).append(payload)
        return payload

    streamer, streamer_pages = pull_pages(call, "/api/guild/streamer_stat", args.day, "total_earns")
    room, room_pages = pull_pages(call, "/api/guild/live_room_stat", args.day, "receive_diamonds")
    detected_at = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence = {
        "schemaVersion": 1, "guild": args.guild, "businessDateUtc": args.day,
        "detectedAt": detected_at, "scanComplete": True,
        "streamer": {"pages": streamer_pages, "positiveRows": len(streamer), "checksum": checksum(streamer, "total_earns")},
        "voiceRoom": {"pages": room_pages, "positiveRows": len(room), "amount": sum(float(r.get("receive_diamonds") or 0) for r in room),
                      "checksum": checksum(room, "receive_diamonds")},
        "rawResponses": raw_payloads,
    }
    out = Path(args.evidence_dir) / f"{args.day}-{args.guild}.json"
    atomic_json(out, evidence)
    prune_evidence(out.parent, args.evidence_retention_days)
    written = None
    if not args.dry_run:
        database_url = database_url_from_environment()
        if not database_url:
            raise SystemExit("DATABASE_URL is required unless --dry-run is used")
        written = write_ledger(database_url, args.guild, args.day, streamer, room)
    summary = {k: evidence[k] for k in ("schemaVersion", "guild", "businessDateUtc", "scanComplete", "voiceRoom")}
    summary["ledgerRowsWritten"] = written
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
