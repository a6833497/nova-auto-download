#!/bin/bash
#
# BI 数据自愈守护 v2（2026-05-01 升级，公会粒度）
#
# v1 痛点：阈值是 metrics_daily 总行数 < 3000，单公会缺漏（如 Evian✨ 700+）检测不到
# v2 改进：每个 active 公会昨日 hosts < 昨前一天 × 0.5 视为缺漏，触发自愈或告警
#
# cron 时点（CST）：
#   30 16 * * *  第 1 次（daily-sync 30 分钟后 grace period）
#   0  17 * * *  第 2 次（如果还缺）
#   30 17 * * *  daily-audit 兜底也含自愈
#
# 阈值：
#   - 总量：metrics_daily 昨日 < 3000 行 → 总缺
#   - 公会：active 公会昨日主播数 < 昨前一天 × 50% → 单公会缺

set -u

LOG=/tmp/bi-heal.log
NOTIFY=/home/ubuntu/nova-auto-download/feishu-notify.py
DAILY_SYNC=/home/ubuntu/nova-auto-download/daily-sync.sh
ATTEMPT_FILE=/tmp/bi-heal-attempts.txt
TOTAL_THRESHOLD=3000
GUILD_DROP_RATIO=0.5

PSQL_BIN='PGPASSWORD=Nova2026pg! psql -U nova_app -h localhost -d nova_dashboard -t -A'

