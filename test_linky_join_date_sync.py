import unittest
from unittest.mock import patch

import linky_join_date_sync as subject


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)

    def cursor(self):
        return self.cursor_value


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

    def test_linky_empty_exact_search_shape_is_not_found(self):
        call = lambda _path: {"next_page": 0, "show_star": False}
        with patch.object(subject, "_authenticated_call", return_value=call), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["Nova-Indonesia"])
        self.assertEqual(result["status"], "NOT_FOUND")

    def test_partial_source_failure_fails_closed(self):
        with patch.object(subject, "_authenticated_call", side_effect=RuntimeError("offline")), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["Nova-Indonesia"])
        self.assertEqual(result["status"], "SOURCE_STALE")
        self.assertIsNone(result["date"])

    def test_match_plus_partial_source_failure_still_fails_closed(self):
        responses = [
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000}]},
            RuntimeError("offline"),
        ]
        with patch.object(subject, "_authenticated_call", side_effect=responses), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["A", "B"])
        self.assertEqual(result["status"], "SOURCE_STALE")
        self.assertIsNone(result["guild"])

    def test_conflicting_dates_fail_closed(self):
        responses = [
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000}]},
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787086400}]},
        ]
        with patch.object(subject, "_authenticated_call", side_effect=responses), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["A", "B"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "multiple_official_guilds")

    def test_same_date_in_two_guilds_is_ambiguous(self):
        responses = [
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000}]},
            lambda _path: {"total": 1, "items": [{"sid": "12345678", "created_at": 1787000000}]},
        ]
        with patch.object(subject, "_authenticated_call", side_effect=responses), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["A", "B"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "multiple_official_guilds")

    def test_conflicting_rows_inside_one_guild_fail_closed(self):
        call = lambda _path: {"items": [
            {"sid": "12345678", "created_at": 1787000000},
            {"sid": "12345678", "created_at": 1787086400},
        ]}
        with patch.object(subject, "_authenticated_call", return_value=call), patch.object(subject.time, "sleep"):
            result = subject.lookup("12345678", ["A"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"], "conflicting_official_records")

    def test_candidates_are_valid_linky_ids_and_not_bound_to_old_guild(self):
        connection = FakeConnection([("12345678",)])
        self.assertEqual(subject.candidates(connection, 20), ["12345678"])
        self.assertIn("d.subject_id ~ '^[1-9][0-9]{7}$'", connection.cursor_value.sql)
        self.assertNotIn("g.guild_alias=d.guild_name", connection.cursor_value.sql)

    def test_all_configured_guilds_must_have_formal_mapping(self):
        complete = FakeConnection([("A", "印尼1", "ID"), ("B", "巴西1", "BR")])
        self.assertEqual(subject.guild_mappings(complete, {"A", "B"})["A"], ("印尼1", "ID"))
        incomplete = FakeConnection([("A", "印尼1", "ID")])
        with self.assertRaisesRegex(RuntimeError, "configured_guild_mapping_incomplete:B"):
            subject.guild_mappings(incomplete, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
