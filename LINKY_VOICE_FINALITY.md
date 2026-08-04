# Linky voice-room finality

The realtime ledger is provisional until the independent BI voice-room report agrees per SID.

1. `linke_ledger_pull.py GUILD YYYYMMDD` reads every raw API page. Zero-value rows do not terminate pagination. It atomically archives response bodies and a checksum under the existing `state/` tree (`LINKE_EVIDENCE_DIR`, default `state/linky-ledger-evidence`) before the existing ledger upsert. Authorization headers, cookies and BI tickets are never persisted. The directory is mode `0700`, files are `0600`, and matching evidence files are retained for 14 days by default (`LINKE_EVIDENCE_RETENTION_DAYS`).
2. After BI download, run the read-only audit:

   ```sh
   python3 linky_voice_bi_audit.py \
     --date 2026-07-28 \
     --guild Nova-Indonesia \
     --bi-file /path/to/印尼1-Nova_语音房主播行为数据.json \
     --output /home/ubuntu/nova-auto-download/state/linky-voice-audit/2026-07-28-Nova-Indonesia.json
   ```

The audit never updates the ledger. `MATCH` allows the dashboard to show `BI已核对`; `MISMATCH` remains visible with SID count and amount delta; missing or invalid evidence remains `实时暂存，等待BI核对`.

Do not use the salary-reward report for daily revenue finality. The comparable BI fields are `active_date(day)`, `sid`, and `diamond_amount` from `语音房主播行为数据` under UTC+0.
