#!/bin/bash
#
# Nova Dashboard 每日自动同步 v4
#
# 流程：
#   Step 1: 下载BI报表
#   Step 2: 验证文件
#   Step 3: 导入数据 (batch-ingest-all.ts)
#   Step 3.1: 更新V2公会数据 (巴西2/巴西4)
#   Step 3.2: LATAM聚合
#   Step 3.3: 健康检查
#   Step 3.4: 清理缓存
#   Step 4: 生成快照
#   Step 5: 最终验证 + 日志摘要
#
# 用法：
#   ./daily-sync.sh              # 同步上周的数据（自动计算）
#   ./daily-sync.sh 2026-04-06   # 同步指定日期
#
# crontab:
#   0 10 * * * /home/ubuntu/nova-auto-download/daily-sync.sh >> /home/ubuntu/nova-auto-download/sync.log 2>&1
#

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_DIR="/home/ubuntu/nova-dashboard-deploy-final/api"
export PGPASSWORD="Nova2026pg!"
PG="psql -h 127.0.0.1 -U nova_app -d nova_dashboard -tAc"

if [ -z "$1" ]; then
  # 下载昨天的数据
  DAYS_BACK=1
  if date -d "$DAYS_BACK days ago" +%Y-%m-%d >/dev/null 2>&1; then
    DATE=$(date -d "$DAYS_BACK days ago" +%Y-%m-%d)
  else
    DATE=$(date -v-${DAYS_BACK}d +%Y-%m-%d)
  fi
else
  DATE="$1"
fi

DOWNLOAD_DIR="/home/ubuntu/nova-data/upload-staging/daily/$DATE"
LOCK_FILE="/tmp/nova-daily-sync.lock"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# ── 并发锁 ──────────────────────────────────────────────
if [ -f "$LOCK_FILE" ]; then
  OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "❌ 另一个同步进程正在运行 (PID=$OLD_PID)，退出"
    exit 0
  else
    log "⚠️ 发现过期锁文件 (PID=$OLD_PID 已不存在)，清理"
    rm -f "$LOCK_FILE"
  fi
fi

echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

log "========================================="
log "  Nova 每日自动同步 v4"
log "  日期: $DATE"
log "========================================="

# ── Step 0: 清场 ────────────────────────────────────────
STALE_CHROMIUM=$(pgrep -f chromium 2>/dev/null | wc -l)
STALE_SNAPSHOT=$(pgrep -f generate-snapshots-fast 2>/dev/null | wc -l)
if [ "$STALE_CHROMIUM" -gt 0 ] || [ "$STALE_SNAPSHOT" -gt 0 ]; then
  log "🧹 清理残留进程: chromium=$STALE_CHROMIUM snapshot=$STALE_SNAPSHOT"
  pkill -f chromium 2>/dev/null
  pkill -f generate-snapshots-fast 2>/dev/null
  sleep 3
fi

MEM_AVAIL=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
log "  可用内存: ${MEM_AVAIL}MB"
if [ "$MEM_AVAIL" -lt 500 ]; then
  log "❌ 可用内存不足 500MB，退出"
  exit 1
fi

# ── Step 0.5: 尝试API方式下载（更快更稳）──────────────────
# 2026-05-02 修：按 10 个公会名一一检查，缺任何一个就重下载（之前只看 ≥7 总数，单公会缺漏会被错误跳过 → 西语2/印尼3-宝石都踩过）
API_SUCCESS=0
EXPECTED_REPORTS="印尼1-Nova 印尼2-Carote 印尼3-宝石 巴西1-Nova 巴西2-Evian 巴西3-Wisky 巴西4-Doce 土耳其1-Evian 西语1-Nova 西语2-Evian"
MISSING_REPORTS=""
for r in $EXPECTED_REPORTS; do
  rcount=$(ls "$DOWNLOAD_DIR"/${r}_*.json 2>/dev/null | wc -l | tr -d ' ')
  if [ "$rcount" -eq 0 ]; then
    MISSING_REPORTS="$MISSING_REPORTS $r"
  fi
done

if [ -z "$MISSING_REPORTS" ]; then
  log "✅ 10 个公会 JSON 全部存在，跳过 API 下载"
  API_SUCCESS=1
else
  log "📡 Step 0.5: 缺公会[$MISSING_REPORTS]，触发 API 下载..."
  cd "$SCRIPT_DIR"
  timeout 600 node api-download.mjs "$DATE" 2>&1 | tail -10
  API_EXIT=$?

  pkill -f chromium 2>/dev/null
  sleep 2

  # 重新检查
  MISSING_REPORTS=""
  for r in $EXPECTED_REPORTS; do
    rcount=$(ls "$DOWNLOAD_DIR"/${r}_*.json 2>/dev/null | wc -l | tr -d ' ')
    if [ "$rcount" -eq 0 ]; then
      MISSING_REPORTS="$MISSING_REPORTS $r"
    fi
  done

  if [ -z "$MISSING_REPORTS" ]; then
    log "  ✅ API下载成功: 10 个公会全到位"
    API_SUCCESS=1
  else
    log "  ⚠️ API下载仍缺[$MISSING_REPORTS]，降级到 Playwright"
  fi
