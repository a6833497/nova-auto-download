import unittest
from unittest.mock import patch
from tempfile import NamedTemporaryFile, TemporaryDirectory
from pathlib import Path
import datetime as dt

from linke_ledger_pull import atomic_json, database_url_from_environment, evidence_filename, prune_evidence
from linky_api_pagination import pull_pages


class PaginationTests(unittest.TestCase):
    def test_all_production_callers_share_or_explicitly_block_pagination(self):
        root=Path(__file__).parent
        fetch=(root/'linky_fetch.py').read_text()
        self.assertIn('pull_pages',fetch)
        for name in ('linke_ledger_pull.py','linke_live_pull.py','linky_consumers.py'):
            source=(root/name).read_text()
            self.assertNotIn('urllib.request',source)
            self.assertNotIn('pull_pages(',source)
        self.assertIn('from linky_sync_runner import main as runner_main',(root/'linke_ledger_pull.py').read_text())
        self.assertIn('from linky_sync_runner import main as runner_main',(root/'linke_live_pull.py').read_text())
        self.assertIn('is retired and must not enter a production execution path',(root/'backfill_voice_call.py').read_text())

    def test_existing_conflict_does_not_reassign_a_sid_to_another_guild(self):
        source=Path(__file__).with_name('linky_consumers.py').read_text()
        conflict=source.split('ON CONFLICT (sid,stat_date) DO UPDATE SET',1)[1]
        self.assertNotIn('guild=EXCLUDED.guild',conflict)

    def test_only_ended_business_days_keep_multiple_timestamped_versions(self):
        detected=dt.datetime(2026,8,4,9,30,tzinfo=dt.timezone.utc)
        self.assertEqual(evidence_filename('20260804','Nova-Indonesia',detected,dt.date(2026,8,4)),'20260804-Nova-Indonesia.json')
        self.assertEqual(evidence_filename('20260803','Nova-Indonesia',detected,dt.date(2026,8,4)),'20260803-Nova-Indonesia-20260804T093000000000Z.json')
    def test_evidence_is_private_and_retention_only_removes_matching_old_files(self):
        with TemporaryDirectory() as root:
            directory=Path(root)/'evidence'
            old=directory/'20260701-guild.json';recent=directory/'20260728-guild.json';unrelated=directory/'notes.json'
            atomic_json(old,{'ok':True});atomic_json(recent,{'ok':True});unrelated.write_text('keep')
            removed=prune_evidence(directory,14,dt.date(2026,8,4))
            self.assertEqual(removed,['20260701-guild.json'])
            self.assertTrue(recent.exists());self.assertTrue(unrelated.exists())
            self.assertEqual(directory.stat().st_mode & 0o777,0o700)
            self.assertEqual(recent.stat().st_mode & 0o777,0o600)
    def test_database_url_comes_from_existing_api_env_without_embedding_a_password(self):
        with NamedTemporaryFile(mode="w",encoding="utf-8") as env:
            env.write('DATABASE_URL="postgresql://example-from-private-env"\n');env.flush()
            with patch.dict('os.environ',{'NOVA_API_ENV':env.name},clear=True):
                self.assertEqual(database_url_from_environment(),'postgresql://example-from-private-env')

    def test_zero_rows_do_not_stop_the_next_page(self):
        pages = {
            1: {"items": [{"sid": str(i), "receive_diamonds": 0 if i == 0 else 1} for i in range(500)], "total": 501},
            2: {"items": [{"sid": "later", "receive_diamonds": 9}], "total": 501},
        }
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            return pages[page]
        rows, evidence = pull_pages(call, "/voice", "20260728", "receive_diamonds", page_size=500)
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[-1]["sid"], "later")
        self.assertEqual([x["rawCount"] for x in evidence], [500, 1])

    def test_duplicate_positive_sid_within_page_fails_instead_of_overwriting(self):
        pages = {
            1: {"items": [{"sid": str(i), "v": 1} for i in range(500)], "total": 502},
            2: {"items": [{"sid": "39348432", "v": 1}, {"sid": "39348432", "v": 2}], "total": 502},
        }
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            return pages[page]
        with self.assertRaisesRegex(RuntimeError, "duplicate raw SID"):
            pull_pages(call, "/x", "20260728", "v", page_size=500)

    def test_mutable_cross_page_duplicate_requires_exact_summary_reconciliation(self):
        pages = {
            1: {"items": [{"sid": "1", "v": 5}, {"sid": "2", "v": 0}],
                "total": 4, "total_item": {"v": 5}},
            2: {"items": [{"sid": "2", "v": 0}, {"sid": "4", "v": 3}],
                "total": 4, "total_item": {"v": 8}},
        }
        call = lambda path: pages[int(path.split("page_num=")[1].split("&")[0])]
        rows, evidence = pull_pages(call, "/x", "20260811", "v", page_size=2,
            require_summary=False, allow_mutable_summary_reconciliation=True)
        self.assertEqual(["1", "4"], [row["sid"] for row in rows])
        summary = evidence[-1]["scanSummary"]
        self.assertEqual(1, summary["duplicateSidCount"])
        self.assertEqual(1, summary["totalChangeCount"])
        self.assertEqual("8", summary["detailAmount"])
        self.assertEqual("8", summary["totalItemAmount"])

    def test_mutable_cross_page_duplicate_with_changed_row_fails_closed(self):
        pages = {
            1: {"items": [{"sid": "1", "v": 5}, {"sid": "2", "v": 1}],
                "total": 4, "total_item": {"v": 6}},
            2: {"items": [{"sid": "2", "v": 2}, {"sid": "4", "v": 3}],
                "total": 4, "total_item": {"v": 10}},
        }
        call = lambda path: pages[int(path.split("page_num=")[1].split("&")[0])]
        with self.assertRaisesRegex(RuntimeError, "changed across pages"):
            pull_pages(call, "/x", "20260811", "v", page_size=2,
                allow_mutable_summary_reconciliation=True)

    def test_mutable_cross_page_duplicate_with_missing_positive_row_fails_closed(self):
        pages = {
            1: {"items": [{"sid": "1", "v": 5}, {"sid": "2", "v": 0}],
                "total": 4, "total_item": {"v": 5}},
            2: {"items": [{"sid": "2", "v": 0}, {"sid": "4", "v": 3}],
                "total": 4, "total_item": {"v": 9}},
        }
        call = lambda path: pages[int(path.split("page_num=")[1].split("&")[0])]
        with self.assertRaisesRegex(RuntimeError, "did not reconcile after merge"):
            pull_pages(call, "/x", "20260811", "v", page_size=2,
                allow_mutable_summary_reconciliation=True)

    def test_mutable_second_pass_merges_a_positive_row_shifted_out_of_first_pass(self):
        calls = []
        passes = [
            {
                1: {"items": [{"sid": "1", "v": 5}, {"sid": "2", "v": 0}],
                    "total": 4, "total_item": {"v": 5}},
                2: {"items": [{"sid": "2", "v": 0}, {"sid": "4", "v": 3}],
                    "total": 4, "total_item": {"v": 9}},
            },
            {
                1: {"items": [{"sid": "1", "v": 5}, {"sid": "3", "v": 1}],
                    "total": 4, "total_item": {"v": 9}},
                2: {"items": [{"sid": "3", "v": 1}, {"sid": "4", "v": 3}],
                    "total": 4, "total_item": {"v": 9}},
            },
        ]
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            pass_number = len(calls) // 2
            calls.append((pass_number, page))
            return passes[pass_number][page]
        rows, evidence = pull_pages(call, "/x", "20260811", "v", page_size=2,
            allow_mutable_summary_reconciliation=True)
        self.assertEqual(["1", "4", "3"], [row["sid"] for row in rows])
        self.assertEqual(4, len(calls))
        summary = evidence[-1]["scanSummary"]
        self.assertEqual(1, summary["reconciliationPassCount"])
        self.assertEqual("9", summary["detailAmount"])
        self.assertEqual("9", summary["totalItemAmount"])

    def test_historical_cross_page_duplicate_remains_fail_closed(self):
        pages = {
            1: {"items": [{"sid": "1", "v": 5}, {"sid": "2", "v": 0}], "total": 4},
            2: {"items": [{"sid": "2", "v": 0}, {"sid": "4", "v": 3}], "total": 4},
        }
        call = lambda path: pages[int(path.split("page_num=")[1].split("&")[0])]
        with self.assertRaisesRegex(RuntimeError, "across pages"):
            pull_pages(call, "/x", "20260810", "v", page_size=2)

    def test_total_change_and_raw_count_overflow_fail(self):
        pages = {1: {"items": [{"sid": "1", "v": 1}], "total": 2},
                 2: {"items": [{"sid": "2", "v": 1}], "total": 3}}
        with self.assertRaisesRegex(RuntimeError, "total changed"):
            pull_pages(lambda path: pages[int(path.split("page_num=")[1].split("&")[0])], "/x", "20260728", "v", page_size=1)
        with self.assertRaisesRegex(RuntimeError, "exceed"):
            pull_pages(lambda _path: {"items": [{"sid": "1", "v": 1}, {"sid": "2", "v": 1}], "total": 1}, "/x", "20260728", "v", page_size=2)

    def test_string_total_stops_at_the_reported_last_page(self):
        pages = {
            1: {"items": [{"sid": str(i), "v": 1} for i in range(500)], "total": "501"},
            2: {"items": [{"sid": "later", "v": 2}], "total": "501"},
        }
        calls = []
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            calls.append(page)
            return pages[page]
        rows, _evidence = pull_pages(call, "/x", "20260728", "v", page_size=500)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(rows), 501)

    def test_live_room_duplicate_sid_fails_closed(self):
        page = {"items": [{"sid": "1", "v": 1}, {"sid": "1", "v": 2}], "total": 2}
        with self.assertRaisesRegex(RuntimeError, "duplicate raw SID"):
            pull_pages(lambda _path: page, "/voice", "20260728", "v", page_size=2, require_unique_sid=True)

    def test_repeated_page_fails_closed(self):
        page = {"items": [{"sid": "1", "v": 1}, {"sid": "2", "v": 0}], "total": 3}
        with self.assertRaisesRegex(RuntimeError, "repeated response page"):
            pull_pages(lambda _path: page, "/voice", "20260728", "v", page_size=2)

    def test_short_page_before_total_fails_closed(self):
        page = {"items": [{"sid": "1", "v": 1}], "total": 2}
        with self.assertRaisesRegex(RuntimeError, "ended before reported total"):
            pull_pages(lambda _path: page, "/voice", "20260728", "v", page_size=2)

    def test_protocol_starts_at_page_num_one_with_exact_parameters(self):
        paths = []
        def call(path):
            paths.append(path)
            return {"items": [], "total": 0}
        pull_pages(call, "/voice", "20260728", "v")
        self.assertEqual(paths, ["/voice?begin=20260728&end=20260728&page_num=1&page_size=5000&type=0"])

    def test_one_authoritative_page_size_can_be_explicitly_overridden(self):
        paths = []
        pull_pages(lambda path: paths.append(path) or {"items": [], "total": 0},
            "/voice", "20260728", "v", page_size=5000)
        self.assertEqual(paths, ["/voice?begin=20260728&end=20260728&page_num=1&page_size=5000&type=0"])

    def test_max_page_cap_fails_closed(self):
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            return {"items": [{"sid": str(page), "v": 1}], "total": 3}
        with self.assertRaisesRegex(RuntimeError, "safety cap"):
            pull_pages(call, "/voice", "20260728", "v", page_size=1, max_pages=2)


if __name__ == "__main__":
    unittest.main()
