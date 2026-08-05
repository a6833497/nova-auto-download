import contextlib
import datetime as dt
import fcntl
import io
import json
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import linke_ledger_pull
import linke_live_pull
from linky_consumers import build_ledger_rows, build_live_rows, validate_complete_bundle
from linky_fetch import EndpointScan, FetchBundle
from linky_sync_runner import closure_complete, load_guilds, main, process_bundle, run_cycle, write_closure


def scan(endpoint="/api/guild/streamer_stat", complete=True, raw=2, total=2):
    return EndpointScan(endpoint=endpoint, page_count=1, raw_row_count=raw,
        positive_row_count=1, request_count=1, retry_count=0,
        api_elapsed_seconds=0.01, scan_complete=complete, reported_total=total,
        requested_page_size=500, unique_sid_count=raw, duplicate_sid_count=0,
        total_change_count=0, repeated_page_count=0, detail_amount="1",
        total_item_amount="1", canonical_sid_checksum="sid",
        canonical_amount_checksum="amount", final_page_row_count=raw,
        expected_final_page_row_count=raw,
        pages=({"page": 1, "rawCount": raw, "positiveCount": 1, "reportedTotal": total},))


def bundle(guild, day, complete=True):
    return FetchBundle(source_guild=guild, business_date=day,
        streamer_rows=({"sid": "1", "total_earns": 5, "chat_earns": 3, "nickname": "A"},),
        voice_room_rows=({"sid": "1", "receive_diamonds": 7},),
        streamer_scan=scan(complete=complete),
        voice_room_scan=scan("/api/guild/live_room_stat", complete=complete),
        online_anchor_sids=frozenset({1}), online_scan=None)