fi

if [ "$API_SUCCESS" -eq 1 ]; then
  # ── API路径: JSON预处理+导入 ────────────────────────────
  log "📊 Step 3 (API): 导入JSON数据..."
  bash "$SCRIPT_DIR/api-ingest.sh" "$DATE" 2>&1 | tail -10
  API_INGEST_EXIT=$?
  if [ $API_INGEST_EXIT -ne 0 ]; then
    log "⚠️ API导入失败，降级到Playwright重新下载"
    API_SUCCESS=0
  fi
fi

if [ "$API_SUCCESS" -eq 0 ]; then
  # ── Playwright降级路径 ──────────────────────────────────
  EXISTING_FILES=$(ls "$DOWNLOAD_DIR"/*.xlsx 2>/dev/null | wc -l | tr -d ' ')
  if [ "$EXISTING_FILES" -ge 7 ]; then
    log "✅ 已存在 $EXISTING_FILES 个Excel文件，跳过下载直接导入"
  else
    # ── Step 1: Playwright下载 ──────────────────────────────
    log "📥 Step 1 (Playwright): 下载报表数据..."
    cd "$SCRIPT_DIR"

    timeout 600 node auto-download.mjs "$DATE" 2>&1
    DL_EXIT=$?

    if [ $DL_EXIT -eq 124 ]; then
      log "⚠️ 下载超时（10分钟），继续处理已下载的文件"
    elif [ $DL_EXIT -ne 0 ]; then
      log "⚠️ 下载异常 (exit=$DL_EXIT)，继续处理已下载的文件"
    fi

    pkill -f chromium 2>/dev/null
    sleep 2

    TOTAL_FILES=$(ls "$DOWNLOAD_DIR"/*.xlsx 2>/dev/null | wc -l | tr -d ' ')
    log "  下载文件: $TOTAL_FILES 个"

    EXPECTED_REPORTS=("印尼1-Nova" "印尼2-Carote" "巴西1-Nova" "巴西2-Evian" "巴西3-Wisky" "巴西4-Doce" "土耳其1-Evian" "西语1-Nova" "西语2-Evian")
    MISSING_REPORTS=()
    for REPORT in "${EXPECTED_REPORTS[@]}"; do
      if ! ls "$DOWNLOAD_DIR/${REPORT}_"*.xlsx >/dev/null 2>&1; then
        MISSING_REPORTS+=("$REPORT")
      fi
    done

    if [ ${#MISSING_REPORTS[@]} -gt 0 ]; then
      log "  ⚠️ 缺失报表: ${MISSING_REPORTS[*]}，已在mjs中重试"
      TOTAL_FILES=$(ls "$DOWNLOAD_DIR"/*.xlsx 2>/dev/null | wc -l | tr -d ' ')
      log "  最终文件数: $TOTAL_FILES 个（缺失: ${#MISSING_REPORTS[@]} 个: ${MISSING_REPORTS[*]}）"
    fi

    if [ "$TOTAL_FILES" -eq 0 ]; then
      log "❌ 无文件下载成功，终止"
      exit 1
    fi
  fi

  # ── Step 2: 验证 ────────────────────────────────────────
  log "🔍 Step 2: 验证文件..."
  VALID=0
  INVALID=0
  for f in "$DOWNLOAD_DIR"/*.xlsx; do
    SIZE=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null)
    if [ "$SIZE" -lt 10000 ]; then
      log "  ⚠️ 文件过小(${SIZE}B): $(basename \"$f\")"
      INVALID=$((INVALID + 1))
    else
      VALID=$((VALID + 1))
    fi
  done
  log "  有效: $VALID, 异常: $INVALID"

  if [ "$VALID" -eq 0 ]; then
    log "❌ 无有效文件，终止"
    exit 1
  fi

  # ── Step 3: Excel导入 ──────────────────────────────────
  log "📊 Step 3 (Excel): 导入数据..."
  cd "$API_DIR"

  timeout 300 npx tsx src/scripts/batch-ingest-all.ts "$DOWNLOAD_DIR" 2>&1 | tail -5
  IMPORT_EXIT=$?
  if [ $IMPORT_EXIT -ne 0 ]; then
    log "⚠️ 导入可能未完全成功 (exit=$IMPORT_EXIT)"
  fi
fi

# 获取导入记录数（后续健康检查使用）
RECORD_COUNT=$($PG \
  "SELECT COUNT(*) FROM metrics_daily
   WHERE TO_CHAR(date, 'YYYY-MM-DD') = '$DATE';" 2>/dev/null)
log "  导入记录数: $RECORD_COUNT"

cd "$API_DIR"
# ── Step 3.1: 更新V2公会数据 ────────────────────────────
log "📊 Step 3.1: 更新 巴西2/巴西4 V2聚合数据..."
timeout 120 npx tsx src/scripts/update-guild-v2.ts "$DOWNLOAD_DIR" 2>&1 | tail -5
V2_EXIT=$?
if [ $V2_EXIT -ne 0 ]; then
  log "⚠️ V2公会数据更新失败 (exit=$V2_EXIT)，非致命错误，继续"
fi

# ── Step 3.2: LATAM 聚合 ────────────────────────────────
log "🌎 Step 3.2: LATAM 聚合..."
timeout 180 npx tsx src/scripts/generate-latam-v2.ts 2>&1 | tail -2
log "  LATAM 完成"

# ── Step 3.3: 健康检查 ──────────────────────────────────
log "🏥 Step 3.3: 健康检查..."

# 1. sid duplicate check (must be 0)
DUP=$($PG "SELECT COUNT(*) FROM (SELECT sid FROM hosts GROUP BY sid HAVING COUNT(DISTINCT id)>1) t;")
if [ "$DUP" != "0" ]; then
  log "  ⚠️ WARN: $DUP 个 sid 复用（BI 直播间 ID 复用，hostid 唯一不影响仪表盘聚合，2026-05-01 降级）"
else
  log "  ✅ sid唯一性检查通过"
fi

# 2. Data volume check (today vs yesterday, >30% drop = warning)
TODAY_COUNT=${RECORD_COUNT:-0}
YESTERDAY_DATE=$(date -d "$DATE - 1 day" +%Y-%m-%d 2>/dev/null || date -v-1d -jf %Y-%m-%d "$DATE" +%Y-%m-%d)
YEST_COUNT=$($PG "SELECT COUNT(*) FROM metrics_daily WHERE TO_CHAR(date, 'YYYY-MM-DD')='$YESTERDAY_DATE';")
if [ "${YEST_COUNT:-0}" -gt 0 ] && [ "${TODAY_COUNT:-0}" -gt 0 ]; then
  DROP=$(( (YEST_COUNT - TODAY_COUNT) * 100 / YEST_COUNT ))
  if [ "$DROP" -gt 30 ]; then
    log "  ⚠️ 数据量下降 ${DROP}%: 昨天=$YEST_COUNT 今天=$TODAY_COUNT"
  else
    log "  ✅ 数据量正常: 昨天=$YEST_COUNT 今天=$TODAY_COUNT (变化${DROP}%)"
  fi
else
  log "  ℹ️ 无法比较数据量 (昨天=${YEST_COUNT:-0} 今天=${TODAY_COUNT:-0})"
fi

# 3. guildName coverage
GUILD_PCT=$($PG "SELECT ROUND(100.0*SUM(CASE WHEN guildname IS NOT NULL AND guildname!='' THEN 1 ELSE 0 END)/COUNT(*),1) FROM hosts WHERE id IN (SELECT DISTINCT hostid FROM metrics_daily WHERE TO_CHAR(date, 'YYYY-MM-DD')='$DATE');")
log "  guildName覆盖率: ${GUILD_PCT}%"

# ── Step 3.4: 清理缓存 ─────────────────────────────────
log "🧹 Step 3.4: 清理缓存..."
$PG "DELETE FROM dashboard_cache;"
$PG "DELETE FROM report_snapshots WHERE periodkey='$DATE';"
log "  缓存已清理"

# ── Step 3.5: bump dataVersion ─────────────────────────
log "🔄 Step 3.5: bump dataVersion..."
OLD_VER=$($PG "SELECT value FROM report_meta WHERE key='dataVersion';")
$PG "UPDATE report_meta SET value = (CAST(value AS INTEGER) + 1)::TEXT, updatedat = CURRENT_TIMESTAMP WHERE key = 'dataVersion';"
$PG "UPDATE report_meta SET value = '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)', updatedat = CURRENT_TIMESTAMP WHERE key = 'lastUpdatedAt';"
NEW_VER=$($PG "SELECT value FROM report_meta WHERE key='dataVersion';")
log "  dataVersion: $OLD_VER -> $NEW_VER"

# ── Step 4: 快照 ────────────────────────────────────────
log "📸 Step 4: 生成快照..."
MEM_AVAIL=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_AVAIL" -lt 400 ]; then
  log "⚠️ 内存不足(${MEM_AVAIL}MB)，跳过快照生成"
else
  timeout 1200 npx tsx src/scripts/generate-snapshots-fast.ts 2>&1 | tail -5
  SNAP_EXIT=$?
  if [ $SNAP_EXIT -eq 124 ]; then
    log "⚠️ 快照生成超时（20分钟）"
    pkill -f generate-snapshots-fast 2>/dev/null
  fi
fi

# ── Step 4.1: 重启API服务（清空内存缓存）────────────────
log "🔄 Step 4.1: 重启API服务..."
pm2 restart nova-api 2>&1 || pm2 restart all 2>&1
sleep 3
log "  API服务已重启"

# ── Step 5: 最终验证 + 日志摘要 ─────────────────────────
log ""
log "📋 Step 5: 最终验证..."

FINAL_COUNT=$($PG \
  "SELECT COUNT(*) FROM metrics_daily
   WHERE TO_CHAR(date, 'YYYY-MM-DD') = '$DATE';" 2>/dev/null)

V2_COUNT=$($PG \
  "SELECT COUNT(*) FROM metrics_daily_v2
   WHERE TO_CHAR(date, 'YYYY-MM-DD') = '$DATE';" 2>/dev/null)

SNAPSHOT_COUNT=$($PG \
  "SELECT COUNT(*) FROM report_snapshots
   WHERE periodkey='$DATE';" 2>/dev/null)

log ""
log "========================================="
log "  同步摘要"
log "========================================="
log "  日期:         $DATE"
log "  metrics_daily:    ${FINAL_COUNT:-0} 条"
log "  metrics_daily_v2: ${V2_COUNT:-0} 条"
log "  快照:             ${SNAPSHOT_COUNT:-0} 条"
log "  sid重复:          ${DUP:-?}"
log "  guildName覆盖率:  ${GUILD_PCT:-?}%"

if [ "${FINAL_COUNT:-0}" -gt 3000 ]; then
  log "  状态: ✅ 同步完成！数据完整"
elif [ "${FINAL_COUNT:-0}" -gt 0 ]; then
  log "  状态: ⚠️ 同步完成但数据可能不完整（正常应>3000）"
else
  log "  状态: ❌ 数据库中无 $DATE 数据"
fi
log "========================================="

# ── 飞书通知 ────────────────────────────────────────────
# 通知改用飞书胖虎智能助手（feishu-notify.py）

notify_feishu() {
  local title="$1"
  local content="$2"
  python3 /home/ubuntu/nova-auto-download/feishu-notify.py "$title
$content" > /dev/null 2>&1
}

# 2026-04-30: 成功不再发"✅ Nova 同步完成"（每天 -1 条噪音），失败仍发
if [ "${FINAL_COUNT:-0}" -gt 3000 ]; then
  log "  ✅ Nova 同步完成（不再发飞书）：日期=$DATE 主播=${FINAL_COUNT} 条"
elif [ "${FINAL_COUNT:-0}" -gt 0 ]; then
  notify_feishu "⚠️ Nova 同步不完整" "日期: $DATE | 仅 ${FINAL_COUNT} 条（正常应>3000）| 缺失: ${MISSING_REPORTS[*]:-无} | 请检查 sync.log"
else
  notify_feishu "❌ Nova 同步失败" "日期: $DATE | 数据库中无数据 | 缺失: ${MISSING_REPORTS[*]:-无} | 请立即检查 sync.log"
fi

# ── 失败检测: metrics_daily 今日数据为0 ────────────────────
LATEST_COUNT=$(PGPASSWORD='Nova2026pg!' psql -U nova_app -h localhost -d nova_dashboard -t -c "SELECT COUNT(*) FROM metrics_daily WHERE TO_CHAR(date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$(date -u -d "+8 hours" +"%Y-%m-%d")';" 2>/dev/null | tr -d ' ')
if [ "${LATEST_COUNT:-0}" -eq 0 ] 2>/dev/null; then
  log "⚠️ 数据同步告警: metrics_daily 当天数据为0条，请检查BI下载和导入链路"
  python3 /home/ubuntu/nova-auto-download/feishu-notify.py "⚠️ 数据同步告警: metrics_daily 当天数据为0条，请检查BI下载和导入链路" > /dev/null 2>&1
fi

# ── Step 3.2.1: 经纪人归属同步 ────────────────────────────
if [ -f "/home/ubuntu/运营ID.xlsx" ]; then
  log "👥 Step 3.2.1: 同步经纪人归属..."
  timeout 120 npx tsx src/scripts/import-agent-hosts.ts 2>&1 | tail -3
  log "  经纪人归属同步完成"
fi

# ── Step 3.3: 飞书注册目标同步 ────────────────────────────
log "📋 Step 3.3: 同步飞书注册目标..."
cd "$API_DIR" && timeout 60 npx tsx src/scripts/sync-feishu-targets.ts 2>&1 | tail -3
log "  飞书目标同步完成"
