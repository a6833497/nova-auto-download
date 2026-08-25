import unittest

import linky_all_guild_roster_audit as subject


class LinkyAllGuildRosterAuditTest(unittest.TestCase):
    def test_scan_stops_when_partial_final_page_reaches_total(self):
        pages = {
            1: {"total_anchors": 3, "next_page": 2, "items": [
                {"sid": "12345678", "created_at": 1787000000},
                {"sid": "22345678", "created_at": 1787000001},
            ]},
            2: {"total_anchors": 3, "next_page": 3, "items": [
                {"sid": "32345678", "created_at": 1787000002},
            ]},
        }
        result = subject.scan_guild("A", {"12345678"}, lambda path: pages[int(path.split("page=")[1].split("&")[0])], 2)
        self.assertEqual(result["rowCount"], 3)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["matches"][0]["subjectId"], "12345678")

    def test_duplicate_subject_fails_closed(self):
        payload = {"total_anchors": 2, "next_page": 2, "items": [
            {"sid": "12345678", "created_at": 1787000000},
            {"sid": "12345678", "created_at": 1787000000},
        ]}
        with self.assertRaisesRegex(RuntimeError, "missing_or_duplicate_subject"):
            subject.scan_guild("A", set(), lambda _path: payload, 100)

    def test_build_evidence_classifies_unique_none_and_ambiguous(self):
        payloads = {
            "A": {"total_anchors": 2, "next_page": 2, "items": [
                {"sid": "12345678", "created_at": 1787000000},
                {"sid": "22345678", "created_at": 1787000000},
            ]},
            "B": {"total_anchors": 1, "next_page": 2, "items": [
                {"sid": "22345678", "created_at": 1787000000},
            ]},
        }
        evidence = subject.build_evidence(
            ["12345678", "22345678", "32345678"], ["A", "B"],
            lambda guild: lambda _path: payloads[guild], page_size=100, workers=2)
        self.assertEqual(evidence["summary"], {"FOUND": 1, "NOT_FOUND": 1, "AMBIGUOUS": 1})
        self.assertTrue(evidence["evidenceChecksum"].startswith("sha256:"))

    def test_total_drift_fails_closed(self):
        calls = iter([
            {"total_anchors": 2, "next_page": 2, "items": [{"sid": "12345678", "created_at": 1}]},
            {"total_anchors": 3, "next_page": 3, "items": [{"sid": "22345678", "created_at": 2}]},
        ])
        with self.assertRaisesRegex(RuntimeError, "total_drift"):
            subject.scan_guild("A", set(), lambda _path: next(calls), 100)


if __name__ == "__main__":
    unittest.main()