log() {
  echo "[$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

run_sql() {
  # 用 stdin 喂 SQL，避免 bash 双层引号转义
  eval "$PSQL_BIN" -c "\"$1\"" 2>&1
}

run_sql_stdin() {
  # 通过 stdin 喂多行 SQL，保留引号
  PGPASSWORD='Nova2026pg!' psql -U nova_app -h localhost -d nova_dashboard -t -A -f /dev/stdin 2>&1
}

DATE_YESTERDAY=$(TZ=Asia/Shanghai date -d 'yesterday' +%Y-%m-%d)
DATE_DAY_BEFORE=$(TZ=Asia/Shanghai date -d '2 days ago' +%Y-%m-%d)
TODAY=$(TZ=Asia/Shanghai date +%Y-%m-%d)

# ── 检查 1：总量
TOTAL_COUNT=$(run_sql "SELECT COUNT(*) FROM metrics_daily WHERE TO_CHAR(date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_YESTERDAY';" | tr -d ' ')
log "[heal] 检查 $DATE_YESTERDAY 总量: ${TOTAL_COUNT:-0} 行 (阈值 $TOTAL_THRESHOLD)"

NEEDS_HEAL=0
HEAL_REASON=""

if [ "${TOTAL_COUNT:-0}" -lt "$TOTAL_THRESHOLD" ]; then
  NEEDS_HEAL=1
  HEAL_REASON="总量 $TOTAL_COUNT < $TOTAL_THRESHOLD"
fi

# ── 检查 2：公会粒度（用 stdin 喂 SQL 避免引号转义）
GUILD_DROPS=$(run_sql_stdin <<SQL
WITH active_guilds AS (
  SELECT "guildAlias", "dbGuildNames" FROM guild_config WHERE "isActive" = 1
),
yest AS (
  SELECT gc."guildAlias" AS alias, COUNT(DISTINCT m.hostid) AS hosts
  FROM active_guilds gc
  LEFT JOIN hosts h ON h.guildname = ANY(string_to_array(gc."dbGuildNames", ','))
  LEFT JOIN metrics_daily m ON m.hostid = h.id AND TO_CHAR(m.date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_YESTERDAY'
  GROUP BY gc."guildAlias"
),
prev AS (
  SELECT gc."guildAlias" AS alias, COUNT(DISTINCT m.hostid) AS hosts
  FROM active_guilds gc
  LEFT JOIN hosts h ON h.guildname = ANY(string_to_array(gc."dbGuildNames", ','))
  LEFT JOIN metrics_daily m ON m.hostid = h.id AND TO_CHAR(m.date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_DAY_BEFORE'
  GROUP BY gc."guildAlias"
)
SELECT y.alias || ':' || y.hosts || '/' || p.hosts
FROM yest y JOIN prev p ON y.alias = p.alias
WHERE p.hosts > 50 AND y.hosts < p.hosts * $GUILD_DROP_RATIO
ORDER BY y.alias;
SQL
)

if [ -n "$GUILD_DROPS" ]; then
  NEEDS_HEAL=1
  HEAL_REASON="${HEAL_REASON:+$HEAL_REASON; }公会缺: $(echo "$GUILD_DROPS" | tr '\n' ',' | sed 's/,$//')"
  log "[heal] ⚠️ 检测到公会粒度缺漏: $GUILD_DROPS"
fi

if [ "$NEEDS_HEAL" -eq 0 ]; then
  log "[heal] ✅ 数据完整（总量+公会全过），跳过自愈"
  rm -f "$ATTEMPT_FILE"
  exit 0
fi

# ── 记录尝试次数
LAST_DAY=$(head -1 "$ATTEMPT_FILE" 2>/dev/null | awk '{print $1}')
if [ "${LAST_DAY:-}" != "$TODAY" ]; then
  echo "$TODAY 0" > "$ATTEMPT_FILE"
fi
ATTEMPTS=$(awk '{print $2}' "$ATTEMPT_FILE")
ATTEMPTS=$((${ATTEMPTS:-0} + 1))
echo "$TODAY $ATTEMPTS" > "$ATTEMPT_FILE"

log "[heal] ⚠️ 数据不完整（第 $ATTEMPTS 次自愈）：$HEAL_REASON"

# ── 关键：清空当日 JSON 缓存，强制 daily-sync 重下载
# v2.1 修复（2026-05-01）：之前 daily-sync 看到「已存在 N 个 JSON」会跳过下载，导致 heal 触发也是 ingest 旧 JSON 没新数据
DAILY_JSON_DIR="/home/ubuntu/nova-data/upload-staging/daily/$DATE_YESTERDAY"
if [ -d "$DAILY_JSON_DIR" ]; then
  JSON_COUNT=$(ls "$DAILY_JSON_DIR"/*.json 2>/dev/null | wc -l)
  if [ "$JSON_COUNT" -gt 0 ]; then
    log "[heal] 清空 $DAILY_JSON_DIR 共 $JSON_COUNT 个 JSON 缓存，强制重下载"
    rm -f "$DAILY_JSON_DIR"/*.json
  fi
fi

log "[heal] 触发 daily-sync.sh $DATE_YESTERDAY 重下载"

# ── 触发重下载（指定日期参数）
timeout 1800 bash "$DAILY_SYNC" "$DATE_YESTERDAY" >> "$LOG" 2>&1
SYNC_RC=$?
log "[heal] daily-sync.sh 退出码 $SYNC_RC"

# ── 验证（总量 + 公会粒度都要过）
sleep 5
TOTAL_COUNT_AFTER=$(run_sql "SELECT COUNT(*) FROM metrics_daily WHERE TO_CHAR(date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_YESTERDAY';" | tr -d ' ')
log "[heal] 自愈后 $DATE_YESTERDAY 总量: ${TOTAL_COUNT_AFTER:-0} 行"

# 自愈后再查一次公会粒度
GUILD_DROPS_AFTER=$(run_sql_stdin <<SQL
WITH active_guilds AS (
  SELECT "guildAlias", "dbGuildNames" FROM guild_config WHERE "isActive" = 1
),
yest AS (
  SELECT gc."guildAlias" AS alias, COUNT(DISTINCT m.hostid) AS hosts
  FROM active_guilds gc
  LEFT JOIN hosts h ON h.guildname = ANY(string_to_array(gc."dbGuildNames", ','))
  LEFT JOIN metrics_daily m ON m.hostid = h.id AND TO_CHAR(m.date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_YESTERDAY'
  GROUP BY gc."guildAlias"
),
prev AS (
  SELECT gc."guildAlias" AS alias, COUNT(DISTINCT m.hostid) AS hosts
  FROM active_guilds gc
  LEFT JOIN hosts h ON h.guildname = ANY(string_to_array(gc."dbGuildNames", ','))
  LEFT JOIN metrics_daily m ON m.hostid = h.id AND TO_CHAR(m.date AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD') = '$DATE_DAY_BEFORE'
  GROUP BY gc."guildAlias"
)
SELECT y.alias || ':' || y.hosts || '/' || p.hosts
FROM yest y JOIN prev p ON y.alias = p.alias
WHERE p.hosts > 50 AND y.hosts < p.hosts * $GUILD_DROP_RATIO
ORDER BY y.alias;
SQL
)

if [ "${TOTAL_COUNT_AFTER:-0}" -ge "$TOTAL_THRESHOLD" ] && [ -z "$GUILD_DROPS_AFTER" ]; then
  python3 "$NOTIFY" "✅ BI 数据自愈成功
日期: $DATE_YESTERDAY
现行数: ${TOTAL_COUNT_AFTER} 行（总量+全部公会齐）
原因: $HEAL_REASON
本次第 $ATTEMPTS 次尝试" > /dev/null 2>&1
  log "[heal] ✅ 自愈成功（总量+公会粒度均通过）"
  rm -f "$ATTEMPT_FILE"
  exit 0
fi

if [ -n "$GUILD_DROPS_AFTER" ]; then
  log "[heal] ⚠️ 自愈后仍有公会缺漏: $GUILD_DROPS_AFTER"
fi

# ── critical 触发条件（2026-05-08 Phase 1.0 修复）
# 历史 bug：cron 一天只跑 16:30 + 17:00 两次，ATTEMPTS 当日最多 = 2，
#         原 `>= 3` 阈值永远不满足 → critical 永远不发出（5-3~5-7 缺漏期间没人收到一条告警）。
# 修复：阈值降为 `>= 2 && hour >= 17`（最后一次 cron 重试后还没愈合就告警）
#       业务规则保持：BI 最迟 16:00 昨天数据完整，17:00 后还缺 = 真异常
CURRENT_HOUR=$(TZ=Asia/Shanghai date +%H)
# 把 leading 0 转 base-10 整数，避免 `08` 被当 octal
CURRENT_HOUR_INT=$((10#$CURRENT_HOUR))
if [ "${ATTEMPTS:-0}" -ge 2 ] && [ "$CURRENT_HOUR_INT" -ge 17 ]; then
  log "[heal] 🚨 自愈连续 $ATTEMPTS 次失败（且当前 ${CURRENT_HOUR}:00 >= 17:00 BI 应已完整），发 critical"
  python3 "$NOTIFY" "🚨 [BI 自愈失败 - 需人工干预]
日期: $DATE_YESTERDAY
现行数: ${TOTAL_COUNT_AFTER} 行
原因: $HEAL_REASON
已重试: $ATTEMPTS 次（当前 ${CURRENT_HOUR}:00 BI 应已完整）

诊断方向:
1) BI session 是否过期？看 /tmp/bi-session.json mtime；过期跑 node get-bi-session.mjs <idx> 刷新
2) tail -50 /home/ubuntu/nova-auto-download/sync.log 看下载错误（找 ERR_ABORTED）
3) 看 /tmp/bi-heal.log 完整自愈日志
4) BI 网站可达性 + accessTicket 是否过期
9 个报表 idx：0印尼1 1印尼2 2巴西1 3巴西2 4巴西3 5巴西4 6土耳其1 7西语1 8西语2" > /dev/null 2>&1
elif [ "${ATTEMPTS:-0}" -ge 2 ]; then
  log "[heal] ⏰ 连续 $ATTEMPTS 次失败但当前 ${CURRENT_HOUR}:00 < 17:00（BI 最后一次重试还没到），暂不发 critical 等 17:00 后重试"
fi
exit 1
