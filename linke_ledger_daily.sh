#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR"

# Compatibility schedule: closed-day completion is state-driven and UTC based.
# The hourly runner performs the same check, so this exits without API calls once complete.
python3 linky_sync_runner.py --job-name linky-close-daily --mode close-yesterday
