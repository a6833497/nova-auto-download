import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from linky_voice_bi_audit import audit, reconcile, validate_payload
from linky_voice_bi_batch import choose_bi_file, prune_evidence


def payload(day="20260728", guilds=("Nova",), rows=None, tab="语音房主播行为数据"):
    rows = rows if rows is not None else [
        {"active_date(day)": day, "guild_name": guilds[0], "sid": "1", "diamond_amount": 12}]
    return {"headers": ["active_date(day)", "guild_name", "sid", "diamond_amount"], "rows": rows,
        "meta": {"tabName": tab, "reportName": "印尼", "rowCount": len(rows), "queriedAt": "2026-08-01T00:00:00Z"}}


class VoiceBiAuditTests(unittest.TestCase):
    def test_extracts_requested_utc_day_and_all_mapped_source_guilds(self):
        data = payload(guilds=("Carote", "Carote2"), rows=[
            {"active_date(day)": "20260728", "guild_name": "Carote", "sid": "1", "diamond_amount": 12},
            {"active_date(day)": "20260728", "guild_name": "Carote2", "sid": "1", "diamond_amount": 3},
            {"active_date(day)": "20260729", "guild_name": "Carote", "sid": "2", "diamond_amount": 99}])
        rows, source_day = validate_payload(data, "2026-07-28", ["Carote-Indonesia", "Carote2-Indonesia"])
        self.assertEqual(rows, {"Carote-Indonesia|1": 12, "Carote2-Indonesia|1": 3})
        self.assertEqual(source_day, "2026-07-28")

    def test_mismatch_is_per_source_guild_and_sid(self):
        result = reconcile({"Nova-Indonesia|1": 5}, {"Nova-Indonesia|1": 7, "Nova-Indonesia|3": 4})
        self.assertEqual(result["status"], "BI_MISMATCH")
        self.assertEqual((result["amountDelta"], result["changedSidCount"]), (6, 2))
        self.assertTrue(result["sidChecksum"] and result["amountChecksum"])

    def test_exact_match_is_verified(self):
        self.assertEqual(reconcile({"g|1": 5}, {"g|1": 5})["status"], "BI_VERIFIED")

    def test_missing_bi_is_explicit_waiting(self):
        result = audit("2026-07-28", "ID", ["Nova-Indonesia"], "印尼1语音房", None, None)
        self.assertEqual(result["status"], "WAITING_BI")
        self.assertTrue(result["scanComplete"])

    def test_wrong_date_or_weekly_salary_report_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_payload(payload(day="20260729"), "2026-07-28", ["Nova-Indonesia"])
        with self.assertRaises(ValueError):
            validate_payload(payload(tab="印尼语音房主播薪资奖励"), "2026-07-28", ["Nova-Indonesia"])

    def test_candidate_selection_uses_internal_day_and_latest_valid_query(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            wrong = payload(day="20260729"); newest = payload(); newest["meta"]["queriedAt"] = "2026-08-03T00:00:00Z"
            old = payload(); old["meta"]["queriedAt"] = "2026-08-02T00:00:00Z"
            for name, value in (("wrong_语音房主播行为数据.json", wrong), ("new_语音房主播行为数据.json", newest), ("old_语音房主播行为数据.json", old)):
                (root_path / name).write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(choose_bi_file(root_path, "2026-07-28", ["Nova-Indonesia"]).name, "new_语音房主播行为数据.json")

    def test_evidence_cleanup_only_removes_expired_audit_files(self):
        with TemporaryDirectory() as root:
            directory=Path(root)
            old=directory/"2020-01-01-Nova-Indonesia.json";keep=directory/"notes.json"
            old.write_text("{}");keep.write_text("keep")
            prune_evidence(directory,30)
            self.assertFalse(old.exists());self.assertTrue(keep.exists())


if __name__ == "__main__":
    unittest.main()
