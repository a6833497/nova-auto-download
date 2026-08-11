#!/usr/bin/env python3
"""Single production runner for Linky collection and existing consumers."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from linky_consumers import write_daily_ledger, write_live_views
from linky_fetch import BatchDeadlineExceeded, FetchBundle, FetchScanError, fetch_guild_day, new_request_scope
from linky_api_pagination import configured_page_size
from linky_runtime import atomic_json, database_url_from_environment


DEFAULT_LOCK = "/tmp/linky-collection.lock"
DEFAULT_STATE = "/home/ubuntu/nova-auto-download/state"
DEFAULT_TOKENS = "/home/ubuntu/.config/nova/linky-guild-tokens.json"
CONSISTENCY_RESCAN_DELAY_SECONDS = 2.0
CONSISTENCY_DRIFT_MARKERS = (
    "duplicate raw SID",
    "response total_item changed",
    "mutable pagination drift",
)
BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def valid_batch_id(value: str) -> bool:
    return bool(BATCH_ID_PATTERN.fullmatch(value))


def observation_defaults(job_name: str, batch_id: str, lock_result: str) -> dict[str, Any]:
    return {"jobName": job_name, "batchId": batch_id, "businessDate": None,
        "sourceGuild": None, "endpoint": None, "pageCount": 0, "rawRowCount": 0,
        "positiveRowCount": 0, "requestCount": 0, "retryCount": 0,
        "apiElapsedSeconds": 0.0, "writeElapsedSeconds": 0.0,
        "scanComplete": False, "lockResult": lock_result, "bundleReused": False}


def emit(record: dict[str, Any], sink: Callable[[str], None] = print) -> None:
    sink(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def closure_path(state_root: Path, business_date: str, guild: str) -> Path:
    return state_root / "linky-api-closure" / f"{business_date}-{guild}.json"


def current_cache_path(state_root: Path, business_date: str, guild: str) -> Path:
    identity = hashlib.sha256(guild.encode()).hexdigest()[:16]
    return state_root / "linky-current-cache" / f"{business_date}-{identity}.json"


def load_current_cache(state_root: Path, business_date: str, guild: str) -> dict[str, tuple[dict[str, Any], ...]]:
    try:
        value = json.loads(current_cache_path(state_root, business_date, guild).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if value.get("schemaVersion") != 1 or value.get("businessDate") != business_date or value.get("sourceGuild") != guild:
        return {}
    endpoints = value.get("endpoints")
    if not isinstance(endpoints, dict):
        return {}
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for endpoint in ("/api/guild/streamer_stat", "/api/guild/live_room_stat"):
        rows = endpoints.get(endpoint)
        if isinstance(rows, list) and all(isinstance(row, dict) and str(row.get("sid") or "").strip() for row in rows):
            result[endpoint] = tuple(rows)
    return result


def write_current_cache(state_root: Path, business_date: str, guild: str,
                        rows_by_endpoint: dict[str, tuple[dict[str, Any], ...]], status: str) -> None:
    if not rows_by_endpoint:
        return
    atomic_json(current_cache_path(state_root, business_date, guild), {
        "schemaVersion": 1, "businessDate": business_date, "sourceGuild": guild,
        "status": status, "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "endpoints": {endpoint: list(rows) for endpoint, rows in rows_by_endpoint.items()},
    })


def closure_complete(state_root: Path, business_date: str, guild: str) -> bool:
    path = closure_path(state_root, business_date, guild)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    scans = (value.get("streamer") or {}, value.get("voiceRoom") or {})
    scans_complete = all(scan.get("scanComplete") is True and
        scan.get("rawRowCount") == scan.get("reportedTotal") for scan in scans)
    return value.get("schemaVersion") == 1 and value.get("scanComplete") is True and scans_complete and \
        value.get("businessDate") == business_date and value.get("sourceGuild") == guild and \
        value.get("status") == "API_CLOSED"


def write_closure(state_root: Path, bundle: FetchBundle, batch_id: str) -> None:
    business_date = dt.datetime.strptime(bundle.business_date, "%Y%m%d").date().isoformat()
    atomic_json(closure_path(state_root, business_date, bundle.source_guild), {
        "schemaVersion": 1, "status": "API_CLOSED", "scanComplete": True,
        "businessDate": business_date, "sourceGuild": bundle.source_guild,
        "batchId": batch_id, "completedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "streamer": bundle.streamer_scan.observation(),
        "voiceRoom": bundle.voice_room_scan.observation(),
    })


def rows_checksum(rows: tuple[dict[str, Any], ...], value_key: str) -> str:
    normalized = sorted((str(row.get("sid") or ""), str(row.get(value_key) or 0)) for row in rows)
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def write_fetch_evidence(evidence_dir: Path, bundle: FetchBundle, batch_id: str) -> None:
    atomic_json(evidence_dir / f"{bundle.business_date}-{bundle.source_guild}-{batch_id}.json", {
        "schemaVersion": 2, "batchId": batch_id, "sourceGuild": bundle.source_guild,
        "businessDateUtc": bundle.business_date, "scanComplete": bundle.scan_complete,
        "streamer": {**bundle.streamer_scan.observation(),
            "checksum": rows_checksum(bundle.streamer_rows, "total_earns")},
        "voiceRoom": {**bundle.voice_room_scan.observation(),
            "amount": sum(float(row.get("receive_diamonds") or 0) for row in bundle.voice_room_rows),
            "checksum": rows_checksum(bundle.voice_room_rows, "receive_diamonds")},
    })


def prune_fetch_evidence(evidence_dir: Path, retention_days: int, utc_today: dt.date) -> None:
    cutoff = utc_today - dt.timedelta(days=retention_days)
    if not evidence_dir.is_dir():
        return
    for candidate in evidence_dir.glob("????????-*.json"):
        try:
            if dt.datetime.strptime(candidate.name[:8], "%Y%m%d").date() < cutoff:
                candidate.unlink()
        except ValueError:
            continue


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


def fetch_with_consistency_rescan(fetcher: Callable[..., FetchBundle], guild: str, day: str, *,
                                  utc_today: dt.date, tokens_path: str | None,
                                  deadline_monotonic: float, page_size: int | None,
                                  mutable_seed_rows_by_endpoint: dict[str, tuple[dict[str, Any], ...]] | None = None,
                                  sleeper: Callable[[float], None] = time.sleep) -> tuple[FetchBundle, int]:
    """Retry one complete snapshot only for known cross-page consistency drift."""
    seeds = mutable_seed_rows_by_endpoint or {}
    for attempt in range(2):
        scope = new_request_scope()
        try:
            options = {"request_scope": scope, "utc_today": utc_today,
                "tokens_path": tokens_path, "deadline_monotonic": deadline_monotonic,
                "mutable_seed_rows_by_endpoint": seeds}
            if page_size is not None:
                options["page_size"] = page_size
            return fetcher(guild, day, **options), attempt
        except FetchScanError as error:
            retryable = any(marker in str(error) for marker in CONSISTENCY_DRIFT_MARKERS)
            enough_time = time.monotonic() + CONSISTENCY_RESCAN_DELAY_SECONDS < deadline_monotonic
            if attempt or not retryable or not enough_time:
                raise
            next_seeds = error.cache_rows_by_endpoint
            try:
                detail = Decimal(str(error.observation.get("detailAmount")))
                summary = Decimal(str(error.observation.get("totalItemAmount")))
                if detail > summary:
                    next_seeds = {}
            except (InvalidOperation, TypeError, ValueError):
                pass
            seeds = next_seeds
            sleeper(CONSISTENCY_RESCAN_DELAY_SECONDS)
        finally:
            scope.clear()
    raise AssertionError("unreachable")


def process_bundle(bundle: FetchBundle, *, job_name: str, batch_id: str,
                   database_url: str | None, write_live: bool, write_ledger: bool,
                   dry_run: bool, snapshot_slot: dt.datetime, state_root: Path,
                   mark_closed: bool, evidence_dir: Path,
                   sink: Callable[[str], None] = print) -> None:
    if not bundle.scan_complete:
        raise ValueError("runner rejected incomplete Linky bundle")
    if not dry_run:
        write_fetch_evidence(evidence_dir, bundle, batch_id)
    write_started = time.monotonic()
    ledger_rows = live_rows = 0
    if not dry_run:
        if not database_url:
            raise RuntimeError("DATABASE_URL is required unless --dry-run is used")
        import psycopg2
        # One transaction per guild keeps both current-day consumers consistent;
        # results are released before the next guild to bound memory and lock scope.
        with psycopg2.connect(database_url) as connection:
            if write_live:
                live_rows = write_live_views(connection, bundle, snapshot_slot)
            if write_ledger:
                ledger_rows = write_daily_ledger(connection, bundle)
        if mark_closed:
            write_closure(state_root, bundle, batch_id)
    write_elapsed = time.monotonic() - write_started
    reused = write_live and write_ledger
    for value in bundle.observations():
        emit({**observation_defaults(job_name, batch_id, "ACQUIRED"), **value,
            "writeElapsedSeconds": write_elapsed, "bundleReused": reused}, sink)
    emit({**observation_defaults(job_name, batch_id, "ACQUIRED"),
        "businessDate": bundle.business_date, "sourceGuild": bundle.source_guild,
        "endpoint": "database-consumers", "writeElapsedSeconds": write_elapsed,
        "scanComplete": True, "bundleReused": reused, "liveRows": live_rows,
        "ledgerRows": ledger_rows, "dryRun": dry_run, "closedStateWritten": mark_closed and not dry_run}, sink)


def run_cycle(*, job_name: str, mode: str, guilds: list[str], utc_today: dt.date,
              database_url: str | None, state_root: Path, dry_run: bool,
              target_date: str | None = None, force: bool = False,
              target_write_live: bool = False, target_write_ledger: bool = True,
              tokens_path: str | None = None,
              evidence_dir: Path | None = None, evidence_retention_days: int = 14,
              max_batch_seconds: int = 3300,
              page_size: int | None = None,
              fetcher: Callable[..., FetchBundle] = fetch_guild_day,
              sink: Callable[[str], None] = print, batch_id: str | None = None) -> list[dict[str, Any]]:
    batch = batch_id or f"{utc_today.isoformat()}-{uuid.uuid4().hex[:12]}"
    snapshot_slot = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    def record_sink(line: str) -> None:
        sink(line)
        observations.append(json.loads(line))
    today_ymd = utc_today.strftime("%Y%m%d")
    yesterday = utc_today - dt.timedelta(days=1)
    yesterday_ymd = yesterday.strftime("%Y%m%d")
    archive = evidence_dir or state_root / "linky-ledger-evidence"
    deadline = time.monotonic() + max_batch_seconds
    deadline_reached = False
    for guild in guilds:
        if time.monotonic() >= deadline:
            results.append({"sourceGuild": guild, "businessDate": today_ymd,
                "status": "STOPPED_BATCH_DEADLINE"})
            emit({**observation_defaults(job_name, batch, "ACQUIRED"), "businessDate": today_ymd,
                "sourceGuild": guild, "endpoint": "batch-deadline", "errorType": "BatchDeadlineExceeded",
                "error": "batch stopped before next guild"}, record_sink)
            break
        jobs: list[tuple[str, bool, bool, bool]] = []
        if mode == "hourly":
            jobs.append((today_ymd, True, True, False))
            if not closure_complete(state_root, yesterday.isoformat(), guild):
                jobs.append((yesterday_ymd, False, True, True))
            else:
                results.append({"sourceGuild": guild, "businessDate": yesterday_ymd,
                    "status": "SKIPPED_API_CLOSED"})
                emit({**observation_defaults(job_name, batch, "ACQUIRED"),
                    "businessDate": yesterday.isoformat(), "sourceGuild": guild,
                    "endpoint": "api-closure-state", "scanComplete": True,
                    "bundleReused": True, "status": "SKIPPED_API_CLOSED"}, record_sink)
        elif mode == "close-yesterday":
            if not closure_complete(state_root, yesterday.isoformat(), guild):
                jobs.append((yesterday_ymd, False, True, True))
            else:
                results.append({"sourceGuild": guild, "businessDate": yesterday_ymd,
                    "status": "SKIPPED_API_CLOSED"})
                emit({**observation_defaults(job_name, batch, "ACQUIRED"),
                    "businessDate": yesterday.isoformat(), "sourceGuild": guild,
                    "endpoint": "api-closure-state", "scanComplete": True,
                    "bundleReused": True, "status": "SKIPPED_API_CLOSED"}, record_sink)
        elif mode == "target":
            if not target_date:
                raise ValueError("target mode requires a business date")
            target = dt.datetime.strptime(target_date, "%Y%m%d").date()
            mark_closed = target < utc_today
            if force or dry_run or not mark_closed or not closure_complete(state_root, target.isoformat(), guild):
                jobs.append((target_date, target_write_live, target_write_ledger,
                    mark_closed and target_write_ledger))
        else:
            raise ValueError(f"unsupported runner mode: {mode}")
        for day, write_live, write_ledger, mark_closed in jobs:
            try:
                current_cache = load_current_cache(state_root, day, guild) if day == today_ymd else {}
                bundle, consistency_rescans = fetch_with_consistency_rescan(fetcher, guild, day,
                    utc_today=utc_today, tokens_path=tokens_path,
                    deadline_monotonic=deadline, page_size=page_size,
                    mutable_seed_rows_by_endpoint=current_cache)
                if day == today_ymd and not dry_run:
                    write_current_cache(state_root, day, guild, {
                        "/api/guild/streamer_stat": bundle.streamer_rows,
                        "/api/guild/live_room_stat": bundle.voice_room_rows,
                    }, "COMPLETE")
                process_bundle(bundle, job_name=job_name, batch_id=batch, database_url=database_url,
                    write_live=write_live, write_ledger=write_ledger, dry_run=dry_run,
                    snapshot_slot=snapshot_slot, state_root=state_root, mark_closed=mark_closed,
                    evidence_dir=archive, sink=record_sink)
                results.append({"sourceGuild": guild, "businessDate": day, "status": "SUCCESS",
                    "consistencyRescanCount": consistency_rescans})
            except Exception as error:
                if day == today_ymd and not dry_run and isinstance(error, FetchScanError):
                    write_current_cache(state_root, day, guild,
                        error.cache_rows_by_endpoint, "ACCUMULATING")
                progress = error.observation if isinstance(error, FetchScanError) else {"endpoint": "batch"}
                emit({**observation_defaults(job_name, batch, "ACQUIRED"), "businessDate": day,
                    "sourceGuild": guild, **progress, "errorType": type(error).__name__,
                    "error": str(error)[:300]}, record_sink)
                results.append({"sourceGuild": guild, "businessDate": day, "status": "FAILED",
                    "errorType": type(error).__name__})
                cause = error.__cause__ if isinstance(error, FetchScanError) else error
                if isinstance(cause, BatchDeadlineExceeded):
                    deadline_reached = True
            if deadline_reached:
                break
        if deadline_reached:
            break
    if not dry_run:
        atomic_json(state_root / "linky-runs" / f"{batch}.json", {"schemaVersion": 1,
            "jobName": job_name, "batchId": batch, "mode": mode, "utcDate": utc_today.isoformat(),
            "scanComplete": all(item["status"] in {"SUCCESS", "SKIPPED_API_CLOSED"} for item in results),
            "results": results, "observations": observations})
        prune_fetch_evidence(archive, evidence_retention_days, utc_today)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--mode", choices=("hourly", "close-yesterday", "target"), required=True)
    parser.add_argument("--guild", action="append")
    parser.add_argument("--tokens", default=os.getenv("LINKE_GUILD_TOKENS", DEFAULT_TOKENS))
    parser.add_argument("--state-root", default=os.getenv("LINKE_STATE_ROOT", DEFAULT_STATE))
    parser.add_argument("--evidence-dir", default=os.getenv("LINKE_EVIDENCE_DIR",
        "/home/ubuntu/nova-auto-download/state/linky-ledger-evidence"))
    parser.add_argument("--evidence-retention-days", type=int,
        default=int(os.getenv("LINKE_EVIDENCE_RETENTION_DAYS", "14")))
    parser.add_argument("--lock-file", default=os.getenv("LINKE_COLLECTION_LOCK", DEFAULT_LOCK))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--utc-date", help="test/dry-run UTC date YYYY-MM-DD")
    parser.add_argument("--business-date", help="target UTC business date YYYYMMDD")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-batch-seconds", type=int,
        default=int(os.getenv("LINKE_MAX_BATCH_SECONDS", "3300")))
    parser.add_argument("--page-size", type=int,
        default=configured_page_size())
    parser.add_argument("--target-write-live", action="store_true")
    parser.add_argument("--target-no-ledger", action="store_true")
    args = parser.parse_args(argv)
    batch_id = args.batch_id or f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    if not valid_batch_id(batch_id):
        parser.error("batch id must be a safe opaque identifier")
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record = observation_defaults(args.job_name, batch_id, "SKIPPED_LOCK_BUSY")
            emit(record)
            if not args.dry_run:
                atomic_json(Path(args.state_root) / "linky-runs" / f"{batch_id}.json", {
                    "schemaVersion": 1, "jobName": args.job_name, "batchId": batch_id,
                    "scanComplete": False, "status": "SKIPPED_LOCK_BUSY", "observations": [record]})
            return 0
        today = dt.date.fromisoformat(args.utc_date) if args.utc_date else dt.datetime.now(dt.timezone.utc).date()
        database_url = database_url_from_environment()
        if not args.guild and not database_url:
            raise RuntimeError("DATABASE_URL is required to resolve active Linky source guilds")
        guilds = args.guild or load_guilds(Path(args.tokens), str(database_url), today)
        results = run_cycle(job_name=args.job_name, mode=args.mode, guilds=guilds, utc_today=today,
            database_url=database_url, state_root=Path(args.state_root),
            dry_run=args.dry_run, target_date=args.business_date, force=args.force,
            target_write_live=args.target_write_live, target_write_ledger=not args.target_no_ledger,
            tokens_path=args.tokens, evidence_dir=Path(args.evidence_dir),
            evidence_retention_days=args.evidence_retention_days,
            max_batch_seconds=args.max_batch_seconds, page_size=args.page_size, batch_id=batch_id)
        accepted = {"SUCCESS", "SKIPPED_API_CLOSED"}
        return 0 if results and all(item["status"] in accepted for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
