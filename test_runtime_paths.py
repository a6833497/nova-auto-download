from pathlib import Path


ROOT = Path(__file__).parent


def test_daily_sync_state_and_notify_paths_are_overridable():
    source = (ROOT / "daily-sync.sh").read_text(encoding="utf-8")
    assert 'STATE_ROOT="${NOVA_STATE_ROOT:-/home/ubuntu/nova-auto-download/state}"' in source
    assert 'NOTIFY_SCRIPT="${NOVA_NOTIFY_SCRIPT:-$SCRIPT_DIR/feishu-notify.py}"' in source
    assert '"$STATE_ROOT/linky-voice-audit"' in source
    assert '"$STATE_ROOT/linky-voice-repair"' in source
    assert 'python3 "$NOTIFY_SCRIPT"' in source


def test_heal_and_timo_use_their_runtime_script_directory_by_default():
    heal = (ROOT / "bi-data-heal.sh").read_text(encoding="utf-8")
    timo = (ROOT / "sync-timo-external.sh").read_text(encoding="utf-8")
    assert 'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"' in heal
    assert 'NOVA_DAILY_SYNC_COMMAND:-$SCRIPT_DIR/daily-sync.sh' in heal
    assert 'NOVA_NOTIFY_SCRIPT:-$SCRIPT_DIR/feishu-notify.py' in heal
    assert 'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"' in timo
    assert 'TIMO_DISPLAY_TIME_REBUILDER:-$SCRIPT_DIR/rebuild_display_time.py' in timo
