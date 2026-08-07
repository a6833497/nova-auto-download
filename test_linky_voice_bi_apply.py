import json
import tempfile
import unittest
from pathlib import Path

from linky_voice_bi_apply import load_evidence, targets_from_evidence


def evidence(changed):
    return {"businessDate": "2026-08-03", "sourceGuilds": ["Nova-Indonesia"], "changedItems": changed}


class VoiceBiApplyTest(unittest.TestCase):
    def test_builds_stable_composite_targets(self):
        rows = targets_from_evidence([evidence([
            {"identity": "Nova-Indonesia|20", "ledger": 1, "bi": 3, "delta": 2},
            {"identity": "Nova-Indonesia|10", "ledger": 0, "bi": 2, "delta": 2},
        ])])
        self.assertEqual([row["sid"] for row in rows], ["10", "20"])

    def test_rejects_cross_guild_sid_date_conflict(self):
        other = {"businessDate": "2026-08-03", "sourceGuilds": ["Permata-Indonesia"],
            "changedItems": [{"identity": "Permata-Indonesia|10", "ledger": 0, "bi": 4, "delta": 4}]}
        with self.assertRaisesRegex(ValueError, "multiple BI voice facts"):
            targets_from_evidence([evidence([
                {"identity": "Nova-Indonesia|10", "ledger": 0, "bi": 2, "delta": 2}]), other])

    def test_rejects_identity_outside_evidence_scope(self):
        with self.assertRaisesRegex(ValueError, "outside evidence scope"):
            targets_from_evidence([evidence([
                {"identity": "Other-Indonesia|10", "ledger": 0, "bi": 2, "delta": 2}])])

    def test_rejects_invalid_or_negative_amount(self):
        with self.assertRaisesRegex(ValueError, "invalid voice amount"):
            targets_from_evidence([evidence([
                {"identity": "Nova-Indonesia|10", "ledger": 2, "bi": -1, "delta": -3}])])

    def test_waiting_evidence_is_a_safe_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-03-Nova-Indonesia.json"
            path.write_text(json.dumps({"schemaVersion": 2, "scanComplete": True,
                "status": "WAITING_BI", "businessDate": "2026-08-03",
                "formalGuild": "印尼1语音房", "sourceGuilds": ["Nova-Indonesia"]}))
            loaded = load_evidence(Path(directory), "2026-08-03")
            self.assertEqual(targets_from_evidence(loaded), [])


if __name__ == "__main__":
    unittest.main()
