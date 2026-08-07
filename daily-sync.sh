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
API_DIR="/home/ubuntu/nova-backend-current/api"
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
    exit 75  # 2026-05-28: 原 exit 0 会让 bi-data-heal 误判"重下载成功"；非0=被锁挡住没真跑
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
  timeout 1500 node api-download.mjs "$DATE" 2>&1 | tail -10  # 2026-05-28: 600s→1500s，5/13 实测下载需 1021s 被 600s 砍断丢表
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
  # 发布门禁：不能再以“文件存在/文件够大”代替正确性证明。
  # 必须同时证明报表类型、业务日期、公会和核心字段正确，失败即隔离，不覆盖正式库。
  QUALITY_JSON="$DOWNLOAD_DIR/_quality_gate.json"
  log "🛡️ Step 0.8: BI 文件发布门禁..."
  if ! python3 "$SCRIPT_DIR/data-quality-gate.py" "$DOWNLOAD_DIR" "$DATE" --json-out "$QUALITY_JSON" >/tmp/bi-quality-gate.log 2>&1; then
    log "❌ BI 发布门禁失败，未导入正式库: $(tail -1 /tmp/bi-quality-gate.log)"
    exit 66
  fi
  log "  ✅ 报表类型/日期/公会/核心字段全部通过"
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
  IMPORT_EXIT=${PIPESTATUS[0]}
  if [ $IMPORT_EXIT -ne 0 ]; then
    log "❌ Excel导入失败 (exit=$IMPORT_EXIT)，停止后续核对与发布"
    exit "$IMPORT_EXIT"
  fi
fi

# ── Step 3.0.1: 语音房 API 暂存与 BI 日终核对 ─────────────
# 在现有下载链和同步锁内运行；只生成证据，不修改日账本或 publication。
log "🔎 Step 3.0.1: 核对 Linky 语音房 BI..."
if ! timeout 180 python3 "$SCRIPT_DIR/linky_voice_bi_batch.py" --date "$DATE" \
    --staging-root "/home/ubuntu/nova-data/upload-staging/daily" \
    --evidence-dir "$SCRIPT_DIR/state/linky-voice-audit"; then
  log "⚠️ 语音房 BI 核对程序失败；不生成虚假已核对状态"
fi

# 已结束业务日以语音房主播行为数据为最终事实。复用同一证据、同一日账本和
# 全局数据写锁受控落账；每次写入前生成0600快照，失败即停止后续publication。
log "🔐 Step 3.0.2: 应用 Linky 语音房 BI 最终事实..."
if ! timeout 180 python3 "$SCRIPT_DIR/linky_voice_bi_apply.py" --date "$DATE" \
    --evidence-dir "$SCRIPT_DIR/state/linky-voice-audit" \
    --snapshot-dir "$SCRIPT_DIR/state/linky-voice-repair" --apply; then
  log "❌ 语音房 BI 最终事实落账失败；停止后续publication"
  exit 68
fi
if ! timeout 180 python3 "$SCRIPT_DIR/linky_voice_bi_batch.py" --date "$DATE" \
    --staging-root "/home/ubuntu/nova-data/upload-staging/daily" \
    --evidence-dir "$SCRIPT_DIR/state/linky-voice-audit"; then
  log "❌ 语音房 BI 落账后复核失败；停止后续publication"
  exit 68
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

# ── Step 3.1b: 强制从主表md重聚合当天全部V2 (2026-05-30 根治) ──
# 原因: update-guild-v2 只覆盖部分公会+波动拦截器>50%跳变时return不写,造成V2静默缺公会
#       (5-28/5-29 漏胡萝卜/宝石/巴西2/3/4)。reaggregate-v2-fix 读md先删后写,确保 V2=md。
log "🔁 Step 3.1b: 强制重聚合 $DATE 全部 V2 (确保 V2=md, 绕过波动拦截静默缺口)..."
TZ=Asia/Shanghai timeout 150 npx tsx src/scripts/reaggregate-v2-fix.ts "$DATE" 2>&1 | tail -3
V2_REAGG_EXIT=${PIPESTATUS[0]}
if [ "$V2_REAGG_EXIT" -ne 0 ]; then
  log "❌ V2重聚合失败 (exit=$V2_REAGG_EXIT)，停止发布"
  exit "$V2_REAGG_EXIT"
