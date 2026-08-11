import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from linky_summary_runner import fetch_guild_summary, publish_summary


class LinkySummaryRunnerTest(unittest.TestCase):
    def test_reads_only_page_one_totals_and_marks_provisional(self):
        calls = []
        def call(path):
            calls.append(path)
            if "streamer_stat" in path:
                return {"items": [{"sid": "1"}], "total": 14079,
                    "total_item": {"total_earns": 94204}}
            return {"items": [{"sid": "2"}], "total": 16747,
                "total_item": {"receive_diamonds": 85}}
        result = fetch_guild_summary("Evian-ES2", "20260811", call=call)
        self.assertEqual("PROVISIONAL", result["status"])
        self.assertEqual("94204", result["chatIncome"])
        self.assertEqual("85", result["roomIncome"])
        self.assertEqual(2, len(calls))
        self.assertTrue(all("page_num=1&page_size=1" in path for path in calls))

    def test_missing_summary_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "has no total_earns"):
            fetch_guild_summary("Nova", "20260811", call=lambda _path: {"items": [], "total": None})

    def test_failed_guild_retains_last_value_as_stale(self):
        with TemporaryDirectory() as root:
            state = Path(root)
            previous = {"schemaVersion": 1, "guilds": [{"sourceGuild": "A",
                "businessDate": "20260811", "status": "PROVISIONAL", "freshness": "FRESH",
                "chatIncome": "12", "roomIncome": "3", "observedAt": "prior"}]}
            latest = state / "linky-guild-summary" / "latest.json"
            latest.parent.mkdir(parents=True)
            latest.write_text(json.dumps(previous))
            document, complete = publish_summary(state, ["A", "B"], "20260811", "batch",
                fetcher=lambda _guild, _day: (_ for _ in ()).throw(RuntimeError("upstream")))
            self.assertFalse(complete)
            self.assertEqual("STALE", document["guilds"][0]["freshness"])
            self.assertEqual("12", document["guilds"][0]["chatIncome"])
            self.assertEqual("UNAVAILABLE", document["guilds"][1]["status"])
            persisted = json.loads(latest.read_text())
            self.assertEqual("PARTIAL", persisted["status"])

    def test_batch_deadline_retains_prior_and_skips_remaining_network_calls(self):
        with TemporaryDirectory() as root, patch("linky_summary_runner.time.monotonic", return_value=6):
            state = Path(root)
            latest = state / "linky-guild-summary" / "latest.json"
            latest.parent.mkdir(parents=True)
            latest.write_text(json.dumps({"guilds": [{"sourceGuild": "A",
                "businessDate": "20260811", "chatIncome": "12", "roomIncome": "3"}]}))
            calls = []
            document, complete = publish_summary(state, ["A", "B"], "20260811", "deadline",
                fetcher=lambda *_args: calls.append(1), deadline_monotonic=5)
        self.assertFalse(complete)
        self.assertEqual([], calls)
        self.assertEqual("STALE", document["guilds"][0]["freshness"])
        self.assertEqual("UNAVAILABLE", document["guilds"][1]["status"])


if __name__ == "__main__":
    unittest.main()
