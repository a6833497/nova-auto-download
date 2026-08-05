#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR"

python3 linky_sync_runner.py --job-name linky-hourly --mode hourly
python3 rebuild_display_time.py "$(TZ=UTC date +%F)" "$(TZ=UTC date +%F)"

cd /home/ubuntu/nova-backend-current/api
npx tsx src/scripts/refresh-operations-projections.ts