fi
log "  V2 强制重聚合完成"

# ── Step 3.1c: 每日新注册主播数入库 (2026-06-01 新增) ──
# 来源: BI公会数据 new_registe_streamer_dau (服务端汇总,不截断),按 guild_config 映射 guild_name→alias 加总。
# 替代看板原"数hosts.registrationdate"反推(大公会主播明细下载截断→注册严重低估,巴西3曾183 vs 真490)。
log "📝 Step 3.1c: 入库每日新注册主播数 (BI权威口径)..."
TZ=Asia/Shanghai timeout 120 npx tsx src/scripts/ingest-guild-registrations.ts "$DATE" 2>&1 | tail -3
REG_EXIT=${PIPESTATUS[0]}
if [ "$REG_EXIT" -ne 0 ]; then
  log "❌ 注册数入库失败 (exit=$REG_EXIT)，停止发布"
  exit "$REG_EXIT"
fi
log "  注册数入库完成"

# ── Step 3.2: LATAM 聚合 ────────────────────────────────
log "🌎 Step 3.2: LATAM 聚合..."
timeout 180 npx tsx src/scripts/generate-latam-v2.ts 2>&1 | tail -2
LATAM_EXIT=${PIPESTATUS[0]}
if [ "$LATAM_EXIT" -ne 0 ]; then
  log "❌ LATAM聚合失败 (exit=$LATAM_EXIT)，停止发布"
  exit "$LATAM_EXIT"
fi
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
log "⏸️ Step 3.4: 缓存清理延后到发布确认"

# ── Step 3.5: bump dataVersion ─────────────────────────
log "⏸️ Step 3.5: dataVersion 更新延后到发布确认"
OLD_VER=$($PG "SELECT value FROM report_meta WHERE key='dataVersion';")

# Snapshot generation is part of publication, not ingestion. It now runs only
# after the source completeness gate below succeeds.
log "⏸️ Step 4: 快照生成延后到源数据完整性校验通过后"

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

MIN_COMPLETE_ROWS=${BI_MIN_ROWS:-1500}
SOURCE_VALID=0
PUBLISH_OK=0
if [ "${FINAL_COUNT:-0}" -ge "$MIN_COMPLETE_ROWS" ] && [ -z "${MISSING_REPORTS:-}" ] && [ "${GUILD_PCT:-0}" = "100.0" ]; then
  SOURCE_VALID=1
  log "  源数据状态: ✅ 完整，进入统一publication候选验证"
  if NOVA_API_DIR="$API_DIR" timeout 45m "$API_DIR/scripts/run-daily-publication.sh" \
      --version="daily-$DATE-$(date +%s)" --business-date="$DATE"; then
    PUBLISH_OK=1
    log "  发布确认: ✅ 候选验证通过并完成原子切换"
  else
    PUBLISH_EXIT=$?
    log "  发布确认: ❌ 统一publication失败(exit=$PUBLISH_EXIT)；原PUBLISHED保持或已自动恢复"
  fi
elif [ "${FINAL_COUNT:-0}" -gt 0 ]; then
  log "  源数据状态: ⚠️ 未通过动态完整性校验；不发布"
else
  log "  源数据状态: ❌ 数据库中无 $DATE 数据；不发布"
fi
log "========================================="

# ── 飞书通知 ────────────────────────────────────────────
# 通知改用飞书胖虎智能助手（feishu-notify.py）

notify_feishu() {
  local title="$1"
  local content="$2"
  # 2026-05-09 P2 Day 4 final: 补 --source / --key 让 dedupe 正常工作（不传 --channel，走默认 push 因为是真同步失败）
  python3 /home/ubuntu/nova-auto-download/feishu-notify.py "$title
$content" --source daily-sync --key "sync-status-$(date +%Y-%m-%d)" > /dev/null 2>&1
}

# 2026-04-30: 成功不再发"✅ Nova 同步完成"（每天 -1 条噪音），失败仍发
if [ "$SOURCE_VALID" -eq 1 ] && [ "$PUBLISH_OK" -eq 1 ]; then
  log "  ✅ Nova 同步完成（不再发飞书）：日期=$DATE 主播=${FINAL_COUNT} 条"
