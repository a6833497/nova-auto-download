import unittest

from linky_voice_bi_audit import reconcile, voice_rows


class VoiceBiAuditTests(unittest.TestCase):
    def test_extracts_only_requested_utc_business_day(self):
        payload = {"rows": [
            {"active_date(day)": "20260728", "sid": "1", "diamond_amount": 12},
            {"active_date(day)": "20260729", "sid": "2", "diamond_amount": 99},
        ]}
        self.assertEqual(voice_rows(payload, "2026-07-28"), {"1": 12.0})

    def test_mismatch_is_per_sid_and_auditable(self):
        result = reconcile({"1": 5, "2": 0}, {"1": 7, "3": 4})
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["amountDelta"], 6)
        self.assertEqual(result["changedSidCount"], 2)

    def test_exact_match(self):
        self.assertEqual(reconcile({"1": 5}, {"1": 5})["status"], "MATCH")


if __name__ == "__main__":
    unittest.main()
