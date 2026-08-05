#!/usr/bin/env python3
"""Generate voice-room BI audit evidence from the existing staging tree and guild dictionary."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from linke_ledger_pull import atomic_json, database_url_from_environment
from linky_voice_bi_audit import audit, validate_payload, waiting_result, VOICE_REPORT


def load_mappings(database_url: str, day: str) -> list[dict[str, Any]]:
    import psycopg2
    with psycopg2.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT guild_alias,country,raw_guild FROM guild_source_dictionary
          WHERE active AND source_key='LINKY' AND raw_guild LIKE 'VOICE:%%'
            AND effective_from<=%s AND (effective_to IS NULL OR effective_to>=%s)
          ORDER BY display_order,id""", (day, day))
        groups: dict[tuple[str, str], list[str]] = {}
        for formal, country, raw in cursor.fetchall():
            groups.setdefault((str(formal), str(country)), []).append(str(raw).removeprefix("VOICE:"))
    return [{"formalGuild": formal, "country": country, "sourceGuilds": sources}
            for (formal, country), sources in groups.items()]


def choose_bi_file(staging_root: Path, day: str, source_guilds: list[str]) -> Path | None:
    candidates: list[tuple[str, Path]] = []
    for candidate in staging_root.glob("**/*_语音房主播行为数据.json"):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if (payload.get("meta") or {}).get("tabName") != VOICE_REPORT:
                continue
            validate_payload(payload, day, source_guilds)
            queried_at = str((payload.get("meta") or {}).get("queriedAt") or "")
            candidates.append((queried_at, candidate))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return max(candidates, default=("", None), key=lambda item: (item[0], str(item[1])))[1]


def run(day: str, staging_root: Path, evidence_dir: Path, database_url: str) -> list[dict[str, Any]]:
    summaries = []
    for mapping in load_mappings(database_url, day):
        sources = mapping["sourceGuilds"]
        output = evidence_dir / f"{day}-{sources[0]}.json"
        candidate = choose_bi_file(staging_root, day, sources)
        if candidate is None:
            result = waiting_result(day, mapping["country"], sources, mapping["formalGuild"],
                "BI_FILE_MISSING", "No complete 语音房主播行为数据 file contains the requested internal business date and guild mapping")
        else:
            result = audit(day, mapping["country"], sources, mapping["formalGuild"], candidate, database_url)
        atomic_json(output, result)
        summaries.append({"output": str(output), "sourceFile": str(candidate) if candidate else None,
            **{key: result.get(key) for key in ("businessDate", "formalGuild", "sourceGuilds", "status",
                "ledgerAmount", "biAmount", "amountDelta", "changedSidCount")}})
    return summaries


def prune_evidence(evidence_dir: Path, retention_days: int) -> None:
    cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=retention_days)
    if not evidence_dir.is_dir():
        return
    for candidate in evidence_dir.glob("????-??-??-*.json"):
        try:
            if dt.date.fromisoformat(candidate.name[:10]) < cutoff:
                candidate.unlink()
        except ValueError:
            continue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--staging-root", default="/home/ubuntu/nova-data/upload-staging/daily")
    parser.add_argument("--evidence-dir", default="/home/ubuntu/nova-auto-download/state/linky-voice-audit")
    args = parser.parse_args()
    database_url = database_url_from_environment()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    summaries = run(args.date, Path(args.staging_root), Path(args.evidence_dir), database_url)
    prune_evidence(Path(args.evidence_dir), int(os.getenv("LINKY_VOICE_AUDIT_RETENTION_DAYS", "30")))
    print(json.dumps(summaries, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
