import datetime as dt
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error

from linky_fetch import FetchScanError, _authenticated_call, fetch_guild_day, new_request_scope


class FetchGuildDayTest(unittest.TestCase):
    def core_call(self, calls, fail_room=False):
        def call(path):
            calls.append(path)
            if "online_anchors" in path:
                return {"items": [{"sid": "9"}], "next_page": False}
            if fail_room and "live_room_stat" in path:
                raise RuntimeError("room failed")
            if "streamer_stat" in path:
                return {"items": [
                    {"sid": "1", "total_earns": 5},
                    {"sid": "2", "total_earns": 0},
                ], "total": 2, "total_item": {"total_earns": 5}}
            return {"items": [
                {"sid": "1", "receive_diamonds": 7},
                {"sid": "3", "receive_diamonds": 0},
            ], "total": 2, "total_item": {"receive_diamonds": 7}}
        return call

    def test_complete_bundle_and_observation_counts_zero_rows(self):
        calls = []
        bundle = fetch_guild_day("Nova-Indonesia", "20260804",
            call=self.core_call(calls), utc_today=dt.date(2026, 8, 5))
        self.assertTrue(bundle.scan_complete)
        self.assertEqual(["1"], [r["sid"] for r in bundle.streamer_rows])
        self.assertEqual(2, bundle.streamer_scan.raw_row_count)
        self.assertEqual(1, bundle.streamer_scan.positive_row_count)
        self.assertEqual(1, bundle.streamer_scan.request_count)
        self.assertIsNone(bundle.online_scan)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, bundle.streamer_scan.observation()["rawRowCount"])
        self.assertEqual(5000, bundle.streamer_scan.requested_page_size)
        self.assertEqual("5", bundle.streamer_scan.detail_amount)

    def test_page_size_is_part_of_request_and_request_scope_key(self):
        calls = []
        scope = new_request_scope()
        fetch_guild_day("Nova", "20260804", call=self.core_call(calls),
            request_scope=scope, utc_today=dt.date(2026, 8, 5), page_size=5000)
        self.assertIn("page_size=5000", calls[0])
        self.assertEqual(2, len(calls))

    def test_transient_gateway_error_retries_the_same_request_only(self):
        with TemporaryDirectory() as root:
            tokens = Path(root) / "tokens.json"
            tokens.write_text(json.dumps({"guilds": {"Nova": {
                "oauth_token": "token", "oauth_token_secret": "secret"}}}))
            response = io.BytesIO(b'{"items":[],"total":0}')
            error = urllib.error.HTTPError("https://api.linke.ai/x", 504, "timeout", {}, None)
            with patch("linky_fetch.urllib.request.urlopen", side_effect=[error, response]), \
                    patch("linky_fetch.time.sleep"):
                call = _authenticated_call("Nova", str(tokens))
                self.assertEqual(0, call("/x")["total"])
            self.assertEqual(2, call.attempt_count)
            self.assertEqual(1, call.retry_count)

    def test_bundle_observation_keeps_underlying_request_and_retry_counts(self):
        class CountingCall:
            attempt_count = 0
            retry_count = 0
            def __call__(self, path):
                if "streamer_stat" in path:
                    self.attempt_count += 2
                    self.retry_count += 1
                    return {"items": [{"sid": "1", "total_earns": 5}], "total": 1,
                        "total_item": {"total_earns": 5}}
                self.attempt_count += 1
                return {"items": [{"sid": "1", "receive_diamonds": 7}], "total": 1,
                    "total_item": {"receive_diamonds": 7}}
        value = fetch_guild_day("Nova", "20260804", call=CountingCall(),
            utc_today=dt.date(2026, 8, 5))
        self.assertEqual(2, value.streamer_scan.request_count)
        self.assertEqual(1, value.streamer_scan.retry_count)
        self.assertEqual(1, value.voice_room_scan.request_count)
        self.assertEqual(0, value.voice_room_scan.retry_count)

    def test_current_day_allows_mutable_or_empty_summary_but_ended_day_does_not(self):
        def mutable(path):
            if "streamer_stat" in path:
                return {"items": [{"sid": "1", "total_earns": 18}], "total": 1,
                    "total_item": {"total_earns": 0}}
            return {"items": [{"sid": "1", "receive_diamonds": 0}], "total": 1,
                "total_item": {}}
        current = fetch_guild_day("Nova", "20260805", call=mutable,
            utc_today=dt.date(2026, 8, 5))
        self.assertTrue(current.scan_complete)
        self.assertEqual("18", current.streamer_scan.detail_amount)
        self.assertEqual("0", current.streamer_scan.total_item_amount)
        self.assertIsNone(current.voice_room_scan.total_item_amount)
        with self.assertRaisesRegex(FetchScanError, "differs from total_item"):
            fetch_guild_day("Nova", "20260804", call=mutable,
                utc_today=dt.date(2026, 8, 5))

    def test_core_failure_returns_no_partial_bundle_and_does_not_memoize(self):
        calls = []
        scope = new_request_scope()
        with self.assertRaisesRegex(RuntimeError, "room failed") as caught:
            fetch_guild_day("Nova", "20260804", call=self.core_call(calls, True),
                request_scope=scope, utc_today=dt.date(2026, 8, 5))
        self.assertIsInstance(caught.exception, FetchScanError)
        self.assertEqual("/api/guild/live_room_stat", caught.exception.observation["endpoint"])
        self.assertEqual(1, caught.exception.observation["requestCount"])
        self.assertFalse(caught.exception.observation["scanComplete"])
        self.assertEqual({}, scope)

    def test_request_scope_reuses_bundle_without_network_calls(self):
        calls = []
        scope = new_request_scope()
        first = fetch_guild_day("Nova", "20260804", call=self.core_call(calls),
            request_scope=scope, utc_today=dt.date(2026, 8, 5))
        second = fetch_guild_day("Nova", "20260804", call=self.core_call(calls),
            request_scope=scope, utc_today=dt.date(2026, 8, 5))
        self.assertFalse(first.bundle_reused)
        self.assertTrue(second.bundle_reused)
        self.assertEqual(2, len(calls))

    def test_memo_does_not_cross_request_scopes(self):
        calls = []
        for scope in (new_request_scope(), new_request_scope()):
            fetch_guild_day("Nova", "20260804", call=self.core_call(calls),
                request_scope=scope, utc_today=dt.date(2026, 8, 5))
        self.assertEqual(4, len(calls))

    def test_online_anchors_are_today_only_and_best_effort(self):
        calls = []
        bundle = fetch_guild_day("Nova", "20260805", call=self.core_call(calls),
            utc_today=dt.date(2026, 8, 5))
        self.assertEqual(frozenset({9}), bundle.online_anchor_sids)
        self.assertTrue(bundle.online_scan.scan_complete)
        self.assertIn("page_size=5000", calls[-1])

        def broken_online(path):
            if "online_anchors" in path:
                raise RuntimeError("optional endpoint down")
            return self.core_call([])(path)
        bundle = fetch_guild_day("Nova", "20260805", call=broken_online,
            utc_today=dt.date(2026, 8, 5))
        self.assertTrue(bundle.scan_complete)
        self.assertFalse(bundle.online_scan.scan_complete)


if __name__ == "__main__":
    unittest.main()
