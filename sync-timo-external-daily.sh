#!/usr/bin/env bash
# Current low-load schedule: at 17:20/17:50 Asia/Shanghai, import yesterday only.
set -euo pipefail
export TIMO_SYNC_WINDOW=daily
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
DATA_WRITE_LOCK="${NOVA_DATA_WRITE_LOCK:-/tmp/nova-data-write.lock}"
python3 "$SCRIPT_DIR/verify_runtime_closure.py" --preflight-entry sync-timo-external-daily.sh || exit $?
exec 8>"$DATA_WRITE_LOCK"
flock 8
exec "$SCRIPT_DIR/sync-timo-external.sh" "$@"
