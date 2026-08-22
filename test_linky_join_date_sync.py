import unittest
from unittest.mock import patch

import linky_join_date_sync as subject


class LinkyJoinDateSyncTest(unittest.TestCase):
    def test_exact_match_and_epoch_milliseconds(self):
        call = lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000000}]}
        with patch.object(subject, "_authenticated_call", return_value=call), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["Nova-Indonesia"])
        self.assertEqual(result["status"], "FOUND")
        self.assertRegex(result["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_non_exact_result_is_not_found(self):
        call = lambda _path: {"total": 1, "items": [{"sid": "87654321", "created_at": 1787000000}]}
        with patch.object(subject, "_authenticated_call", return_value=call), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["Nova-Indonesia"])
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertIsNone(result["date"])

    def test_partial_source_failure_fails_closed(self):
        with patch.object(subject, "_authenticated_call", side_effect=RuntimeError("offline")), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["Nova-Indonesia"])
        self.assertEqual(result["status"], "SOURCE_STALE")
        self.assertIsNone(result["date"])

    def test_conflicting_dates_fail_closed(self):
        responses = [
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000}]},
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787086400}]},
        ]
        with patch.object(subject, "_authenticated_call", side_effect=responses), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["A", "B"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "conflicting_official_dates")


if __name__ == "__main__":
    unittest.main()
