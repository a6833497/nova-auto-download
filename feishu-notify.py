#!/usr/bin/env python3
"""
统一通知：飞书胖虎助理群推送（牛马中台 P2 Day 1，2026-05-09）

升级特性：
  --level P0/P1/P2/P3   (默认 P1)
  --source <name>       (默认 unknown，必须用于 dedupe)
  --key <key>           (默认 None；同 source+key 24h dedupe)
  --channel push/digest (默认 push；digest=仅写仪表盘不推飞书+不参与dedupe+无视静默)

行为矩阵：
  P0：无视静默时段+dedupe，推机器人，PG history 标记 pushed=TRUE，标题加「[紧急]」🔴
  P1：09:00-22:00 推机器人；同 (source,key) 24h dedupe；超出静默时段或被 dedupe → 写 history pushed=FALSE
  P2：永不推机器人（除非未指定 --level，老调用兼容仍当 P1），写 history pushed=FALSE
  P3：连 PG 也不写，stdout 而已

channel=digest 模式（P2 Day4 新增）：
  无论 --level 取值，强制不推飞书机器人，但写 alert_history 标记 channel='digest' pushed=FALSE。
  不参与 dedupe（每次都写一条），不受静默时段限制，仪表盘 /audit-log 看完整历史。
  默认 channel='push' 向下兼容老调用。

向下兼容：原调用 `python3 feishu-notify.py "xxx"` 等价于 --level P1 --source unknown --channel push，
         没有 --key 时退化用 sha256(text)[:16] 做 dedupe，仍然能防刷屏。

PG：alert_dedupe (source,key,last_sent_at,count,last_text)
    alert_history (id, ts, level, source, key, text, pushed, channel)
"""
import json
import urllib.request
import sys
import os
import argparse
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta

CONFIG_PATH = "/home/ubuntu/feishu-sync/config.json"
PG_USER = "nova_app"
PG_DB = "nova_dashboard"
PG_PASS = "Nova2026pg!"
PG_HOST = "localhost"

CST = timezone(timedelta(hours=8))


# ════════════════════════════════════════════════════════
# 中文化翻译层（2026-05-19 加，CEO 反馈告警一堆英文看不懂）
# 规则：
#   1) 字面 \n 替换成真换行（JSON 跨进程传过来常是字面 \n）
#   2) 英文字段名 / 表名 / 技术缩写词按 FIELD_TRANSLATIONS 替换
#   3) 长 key 优先 + 词边界匹配，避免短 key 误伤长 key（先匹配
#      directTotalDailyOutput 再匹配 operator）
# ════════════════════════════════════════════════════════
import re as _re

FIELD_TRANSLATIONS = {
    # —— Lark / BI 业务字段（最常出现在 import-lark-all 异常里）——
    "directTotalDailyOutput":      "直属粉日总产出",
    "directTotalWeeklyOutput":     "直属粉周总产出",
    "guildTotalDailyOutput":       "公会日总产出",
    "guildTotalWeeklyOutput":      "公会周总产出",
    "directSUser":                 "直属粉 S 用户数",
    "guildSUser":                  "公会 S 用户数",
    "paiddiamondtotal":            "付费钻石总计",
    "paiddiamondstotal":           "付费钻石总计",
    "all_diamond_amount":          "钻石总额",
    "operator":                    "操作员归属",
    "guild":                       "公会",
    "country":                     "国家区域",
    "header":                      "表头",
    "headers":                     "表头",
    "date":                        "日期",
    # —— 数据库表名 ——
    "lark_daily_kpi":              "Lark 日报指标表",
    "lark_weekly_kpi":             "Lark 周报指标表",
    "lark_monthly_kpi":            "Lark 月报指标表",
    "lark_operator_daily":         "Lark 运营日报表",
    "lark_operator_weekly":        "Lark 运营周报表",
    "metrics_daily_v2":            "主播日数据表 V2",
    "metrics_daily":               "主播日数据表",
    "host_agent":                  "经纪人归属表",
    "lark_chat_id_records":        "Lark 归属登记表",
    "feishu_finance_detail":       "飞书财务明细表",
    "guild_targets":               "公会目标表",
    "alert_history":               "告警历史表",
    "alert_dedupe":                "告警去重表",
    "sync_status":                 "同步状态表",
    # —— 技术缩写词 ——
    "schema":                      "表结构",
    "Schema":                      "表结构",
    "CSV":                         "数据文件（CSV）",
    "JSON":                        "数据（JSON）",
    "API":                         "接口",
    "SQL":                         "数据库查询",
    "PostgreSQL":                  "数据库（PG）",
    "Postgres":                    "数据库（PG）",
    "log":                         "日志",
    "logs":                        "日志",
    "bug":                         "错误",
    "fingerprint":                 "指纹",
    "Lark":                        "Lark（多维表格）",
}

