#!/usr/bin/env bash
# Timo sync runner. Daily mode is intentionally light; rolling mode is reserved
# for the future 4-hour/hourly schedule after the source server is upgraded.
set -euo pipefail

API_DIR=${TIMO_API_DIR:-/home/ubuntu/nova-backend-current/api}
DISPLAY_TIME_REBUILDER=${TIMO_DISPLAY_TIME_REBUILDER:-/home/ubuntu/nova-auto-download/rebuild_display_time.py}
SYNC_WINDOW=${TIMO_SYNC_WINDOW:-daily}

exec 9>/tmp/timo-external-sync.lock
flock -n 9 || exit 75

today=$(TZ=Asia/Shanghai date +%F)
yesterday=$(TZ=Asia/Shanghai date -d yesterday +%F)
case "$SYNC_WINDOW" in
  daily)
    date_from=$yesterday
    date_to=$yesterday
    ;;
  rolling)
    date_from=$yesterday
    date_to=$today
    ;;
  *)
    echo "Unsupported TIMO_SYNC_WINDOW=$SYNC_WINDOW (expected daily or rolling)" >&2
    exit 64
    ;;
esac

cd "$API_DIR"
set -a
source .env >/dev/null 2>&1
set +a
# Idle I/O priority and a positive nice value keep this non-urgent import from
# competing with interactive API traffic. The timeout prevents a stuck source
# request from accumulating into the next scheduled run.
ionice -c 3 nice -n 10 timeout 45m \
  npx tsx src/scripts/sync-timo-external.ts --from="$date_from" --to="$date_to"
ionice -c 3 nice -n 10 timeout 15m \
  python3 "$DISPLAY_TIME_REBUILDER" "$date_from" "$date_to"

# A successful source request is not a publication. Every business date must
# reconcile the staged source total to the rebuilt ledger before the shared
# daily-subject release can advance.
for date in $(seq 0 $(( ( $(date -d "$date_to" +%s) - $(date -d "$date_from" +%s) ) / 86400 ))); do
  business_date=$(date -d "$date_from +$date day" +%F)
  npx tsx src/scripts/reconcile-timo-display.ts "$business_date"
done

# current_subject_owner is published by the canonical six-sheet ownership
# chain. Timo source refresh must consume that projection, not rebuild a second
# ownership system from the external roster.
npx tsx src/scripts/publish-daily-subject-metrics.ts \
  --version="timo-${date_from}-${date_to}-$(date +%s)"

release_id=$(psql "$DATABASE_URL" -qAt -c \
  "SELECT id FROM data_publication_release WHERE domain='DAILY_SUBJECT_METRICS' AND status='PUBLISHED'")
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "DELETE FROM dashboard_cache;"
set +e
timeout 1800 ./node_modules/.bin/tsx src/scripts/rebuild-page-projections.ts
page_exit=$?
set -e
if [[ "$page_exit" -ne 0 ]]; then
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
    "UPDATE data_publication_release SET metadata=metadata||jsonb_build_object('pageProjectionState','FAILED','pageProjectionError','timo page_exit=$page_exit') WHERE id=$release_id AND status='PUBLISHED';"
  exit "$page_exit"
fi
set +e
timeout 1800 ./node_modules/.bin/tsx src/scripts/rebuild-core-report-snapshots.ts
core_report_exit=$?
set -e
if [[ "$core_report_exit" -ne 0 ]]; then
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
    "UPDATE data_publication_release SET metadata=metadata||jsonb_build_object('pageProjectionState','FAILED','pageProjectionError','timo core_report_exit=$core_report_exit') WHERE id=$release_id AND status='PUBLISHED';"
  exit "$core_report_exit"
fi
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c \
  "UPDATE data_publication_release SET metadata=metadata||'{\"pageProjectionState\":\"COMPLETE\"}'::jsonb WHERE id=$release_id AND status='PUBLISHED';
   UPDATE report_meta SET value=(CAST(value AS INTEGER)+1)::TEXT,updatedat=CURRENT_TIMESTAMP WHERE key='dataVersion';
   UPDATE report_meta SET value='$(date -u +%Y-%m-%dT%H:%M:%S.000Z)',updatedat=CURRENT_TIMESTAMP WHERE key='lastUpdatedAt';"
