import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
RUNNER = (ROOT / "sync-timo-external.sh").read_text()
DAILY = (ROOT / "sync-timo-external-daily.sh").read_text()
DISPLAY_TIME = (ROOT / "rebuild_display_time.py").read_text()


class TimoSyncRunnerContractTest(unittest.TestCase):
    def test_uses_one_non_blocking_production_lock(self):
        self.assertIn("exec 9>/tmp/timo-external-sync.lock", RUNNER)
        self.assertIn("flock -n 9 || exit 75", RUNNER)
        self.assertIn('DATA_WRITE_LOCK="${NOVA_DATA_WRITE_LOCK:-/tmp/nova-data-write.lock}"', RUNNER)
        self.assertLess(RUNNER.index("flock -n 9"), RUNNER.index("flock 8"))
        self.assertLess(RUNNER.index("flock 8"), RUNNER.index("sync-timo-external.ts"))

    def test_consumes_canonical_owner_projection_without_legacy_rebuild(self):
        self.assertNotIn("sync-timo-ownership", RUNNER)
        self.assertIn("current_subject_owner is published by the canonical six-sheet", RUNNER)

    def test_reconciles_before_publication(self):
        reconcile = RUNNER.index("reconcile-timo-display.ts")
        publication = RUNNER.index("scripts/run-daily-publication.sh")
        self.assertLess(reconcile, publication)

    def test_delegates_candidate_projection_and_switch_to_canonical_runner(self):
        self.assertEqual(RUNNER.count("scripts/run-daily-publication.sh"), 1)
        self.assertNotIn("src/scripts/publish-daily-subject-metrics.ts", RUNNER)
        self.assertNotIn("src/scripts/rebuild-page-projections.ts", RUNNER)
        self.assertNotIn("DELETE FROM dashboard_cache", RUNNER)

    def test_display_time_uses_exact_storage_identity_and_configured_database(self):
        self.assertIn("DATABASE_URL is required", DISPLAY_TIME)
        self.assertIn("database_url_from_environment()", DISPLAY_TIME)
        self.assertNotIn("postgresql://", DISPLAY_TIME)
        self.assertIn("external_timo_revenue_metric_snapshot s", DISPLAY_TIME)
        self.assertIn("external_timo_revenue_daily_staging t", DISPLAY_TIME)
        self.assertEqual(DISPLAY_TIME.count("d.raw_guild=trim("), 2)
        self.assertNotIn("ILIKE", DISPLAY_TIME)

    def test_daily_wrapper_uses_the_single_runner(self):
        self.assertIn("TIMO_SYNC_WINDOW=daily", DAILY)
        self.assertIn('SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"', DAILY)
        self.assertIn('exec "$SCRIPT_DIR/sync-timo-external.sh" "$@"', DAILY)
        self.assertNotIn("/home/ubuntu/nova-auto-download/sync-timo-external.sh", DAILY)


if __name__ == "__main__":
    unittest.main()