# 长 key 优先匹配，避免短 key 误伤
_TRANSLATION_KEYS_SORTED = sorted(FIELD_TRANSLATIONS.keys(), key=len, reverse=True)


def humanize_alert(text: str) -> str:
    """把含 \\n 字面、英文字段名、技术术语的告警文本翻成"人话"。

    用于 send_feishu() 推送前 + write_history() 落库前。
    保留专有名词（公会名 / 平台名）原样不动。
    """
    if not text:
        return text
    s = text
    # 1) 字面 \n（反斜杠 + n）→ 真换行
    s = s.replace("\\n", "\n")
    # 2) 英文字段 / 技术缩写词 → 中文。词边界匹配
    for k in _TRANSLATION_KEYS_SORTED:
        zh = FIELD_TRANSLATIONS[k]
        pattern = r"\b" + _re.escape(k) + r"\b"
        s = _re.sub(pattern, zh, s)
    return s


def format_alert_4sections(title: str,
                           what_happened: str,
                           what_missing: str,
                           who_fix: str,
                           consequence: str,
                           date_str: str = None) -> str:
    """4 段傻瓜化告警模板。所有给 CEO 看的告警按这个固定格式发。

    输出示例：
      🚨 巴西运营日报没导入（2026-05-19）

      发生了什么：
      导入巴西区运营日报失败，飞书源头表少了关键字段

      具体缺/错：
      操作员归属 / 直属粉日总产出 等 7 个字段

      谁来修：
      数据团队 - 巴西区负责人

      不修的后果：
      巴西区运营日报每天都空，影响 CEO 看巴西大盘
    """
    if date_str is None:
        date_str = datetime.now(CST).strftime("%Y-%m-%d")
    text = (
        f"🚨 {title}（{date_str}）\n\n"
        f"发生了什么：\n{what_happened}\n\n"
        f"具体缺/错：\n{what_missing}\n\n"
        f"谁来修：\n{who_fix}\n\n"
        f"不修的后果：\n{consequence}"
    )
    return humanize_alert(text)


def cst_hour() -> int:
    return datetime.now(CST).hour


def is_silent_hours() -> bool:
    """22:00-08:00 CST 静默时段"""
    h = cst_hour()
    return h >= 22 or h < 8


def is_offwork_hours() -> bool:
    """非工作时段：22:00-09:00 CST（P1 不推）"""
    h = cst_hour()
    return h >= 22 or h < 9


def pg_exec(sql: str, params: list = None) -> str:
    """通过 psql 执行 SQL，返回 stdout（容错：失败时返回 ''）"""
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASS
    args = ["psql", "-U", PG_USER, "-h", PG_HOST, "-d", PG_DB, "-t", "-A", "-q"]
    if params:
        # 用 prepared 形式更安全；这里简化用 -v 替换
        # 由于参数复杂，直接构造 escape 字符串
        args.extend(["-c", sql])
    else:
        args.extend(["-c", sql])
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        return (result.stdout or "").strip()
    except Exception as e:
        print(f"[feishu-notify pg_exec error] {e}", file=sys.stderr)
        return ""


def pg_escape(s: str) -> str:
    return (s or "").replace("'", "''")


def check_dedupe(source: str, key: str) -> tuple:
    """返回 (is_hit: bool, current_count: int)"""
    s = pg_escape(source)
    k = pg_escape(key)
    sql = (
        f"SELECT count, EXTRACT(EPOCH FROM (NOW() - last_sent_at))::int "
        f"FROM alert_dedupe WHERE source='{s}' AND key='{k}'"
    )
    out = pg_exec(sql)
    if not out:
        return (False, 0)
    parts = out.split("|")
    if len(parts) < 2:
        return (False, 0)
    try:
        cnt = int(parts[0])
        secs = int(parts[1])
    except ValueError:
        return (False, 0)
    # 24h = 86400s
    if secs < 86400:
        return (True, cnt)
    return (False, cnt)


def update_dedupe(source: str, key: str, text: str, sent: bool):
    """更新 dedupe 表：sent=True 时刷新 last_sent_at；sent=False 仅 count++"""
    s = pg_escape(source)
    k = pg_escape(key)
    t = pg_escape(text[:500])
    if sent:
        sql = (
            f"INSERT INTO alert_dedupe(source,key,last_sent_at,count,last_text) "
            f"VALUES('{s}','{k}',NOW(),1,'{t}') "
            f"ON CONFLICT(source,key) DO UPDATE SET last_sent_at=NOW(), count=alert_dedupe.count+1, last_text='{t}'"
        )
    else:
        sql = (
            f"INSERT INTO alert_dedupe(source,key,last_sent_at,count,last_text) "
            f"VALUES('{s}','{k}',NOW(),1,'{t}') "
            f"ON CONFLICT(source,key) DO UPDATE SET count=alert_dedupe.count+1, last_text='{t}'"
        )
    pg_exec(sql)


