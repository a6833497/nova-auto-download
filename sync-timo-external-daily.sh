#!/usr/bin/env bash
# Current low-load schedule: at 16:00 Asia/Shanghai, import yesterday only.
set -euo pipefail
export TIMO_SYNC_WINDOW=daily
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
python3 "$SCRIPT_DIR/verify_runtime_closure.py" --preflight-entry sync-timo-external-daily.sh || exit $?
exec "$SCRIPT_DIR/sync-timo-external.sh" "$@"
