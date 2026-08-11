#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
BATCH_ID="$(TZ=UTC date +%Y%m%dT%H%M%SZ)-summary-$$"

NOVA_BACKEND_API_DIR="$(readlink -f "${NOVA_BACKEND_CURRENT:-/home/ubuntu/nova-backend-current}/api")" \
  python3 verify_runtime_closure.py --preflight-entry linke_guild_summary_refresh.sh

python3 linky_summary_runner.py --batch-id "$BATCH_ID"
