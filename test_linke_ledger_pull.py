import unittest
from unittest.mock import patch
from tempfile import NamedTemporaryFile, TemporaryDirectory
from pathlib import Path
import datetime as dt

from linke_ledger_pull import atomic_json, database_url_from_environment, prune_evidence, pull_pages


class PaginationTests(unittest.TestCase):
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

    def test_duplicate_positive_sid_fails_closed(self):
        pages = {
            1: {"items": [{"sid": "1", "v": 1}] * 500, "total": 501},
        }
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            pull_pages(lambda _path: pages[1], "/x", "20260728", "v")


if __name__ == "__main__":
    unittest.main()
