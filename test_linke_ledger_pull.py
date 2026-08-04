import unittest
from unittest.mock import patch
from tempfile import NamedTemporaryFile, TemporaryDirectory
from pathlib import Path
import datetime as dt

from linke_ledger_pull import atomic_json, database_url_from_environment, evidence_filename, prune_evidence, pull_pages


class PaginationTests(unittest.TestCase):
    def test_existing_conflict_does_not_reassign_a_sid_to_another_guild(self):
        source=Path(__file__).with_name('linke_ledger_pull.py').read_text()
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
        rows, evidence = pull_pages(call, "/voice", "20260728", "receive_diamonds")
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[-1]["sid"], "later")
        self.assertEqual([x["rawCount"] for x in evidence], [500, 1])

    def test_duplicate_positive_sid_is_recorded_and_last_value_wins(self):
        pages = {
            1: {"items": [{"sid": str(i), "v": 1} for i in range(500)], "total": 501},
            2: {"items": [{"sid": "39348432", "v": 1}, {"sid": "39348432", "v": 2}], "total": 501},
        }
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            return pages[page]
        rows, evidence = pull_pages(call, "/x", "20260728", "v")
        self.assertEqual(len(rows), 501)
        self.assertEqual(next(row for row in rows if row["sid"] == "39348432")["v"], 2)
        self.assertEqual(evidence[1]["duplicatePositiveSidCount"], 1)
        self.assertEqual(evidence[1]["duplicatePositiveSids"], ["39348432"])

    def test_string_total_stops_at_the_reported_last_page(self):
        pages = {
            1: {"items": [{"sid": str(i), "v": 1} for i in range(500)], "total": "501"},
            2: {"items": [{"sid": "later", "v": 2}] * 500, "total": "501"},
        }
        calls = []
        def call(path):
            page = int(path.split("page_num=")[1].split("&")[0])
            calls.append(page)
            return pages[page]
        rows, _evidence = pull_pages(call, "/x", "20260728", "v")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(rows), 501)


if __name__ == "__main__":
    unittest.main()