class LinkyRunnerTests(unittest.TestCase):
    def test_default_sources_are_active_dictionary_entries_with_configured_credentials(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, _sql, params): self.params = params
            def fetchall(self):
                return [("Nova-Indonesia",), ("VOICE:Nova-Indonesia",),
                    ("Hidden-No-Credential",), ("Carote2-Indonesia",)]
        class Connection:
            def __init__(self): self.value = Cursor()
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def cursor(self): return self.value
        fake = types.SimpleNamespace(connect=lambda _url: Connection())
        with TemporaryDirectory() as root, patch.dict(sys.modules, {"psycopg2": fake}):
            tokens = Path(root) / "tokens.json"
            tokens.write_text(json.dumps({"guilds": {
                "Nova-Indonesia": {}, "Carote2-Indonesia": {}, "Unused-Token": {}}}))
            self.assertEqual(["Nova-Indonesia", "Carote2-Indonesia"],
                load_guilds(tokens, "postgres://fake", dt.date(2026, 8, 5)))

    def test_consumers_reject_incomplete_bundle_before_database_access(self):
        value = bundle("Nova-Indonesia", "20260805", complete=False)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_complete_bundle(value)
        with self.assertRaises(ValueError):
            build_live_rows(value)
        with self.assertRaises(ValueError):
            build_ledger_rows(value)

    def test_complete_bundle_builds_live_and_ledger_rows_with_utc_date(self):
        value = bundle("Nova-Indonesia", "20260805")
        live = build_live_rows(value)
        ledger = build_ledger_rows(value)
        self.assertEqual(dt.date(2026,8,5), live[0][2])
        self.assertEqual(dt.date(2026,8,5), ledger[0][2])
        self.assertEqual(7, live[0][7])
        self.assertEqual(7, ledger[0][9])

    def test_hourly_fetches_today_and_closes_yesterday_once_without_bi_dependency(self):
        calls = []
        events = []
        def fetcher(guild, day, **_kwargs):
            calls.append((guild, day))
            return bundle(guild, day)
        def consumer(value, **kwargs):
            events.append(("write", value.source_guild, value.business_date))
            if kwargs["mark_closed"]:
                write_closure(kwargs["state_root"], value, kwargs["batch_id"])
        with TemporaryDirectory() as root, patch("linky_sync_runner.process_bundle", side_effect=consumer):
            state = Path(root)
            (state / "linky-voice-audit").mkdir()
            (state / "linky-voice-audit" / "waiting.json").write_text('{"status":"WAITING_BI"}')
            first = run_cycle(job_name="test", mode="hourly", guilds=["Nova-Indonesia"],
                utc_today=dt.date(2026,8,5), database_url=None, state_root=state,
                dry_run=False, fetcher=fetcher, batch_id="batch-one",sink=lambda _value:None)
            second = run_cycle(job_name="test", mode="hourly", guilds=["Nova-Indonesia"],
                utc_today=dt.date(2026,8,5), database_url=None, state_root=state,
                dry_run=False, fetcher=fetcher, batch_id="batch-two",sink=lambda _value:None)
            self.assertEqual(calls, [("Nova-Indonesia","20260805"),
                ("Nova-Indonesia","20260804"), ("Nova-Indonesia","20260805")])
            self.assertTrue(closure_complete(state,"2026-08-04","Nova-Indonesia"))
            self.assertEqual("SUCCESS", first[-1]["status"])
            self.assertEqual(1, sum(item["status"] == "SKIPPED_API_CLOSED" for item in second))

    def test_runner_processes_and_releases_each_guild_in_order(self):
        events = []
        tokens_seen = []
        def fetcher(guild, day, **kwargs):
            events.append(("fetch", guild, day))
            tokens_seen.append(kwargs.get("tokens_path"))
            return bundle(guild, day)
        def consumer(value, **_kwargs):
            events.append(("write", value.source_guild, value.business_date))
        with TemporaryDirectory() as root, patch("linky_sync_runner.process_bundle", side_effect=consumer), \
                patch("linky_sync_runner.closure_complete", return_value=True):
            run_cycle(job_name="test", mode="hourly", guilds=["A","B"],
                utc_today=dt.date(2026,8,5), database_url=None, state_root=Path(root),
                dry_run=True, fetcher=fetcher, tokens_path="/private/tokens.json", batch_id="ordered",
                sink=lambda _value:None)
        self.assertEqual(events, [("fetch","A","20260805"),("write","A","20260805"),
            ("fetch","B","20260805"),("write","B","20260805")])
        self.assertEqual(["/private/tokens.json","/private/tokens.json"], tokens_seen)

    def test_fetch_failure_never_calls_consumers_or_marks_closed(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("room page failed")
        with TemporaryDirectory() as root, patch("linky_sync_runner.process_bundle") as consumer:
            results = run_cycle(job_name="test", mode="close-yesterday", guilds=["Nova"],
                utc_today=dt.date(2026,8,5), database_url=None, state_root=Path(root),
                dry_run=False, fetcher=broken, batch_id="failed",sink=lambda _value:None)
            consumer.assert_not_called()
            self.assertEqual("FAILED", results[0]["status"])
            self.assertFalse(closure_complete(Path(root),"2026-08-04","Nova"))

    def test_nonblocking_domain_lock_skips_without_fetching(self):
        with TemporaryDirectory() as root:
            lock = Path(root) / "collection.lock"
            with lock.open("a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["--job-name","lock-test","--mode","hourly",
                        "--guild","Nova","--lock-file",str(lock),"--state-root",root,"--dry-run"])
            self.assertEqual(0, code)
            record = json.loads(output.getvalue())
            self.assertEqual("SKIPPED_LOCK_BUSY", record["lockResult"])
            self.assertFalse((Path(root) / "linky-runs").exists())
            for field in ("jobName","batchId","businessDate","sourceGuild","endpoint","pageCount",
                    "rawRowCount","positiveRowCount","requestCount","retryCount","apiElapsedSeconds",
                    "writeElapsedSeconds","scanComplete","lockResult","bundleReused"):
                self.assertIn(field, record)

    def test_explicit_guild_dry_run_does_not_require_database_for_discovery(self):
        with TemporaryDirectory() as root, \
                patch("linky_sync_runner.database_url_from_environment", return_value=None), \
                patch("linky_sync_runner.run_cycle", return_value=[{"status": "SUCCESS"}]) as cycle:
            code = main(["--job-name", "target-dry", "--mode", "target",
                "--guild", "Nova-Indonesia", "--business-date", "20260803",
                "--lock-file", str(Path(root) / "lock"), "--state-root", root, "--dry-run"])
        self.assertEqual(0, code)
        self.assertEqual(["Nova-Indonesia"], cycle.call_args.kwargs["guilds"])

    def test_daily_sync_only_runs_bi_audit_not_linky_fetch(self):
        source = Path("daily-sync.sh").read_text()
        self.assertIn("linky_voice_bi_batch.py", source)
        self.assertNotIn("linky_sync_runner.py", source)
        self.assertNotIn("linke_ledger_pull.py", source)

    def test_success_observations_have_required_fields_and_reuse_bundle(self):
        records = []
        with TemporaryDirectory() as root:
            process_bundle(bundle("Nova","20260805"), job_name="hourly", batch_id="batch",
                database_url=None, write_live=True, write_ledger=True, dry_run=True,
                snapshot_slot=dt.datetime(2026,8,5,tzinfo=dt.timezone.utc),
                state_root=Path(root), mark_closed=False, evidence_dir=Path(root)/"evidence",
                sink=lambda value: records.append(json.loads(value)))
        core = [item for item in records if item["endpoint"].startswith("/api/guild/")]
        self.assertEqual(2, len(core))
        for record in core:
            for field in ("jobName","batchId","businessDate","sourceGuild","endpoint","pageCount",
                    "rawRowCount","positiveRowCount","requestCount","retryCount","apiElapsedSeconds",
                    "writeElapsedSeconds","scanComplete","lockResult","bundleReused"):
                self.assertIn(field, record)
            self.assertTrue(record["bundleReused"])

    def test_database_failure_does_not_mark_api_closed(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        fake_psycopg = types.SimpleNamespace(connect=lambda _url: Connection())
        with TemporaryDirectory() as root, patch.dict(sys.modules,{"psycopg2":fake_psycopg}), \
                patch("linky_sync_runner.write_live_views",return_value=1), \
                patch("linky_sync_runner.write_daily_ledger",side_effect=RuntimeError("db failed")):
            with self.assertRaisesRegex(RuntimeError,"db failed"):
                process_bundle(bundle("Nova","20260804"),job_name="close",batch_id="db-fail",
                    database_url="postgres://fake",write_live=True,write_ledger=True,dry_run=False,
                    snapshot_slot=dt.datetime(2026,8,5,tzinfo=dt.timezone.utc),state_root=Path(root),
                    mark_closed=True,evidence_dir=Path(root)/"evidence",sink=lambda _value:None)
            self.assertFalse(closure_complete(Path(root),"2026-08-04","Nova"))

    def test_corrupt_or_incomplete_closure_state_is_not_accepted(self):
        with TemporaryDirectory() as root:
            state=Path(root)
            write_closure(state,bundle("Nova","20260804"),"ok")
            path=state/"linky-api-closure"/"2026-08-04-Nova.json"
            value=json.loads(path.read_text())
            value["voiceRoom"]["rawRowCount"]=999
            path.write_text(json.dumps(value))
            self.assertFalse(closure_complete(state,"2026-08-04","Nova"))

    def test_compatibility_wrappers_only_delegate_with_expected_consumers(self):
        with patch("linke_live_pull.runner_main", return_value=0) as live:
            self.assertEqual(0, linke_live_pull.main(["Nova","20260804"]))
            args=live.call_args.args[0]
            self.assertIn("--target-write-live",args)
            self.assertIn("--target-no-ledger",args)
        with patch("linke_ledger_pull.runner_main", return_value=0) as ledger:
            self.assertEqual(0,linke_ledger_pull.main(["Nova","20260804","--dry-run",
                "--tokens","/private/tokens.json","--evidence-dir","/tmp/evidence"]))
            args=ledger.call_args.args[0]
            self.assertIn("--dry-run",args)
            self.assertEqual("/private/tokens.json",args[args.index("--tokens")+1])
            self.assertEqual("/tmp/evidence",args[args.index("--evidence-dir")+1])

    def test_expired_batch_budget_stops_before_dispatch_and_cannot_accumulate(self):
        with TemporaryDirectory() as root:
            results=run_cycle(job_name="deadline",mode="hourly",guilds=["A","B"],
                utc_today=dt.date(2026,8,5),database_url=None,state_root=Path(root),dry_run=True,
                fetcher=lambda *_args,**_kwargs:self.fail("fetch must not start"),
                max_batch_seconds=0,batch_id="deadline",sink=lambda _value:None)
        self.assertEqual(["STOPPED_BATCH_DEADLINE"],[item["status"] for item in results])


if __name__ == "__main__":
    unittest.main()
