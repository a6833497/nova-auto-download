#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
STATE_ROOT="${LINKE_STATE_ROOT:-/home/ubuntu/nova-auto-download/state}"
BACKEND_CURRENT="${NOVA_BACKEND_CURRENT:-/home/ubuntu/nova-backend-current}"
BACKEND_API_DIR="$(readlink -f "$BACKEND_CURRENT/api")"
BATCH_ID="$(TZ=UTC date +%Y%m%dT%H%M%SZ)-hourly-$$"

NOVA_BACKEND_API_DIR="$BACKEND_API_DIR" \
  python3 verify_runtime_closure.py --preflight-entry linke_live_refresh.sh

python3 linky_sync_runner.py --job-name linky-hourly --batch-id "$BATCH_ID" --mode hourly
python3 rebuild_display_time.py "$(TZ=UTC date +%F)" "$(TZ=UTC date +%F)"

TSX_BIN="$BACKEND_API_DIR/node_modules/.bin/tsx"
PROJECTION_SCRIPT="$BACKEND_API_DIR/src/scripts/refresh-operations-projections.ts"
[[ -x "$TSX_BIN" && -f "$PROJECTION_SCRIPT" ]] || {
  echo "Linky projection runtime is incomplete" >&2
  exit 66
}
"$TSX_BIN" "$PROJECTION_SCRIPT"

python3 write_runtime_receipt.py \
  --batch-id "$BATCH_ID" \
  --auto-release-root "$SCRIPT_DIR" \
  --backend-release-root "$(dirname "$BACKEND_API_DIR")" \
  --state-root "$STATE_ROOT"