def write_history(level: str, source: str, key: str, text: str, pushed: bool, channel: str = "push"):
    # 2026-05-19：落库前先翻译成"人话"，仪表盘 /audit-log 直接显示中文
    text = humanize_alert(text)
    s = pg_escape(source)
    k = pg_escape(key or "")
    t = pg_escape(text[:4000])
    lvl = pg_escape(level)
    ch = pg_escape(channel or "push")
    pushed_v = "TRUE" if pushed else "FALSE"
    sql = (
        f"INSERT INTO alert_history(level,source,key,text,pushed,channel) "
        f"VALUES('{lvl}','{s}','{k}','{t}',{pushed_v},'{ch}')"
    )
    pg_exec(sql)


FAILURE_LOG = "/tmp/feishu-notify-failures.log"


def log_push_failure(reason: str, text: str):
    """A++ 步 3 新增：往 /tmp/feishu-notify-failures.log 追加推送失败日志，供 watchdog 巡检。"""
    try:
        ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        snippet = (text or "")[:120].replace("\n", " ")
        line = f"{ts}\t{reason}\t{snippet}\n"
        with open(FAILURE_LOG, "a", encoding="utf-8") as fp:
            fp.write(line)
    except Exception:
        pass  # 写失败也别炸 send 主流程


def send_feishu(text: str) -> bool:
    """实际发送飞书消息（A++ 步 3：失败 → /tmp/feishu-notify-failures.log）

    2026-05-19：推送前过 humanize_alert，确保飞书消息全中文 + 真换行。
    """
    text = humanize_alert(text)
    try:
        c = json.load(open(CONFIG_PATH))
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": c["app_id"], "app_secret": c["app_secret"]}).encode(),
            headers={"Content-Type": "application/json"}
        )
        token = json.loads(urllib.request.urlopen(req, timeout=10).read())["tenant_access_token"]
        msg = {
            "receive_id": c["chat_id"],
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }
        req2 = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps(msg).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        code = resp.get("code")
        if code == 0:
            return True
        # 飞书 API 业务错误（code != 0）
        log_push_failure(f"feishu-code-{code}", text)
        return False
    except Exception as e:
        print(f"[feishu-notify send error] {e}", file=sys.stderr)
        log_push_failure(f"urlopen-error:{type(e).__name__}", text)
        return False


def parse_args():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--level", default="P1", choices=["P0", "P1", "P2", "P3"])
    p.add_argument("--source", default="unknown")
    p.add_argument("--key", default=None)
    p.add_argument("--channel", default="push", choices=["push", "digest"])
    p.add_argument("text", nargs="?", default=None)
    p.add_argument("-h", "--help", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    if args.help:
        print(__doc__)
        return 0

    text = args.text
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        print("FAIL: no text", file=sys.stderr)
        return 1

    level = args.level.upper()
    source = args.source
    channel = (args.channel or "push").lower()
    # 没传 key 时退化用文本 hash 做 dedupe key（防刷屏 fallback）
    key = args.key or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # P3：仅 stdout
    if level == "P3":
        print(text)
        return 0

    # channel=digest 短路：强制不推飞书+不 dedupe+无视静默时段，仅写 history
    if channel == "digest":
        write_history(level, source, key, text, pushed=False, channel="digest")
        print("DIGEST: written to alert_history (channel=digest, no feishu push)")
        return 0

    # 决策：是否推送
    push = False
    reason = ""

    if level == "P0":
        push = True  # P0 无视一切
    elif level == "P1":
        if is_offwork_hours():
            reason = "off-hours (P1 不推 22:00-09:00)"
        else:
            hit, cnt = check_dedupe(source, key)
            if hit:
                reason = f"dedupe-hit (24h 内已发，累计 {cnt+1} 次)"
            else:
                push = True
    elif level == "P2":
        reason = "P2-no-push (写 PG 不推机器人)"
        push = False

    # 渲染文本
    if level == "P0":
        rendered = "[紧急] 🔴 " + text
    elif level == "P1":
        rendered = text  # P1 不加 emoji 前缀，保持原样
    else:
        rendered = text

    # 发送
    sent_ok = False
    if push:
        sent_ok = send_feishu(rendered)
        if sent_ok:
            update_dedupe(source, key, text, sent=True)
        else:
            reason = "send-failed"

    # 写 history（push channel）
    write_history(level, source, key, text, pushed=sent_ok, channel="push")

    # 抑制时（被 dedupe），仅更新 count
    if not push and level == "P1":
        update_dedupe(source, key, text, sent=False)

    if push and sent_ok:
        print("OK")
        return 0
    elif push and not sent_ok:
        print(f"FAIL: send-failed")
        return 1
    else:
        print(f"SUPPRESSED: {reason}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