elif [ "$SOURCE_VALID" -eq 1 ]; then
  notify_feishu "❌ Nova 发布失败" "日期: $DATE | 源数据完整但快照/统一日事实未发布 | 缓存和版本未更新"
elif [ "${FINAL_COUNT:-0}" -gt 0 ]; then
  notify_feishu "⚠️ Nova 同步未通过发布校验" "日期: $DATE | ${FINAL_COUNT} 条 | 缺失公会: ${MISSING_REPORTS[*]:-无} | guild覆盖率: ${GUILD_PCT:-?}% | 不再使用固定3000行阈值"
else
  notify_feishu "❌ Nova 同步失败" "日期: $DATE | 数据库中无数据 | 缺失: ${MISSING_REPORTS[*]:-无} | 请立即检查 sync.log"
fi

# 2026-05-09 P2 Day 4 final: 删除冗余的"metrics_daily 当天数据为0"检测块
# 原因：1) 与上面 FINAL_COUNT 检测重复 2) 用 $(date -u -d "+8 hours") 算 CST 当天，
#       但 BI 数据落库本来就是昨天（因为 16:00 才有昨天数据），永远会触发 → 噪音

# ── Step 3.2.1: 经纪人归属同步 ────────────────────────────
# 2026-05-30: 去掉过时假xlsx(/home/ubuntu/运营ID.xlsx,停在4-21)的-f门控。
# 同步早已改读PG lark_chat_id_records,门控纯历史残留;删它防"假文件被删->同步静默停"。
log "👥 Step 3.2.1: 同步经纪人归属..."
timeout 120 npx tsx src/scripts/import-agent-hosts.ts 2>&1 | tail -12
AGENT_SYNC_EXIT=${PIPESTATUS[0]}
if [ "$AGENT_SYNC_EXIT" -eq 0 ]; then
  log "  ✅ 经纪人归属同步完成"
elif [ "$AGENT_SYNC_EXIT" -eq 2 ]; then
  log "  🛑 经纪人归属同步被安全阈值拦截，数据库未写入；其他数据同步继续"
else
  log "  ❌ 经纪人归属同步异常 (exit=$AGENT_SYNC_EXIT)，数据库未确认更新；其他数据同步继续"
fi

# ── Step 3.2.2: 从权威归属名单自动补齐运营登录账号 ──────────
# 只新增，不因单日源数据缺失删除或停用已有账号。
log "🔐 Step 3.2.2: 自动补齐新运营账号..."
ACCOUNT_SYNC_ENV="/home/ubuntu/.config/nova/agent-account-sync.env"
if [ -f "$ACCOUNT_SYNC_ENV" ] && [ "$(stat -c '%a' "$ACCOUNT_SYNC_ENV")" = "600" ]; then
  set -a
  source "$ACCOUNT_SYNC_ENV" >/dev/null 2>&1
  set +a
  timeout 120 npx tsx src/scripts/sync-authoritative-agent-accounts.ts 2>&1 | tail -12
  ACCOUNT_SYNC_EXIT=${PIPESTATUS[0]}
  unset DEFAULT_NEW_AGENT_PASSWORD
  if [ "$ACCOUNT_SYNC_EXIT" -eq 0 ]; then
    log "  ✅ 运营账号同步完成"
  else
    log "  ❌ 运营账号同步异常 (exit=$ACCOUNT_SYNC_EXIT)，已有账号不受影响"
  fi
else
  log "  ❌ 运营账号同步安全配置缺失或权限不是600；已有账号不受影响"
fi

# ── Step 3.3: 飞书注册目标同步 ────────────────────────────
log "📋 Step 3.3: 同步飞书注册目标..."
cd "$API_DIR" && timeout 60 npx tsx src/scripts/sync-feishu-targets.ts 2>&1 | tail -3
TARGET_EXIT=${PIPESTATUS[0]}
if [ "$TARGET_EXIT" -eq 0 ]; then
  log "  飞书目标同步完成"
else
  log "  ⚠️ 飞书目标同步失败(exit=$TARGET_EXIT)，不回滚已确认发布"
fi

# Queue workers and settlement callers must receive the publication result,
# not merely the result of the last optional follow-up command.
if [ "$PUBLISH_OK" -ne 1 ]; then
  log "❌ 流水线结束但未发布；返回失败供队列保留并重试"
  exit 67
fi
exit 0
