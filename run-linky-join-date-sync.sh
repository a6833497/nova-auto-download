#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${NOVA_BACKEND_ENV_FILE:-/home/ubuntu/nova-backend-current/api/.env}"
[[ -r "$ENV_FILE" ]] || { echo "backend env unavailable" >&2; exit 66; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
exec timeout "${LINKY_JOIN_DATE_TIMEOUT_SECONDS:-300}" python3 "$SCRIPT_DIR/linky_join_date_sync.py" "$@"
