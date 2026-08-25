import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import direct_id_reconciliation_apply as subject


def evidence(scope, guild_count, results):
    rows = sorted(results, key=lambda row: row["subjectId"])
    return {
        "schemaVersion": 1,
        "sourceScope": scope,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "targetCount": len(rows),
        "targetChecksum": subject.canonical_hash([row["subjectId"] for row in rows]),
        "configuredGuildCount": guild_count,
        "results": rows,
    }


class DirectIdReconciliationApplyTest(unittest.TestCase):
    def test_plan_splits_invalid_correct_and_not_found(self):
        linky = evidence("linky_official_all_configured_guild_rosters", 10, [
            {"subjectId": "12345678", "status": "FOUND", "matches": [
                {"rawGuild": "A", "joinedGuildDate": "2026-08-01"}]},
            {"subjectId": "22345678", "status": "NOT_FOUND", "matches": []},
        ])
        timo = evidence("timo_official_all_three_guild_current_rosters", 3, [
            {"subjectId": "123456789012", "status": "NOT_FOUND", "matches": []},
        ])
        plan = subject.build_plan(["7654321", "12345678", "22345678", "123456789012"], linky, timo)
        self.assertEqual(plan["summary"], {
            "invalid": 1, "correctLinky": 1, "deleteLinkyNotFound": 1,
            "deleteTimoNotFound": 1, "ambiguous": 0,
        })

    def test_target_set_drift_is_rejected(self):
        linky = evidence("linky_official_all_configured_guild_rosters", 10, [])
        timo = evidence("timo_official_all_three_guild_current_rosters", 3, [])
        with self.assertRaisesRegex(ValueError, "active_target_set_drift"):
            subject.build_plan(["12345678"], linky, timo)

    def test_prior_evidence_may_be_reused_for_remaining_invalid_rows(self):
        linky = evidence("linky_official_all_configured_guild_rosters", 10, [
            {"subjectId": "12345678", "status": "NOT_FOUND", "matches": []},
        ])
        timo = evidence("timo_official_all_three_guild_current_rosters", 3, [])
        plan = subject.build_plan(["7654321"], linky, timo)
        self.assertEqual(plan["invalid"], ["7654321"])
        self.assertEqual(plan["deleteLinkyNotFound"], [])

    def test_evidence_checksum_and_freshness_are_verified(self):
        payload = evidence("linky_official_all_configured_guild_rosters", 10, [])
        payload["evidenceChecksum"] = subject.canonical_hash(payload)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            path.write_text(json.dumps(payload))
            loaded = subject.load_evidence(path, "linky_official_all_configured_guild_rosters", 10)
            self.assertEqual(loaded["targetCount"], 0)
            payload["targetCount"] = 1
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "evidence_checksum_invalid"):
                subject.load_evidence(path, "linky_official_all_configured_guild_rosters", 10)


if __name__ == "__main__":
    unittest.main()
