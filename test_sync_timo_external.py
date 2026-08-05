import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
RUNNER = (ROOT / "sync-timo-external.sh").read_text()
DAILY = (ROOT / "sync-timo-external-daily.sh").read_text()


class TimoSyncRunnerContractTest(unittest.TestCase):
    def test_uses_one_non_blocking_production_lock(self):
        self.assertIn("exec 9>/tmp/timo-external-sync.lock", RUNNER)
        self.assertIn("flock -n 9 || exit 75", RUNNER)

    def test_consumes_canonical_owner_projection_without_legacy_rebuild(self):
        self.assertNotIn("sync-timo-ownership", RUNNER)
        self.assertIn("current_subject_owner is published by the canonical six-sheet", RUNNER)

    def test_reconciles_before_publication(self):
        reconcile = RUNNER.index("reconcile-timo-display.ts")
        publication = RUNNER.index("publish-daily-subject-metrics.ts")
        self.assertLess(reconcile, publication)

    def test_daily_wrapper_uses_the_single_runner(self):
        self.assertIn("TIMO_SYNC_WINDOW=daily", DAILY)
        self.assertIn("exec /home/ubuntu/nova-auto-download/sync-timo-external.sh", DAILY)


if __name__ == "__main__":
    unittest.main()
