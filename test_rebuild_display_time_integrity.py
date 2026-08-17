import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).parent


class DisplayTimeIntegrityTest(unittest.TestCase):
    def test_rebuild_fails_closed_before_its_only_commit(self):
        source = (ROOT / "rebuild_display_time.py").read_text(encoding="utf-8")
        ast.parse(source)
        self.assertEqual(1, source.count("conn.commit()"))
        self.assertIn("def validate_allocation_manifests", source)
        self.assertIn("allocation_manifest_mismatch", source)
        self.assertIn("FULL OUTER JOIN", source)
        self.assertIn("COUNT(DISTINCT subject_id)", source)
        self.assertLess(
            source.index("validate_allocation_manifests(cur,date_from,date_to)"),
            source.index("conn.commit()"),
        )
        self.assertLess(source.index("conn.rollback()"), source.index("conn.commit()"))

    def test_both_settled_sources_are_compared_to_their_canonical_facts(self):
        source = (ROOT / "rebuild_display_time.py").read_text(encoding="utf-8")
        validator = source[source.index("def validate_allocation_manifests"):source.index("def stable_int")]
        self.assertIn("FROM metrics_daily m JOIN hosts h", validator)
        self.assertIn("source='LINKY_BI'", validator)
        self.assertIn("FROM external_timo_revenue_daily_staging t", validator)
        self.assertIn("d.source_key='TIMO'", validator)
        self.assertIn("source='TIMO'", validator)
        self.assertIn("BOOL_AND(is_settled)", validator)


if __name__ == "__main__":
    unittest.main()
