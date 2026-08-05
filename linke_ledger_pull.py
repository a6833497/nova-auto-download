#!/usr/bin/env python3
"""Compatibility wrapper for explicit Linky guild/day ledger collection."""

from __future__ import annotations

import datetime as dt
import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from linky_runtime import atomic_json, database_url_from_environment
from linky_sync_runner import main as runner_main


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
    normalized = sorted((str(row.get("sid") or ""), str(row.get(value_key) or 0)) for row in rows)
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def evidence_filename(day: str, guild: str, detected_at: dt.datetime, today: dt.date | None = None) -> str:
    business_date = dt.datetime.strptime(day, "%Y%m%d").date()
    if business_date < (today or dt.datetime.now(dt.timezone.utc).date()):
        stamp = detected_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{day}-{guild}-{stamp}.json"
    return f"{day}-{guild}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("guild")
    parser.add_argument("business_date")
    parser.add_argument("--tokens")
    parser.add_argument("--evidence-dir")
    parser.add_argument("--evidence-retention-days", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args(argv if argv is not None else sys.argv[1:])
    args = ["--job-name", "linky-ledger-compat", "--mode", "target",
        "--guild", options.guild, "--business-date", options.business_date]
    if options.tokens:
        args.extend(["--tokens", options.tokens])
    if options.evidence_dir:
        args.extend(["--evidence-dir", options.evidence_dir])
    if options.evidence_retention_days is not None:
        args.extend(["--evidence-retention-days", str(options.evidence_retention_days)])
    if options.dry_run:
        args.append("--dry-run")
    if options.force:
        args.append("--force")
    return runner_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
