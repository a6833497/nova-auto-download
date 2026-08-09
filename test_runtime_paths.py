from pathlib import Path
import unittest


ROOT = Path(__file__).parent


class RuntimePathsTest(unittest.TestCase):
    def test_daily_sync_state_and_notify_paths_are_overridable(self):
        source = (ROOT / "daily-sync.sh").read_text(encoding="utf-8")
        self.assertIn('STATE_ROOT="${NOVA_STATE_ROOT:-/home/ubuntu/nova-auto-download/state}"', source)
        self.assertIn('NOTIFY_SCRIPT="${NOVA_NOTIFY_SCRIPT:-$SCRIPT_DIR/feishu-notify.py}"', source)
        self.assertIn('"$STATE_ROOT/linky-voice-audit"', source)
        self.assertIn('"$STATE_ROOT/linky-voice-repair"', source)
        self.assertIn('python3 "$NOTIFY_SCRIPT"', source)
        self.assertIn("export PLAYWRIGHT_BROWSERS_PATH=0", source)


    def test_heal_and_timo_use_their_runtime_script_directory_by_default(self):
        heal = (ROOT / "bi-data-heal.sh").read_text(encoding="utf-8")
        timo = (ROOT / "sync-timo-external.sh").read_text(encoding="utf-8")
        self.assertIn('SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"', heal)
        self.assertIn('NOVA_DAILY_SYNC_COMMAND:-$SCRIPT_DIR/daily-sync.sh', heal)
        self.assertIn('NOVA_NOTIFY_SCRIPT:-$SCRIPT_DIR/feishu-notify.py', heal)
        self.assertIn('SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"', timo)
        self.assertIn('TIMO_DISPLAY_TIME_REBUILDER:-$SCRIPT_DIR/rebuild_display_time.py', timo)


    def test_runtime_preflight_is_fail_closed_before_locks_or_business_access(self):
        daily = (ROOT / "daily-sync.sh").read_text(encoding="utf-8")
        heal = (ROOT / "bi-data-heal.sh").read_text(encoding="utf-8")
        timo_daily = (ROOT / "sync-timo-external-daily.sh").read_text(encoding="utf-8")
        self.assertLess(daily.index("--preflight-entry daily-sync.sh || exit $?"), daily.index('echo $$ > "$LOCK_FILE"'))
        self.assertLess(heal.index("--preflight-entry bi-data-heal.sh || exit $?"), heal.index("TOTAL_COUNT=$(run_sql"))
        self.assertLess(timo_daily.index("--preflight-entry sync-timo-external-daily.sh || exit $?"), timo_daily.index('exec "$SCRIPT_DIR/sync-timo-external.sh"'))


if __name__ == "__main__":
    unittest.main()
