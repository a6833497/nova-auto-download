#!/usr/bin/env bash
# Current low-load schedule: at 16:00 Asia/Shanghai, import yesterday only.
set -euo pipefail
export TIMO_SYNC_WINDOW=daily
exec /home/ubuntu/nova-auto-download/sync-timo-external.sh
