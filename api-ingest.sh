#!/bin/bash
# API JSON数据预处理+导入
# 用法: bash api-ingest.sh 2026-04-27
DATE=${1:?需要日期参数}
DATA_DIR="/home/ubuntu/nova-data/upload-staging/daily/$DATE"
API_DIR="/home/ubuntu/nova-dashboard-deploy-final/api"
TMP_DIR="/tmp/api-ingest-${DATE}"

echo "[api-ingest] 预处理 $DATE 的JSON数据..."

# Python预处理：提取rows，过滤，转日期格式，存为数组JSON
export API_INGEST_DATE="$DATE"
export API_INGEST_DATA_DIR="$DATA_DIR"
export API_INGEST_TMP_DIR="$TMP_DIR"

python3 << 'PYEOF'
import json, os, sys

date_arg = os.environ["API_INGEST_DATE"]
data_dir = os.environ["API_INGEST_DATA_DIR"]
tmp_dir = os.environ["API_INGEST_TMP_DIR"]
date_compact = date_arg.replace("-", "")

json_files = [f for f in os.listdir(data_dir)
              if f.endswith(".json") and ("日主播" in f or "每日主播" in f)]

if not json_files:
    print("[api-ingest] 未找到主播数据JSON文件")
    sys.exit(1)

os.makedirs(tmp_dir, exist_ok=True)
total_records = 0

for jf in sorted(json_files):
    filepath = os.path.join(data_dir, jf)
    with open(filepath) as f:
        d = json.load(f)
    rows = d.get("rows", d) if isinstance(d, dict) else d
    if not isinstance(rows, list):
        print("  %s: not valid format, skip" % jf)
        continue

    filtered = []
    for r in rows:
        if r.get("conversation_type") != "total":
            continue
        if r.get("streamer_is_off") == "Y":
            continue
        dd = str(r.get("create_date(day)", ""))
        if dd != date_compact:
            continue
        # 日期格式转换 20260427 -> 2026-04-27
        if len(dd) == 8:
            r["create_date(day)"] = dd[:4] + "-" + dd[4:6] + "-" + dd[6:8]
        # streamer_create_date也转换
        scd = str(r.get("streamer_create_date", ""))
        if len(scd) == 8 and scd.isdigit():
            r["streamer_create_date"] = scd[:4] + "-" + scd[4:6] + "-" + scd[6:8]
        filtered.append(r)

    print("  %s: %d -> %d" % (jf.replace(".json",""), len(rows), len(filtered)))

    out_path = os.path.join(tmp_dir, jf)
    with open(out_path, "w") as f:
        json.dump(filtered, f)
    total_records += len(filtered)

print("[api-ingest] total %d records, %d files" % (total_records, len(json_files)))
PYEOF

PREP_EXIT=$?
if [ $PREP_EXIT -ne 0 ] || [ ! -d "$TMP_DIR" ]; then
  echo "[api-ingest] 预处理失败"
  exit 1
fi

TMP_FILES=$(ls "$TMP_DIR"/*.json 2>/dev/null | wc -l | tr -d ' ')
if [ "$TMP_FILES" -eq 0 ]; then
  echo "[api-ingest] 预处理后无数据文件"
  rm -rf "$TMP_DIR"
  exit 1
fi

echo "[api-ingest] 开始导入 $TMP_FILES 个文件..."
cd "$API_DIR"
timeout 300 npx tsx src/scripts/batch-ingest-all.ts "$TMP_DIR" 2>&1 | tail -10
INGEST_EXIT=$?

rm -rf "$TMP_DIR"

if [ $INGEST_EXIT -ne 0 ]; then
  echo "[api-ingest] 导入失败 (exit=$INGEST_EXIT)"
  exit 1
fi

echo "[api-ingest] 完成"
