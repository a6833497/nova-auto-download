#!/usr/bin/env python3
"""
CEO 每日经营简报
每天早上10:00自动推送到飞书胖虎助理群
数据来源：BI数据库 + 飞书财务表
"""
import json, urllib.request, time, os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")
YESTERDAY = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
DAY_BEFORE = (datetime.now(CST) - timedelta(days=2)).strftime("%Y-%m-%d")
WEEKDAY = datetime.now(CST).weekday()  # 0=周一

PG_CONN = dict(host="127.0.0.1", port=5432, database="nova_dashboard", user="nova_app", password="Nova2026pg!")
CONFIG_PATH = "/home/ubuntu/feishu-sync/config.json"

def ts_range(date_str):
    """YYYY-MM-DD → CST midnight datetime range [start, end) as datetime objects"""
    y, m, d = map(int, date_str.split("-"))
    start = datetime(y, m, d, tzinfo=timezone(timedelta(hours=8)))
    end = start + timedelta(days=1)
    return start, end

def query_db(sql, params=()):
    conn = psycopg2.connect(**PG_CONN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

def rmb(n):
    """人民币格式：超万用万，否则原数"""
    if abs(n) >= 10000: return f"¥{n/10000:.1f}万"
    return f"¥{n:,.0f}"

def wan(n):
    """钻石/人数格式：超万用万，否则原数"""
    if abs(n) >= 10000: return f"{n/10000:.1f}万"
    return f"{n:,.0f}"

def pct(a, b):
    if not a: return "—"
    return f"{(b-a)/a*100:+.0f}%"

# ═══════════════════════════════════════
# 1. BI数据：昨日运营概况
# ═══════════════════════════════════════
def get_daily_ops():
    ts_y = ts_range(YESTERDAY)
    ts_d = ts_range(DAY_BEFORE)

    # 昨天 vs 前天：总钻石、活跃、注册
    sql = """
    SELECT
        CAST(SUM(md.paiddiamondtotal) AS INT) as diamonds,
        COUNT(DISTINCT CASE WHEN md.onlineminutes > 0 THEN h.sid END) as online,
        COUNT(DISTINCT h.sid) as total
    FROM metrics_daily md JOIN hosts h ON h.id = md.hostid
    WHERE md.date >= %s AND md.date < %s
    """
    yesterday = query_db(sql, ts_y)
    dayBefore = query_db(sql, ts_d)

    y = yesterday[0] if yesterday else {"diamonds": 0, "online": 0, "total": 0}
    d = dayBefore[0] if dayBefore else {"diamonds": 0, "online": 0, "total": 0}

    # S女质量
    sq = """
    SELECT
        COUNT(DISTINCT h.sid) as total,
        COUNT(DISTINCT CASE WHEN md.onlineminutes < 120 THEN h.sid END) as offline,
        COUNT(DISTINCT CASE WHEN md.replyrate < 0.8 THEN h.sid END) as "lowReply"
    FROM metrics_daily md JOIN hosts h ON h.id = md.hostid
    WHERE h.level IN ('S','S1','S2') AND md.date >= %s AND md.date < %s
    """
    sq_result = query_db(sq, ts_y)
    s_quality = sq_result[0] if sq_result else {"total": 0, "offline": 0, "lowReply": 0}

    # 风险预警：昨天有收入今天没上线
    risk_sql = """
    SELECT COUNT(DISTINCT h.sid) as cnt
    FROM metrics_daily md1 JOIN hosts h ON h.id = md1.hostid
    WHERE md1.date >= %s AND md1.date < %s AND md1.paiddiamondtotal > 0
    AND NOT EXISTS (
        SELECT 1 FROM metrics_daily md2
        WHERE md2.hostid = md1.hostid AND md2.date >= %s AND md2.date < %s AND md2.onlineminutes > 0
    )
    """
    # 这个查询太慢，用简化版
    risk_cnt = 0

    # 各公会昨天产出
    guild_sql = """
    SELECT gc."guildAlias" as guild,
        CAST(SUM(md.paiddiamondtotal) AS INT) as diamonds,
        COUNT(DISTINCT CASE WHEN md.onlineminutes > 0 THEN h.sid END) as online
    FROM guild_config gc
    JOIN hosts h ON h.guildname IN (
        SELECT TRIM(u) FROM unnest(string_to_array(gc."dbGuildNames", ',')) AS u
    )
    JOIN metrics_daily md ON md.hostid = h.id
    WHERE gc."isActive" = 1 AND md.date >= %s AND md.date < %s
    GROUP BY gc."guildAlias" ORDER BY diamonds DESC
    """
    try:
        guilds = query_db(guild_sql, ts_y)
    except:
        guilds = []

    # 各经纪人昨天产出
    agent_sql = """
    SELECT COALESCE(a.name, '未分配') as agent,
        CAST(SUM(md.paiddiamondtotal) AS INT) as diamonds,
        COUNT(DISTINCT CASE WHEN md.onlineminutes > 0 THEN h.sid END) as online
    FROM metrics_daily md JOIN hosts h ON h.id = md.hostid
    LEFT JOIN host_agent ha ON ha.hostid = h.id AND ha.valid = true
    LEFT JOIN agents a ON a.id = ha.agentid
    WHERE md.date >= %s AND md.date < %s
    GROUP BY agent ORDER BY diamonds DESC
    """
    agents = query_db(agent_sql, ts_y)

    return y, d, s_quality, guilds, agents

# ═══════════════════════════════════════
# 2. 飞书：本周财务数据
# ═══════════════════════════════════════
def get_feishu_weekly():
    try:
        c = json.load(open(CONFIG_PATH))
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": c["app_id"], "app_secret": c["app_secret"]}).encode(),
            headers={"Content-Type": "application/json"}
        )
        token = json.loads(urllib.request.urlopen(req).read())["tenant_access_token"]

        bt = "V1LNbTEv1aBvpXsRLU8cWzuhn6d"
        # 利润周报表
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bt}/tables/tblWCN0GSZ2mNLsS/records?page_size=500"
        resp = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})).read())
        rows = resp.get("data", {}).get("items", [])

        # 找最近两周
        weeks = set()
        for r in rows:
            f = r.get("fields", {})
            w = f.get("对应周")
            if isinstance(w, list) and w: w = w[0].get("text", "")
            elif isinstance(w, str): pass
            else: w = str(w or "")
            if w: weeks.add(w)

        sorted_weeks = sorted(weeks)[-2:]
        if len(sorted_weeks) < 2:
            return None, None, sorted_weeks

        # 按周汇总
        week_data = {}
        for w in sorted_weeks:
            week_data[w] = {"invest": 0, "profit": 0, "cpa": 0, "cps": 0, "rev": 0, "salary": 0, "convert": 0, "company": 0, "adBuyer": 0}

        for r in rows:
            f = r.get("fields", {})
            w = f.get("对应周")
            if isinstance(w, list) and w: w = w[0].get("text", "")
            elif not isinstance(w, str): w = str(w or "")
            if w not in week_data: continue
            d = week_data[w]
            for key in ["invest", "profit", "cpa", "cps", "rev", "salary", "convert", "company", "adBuyer"]:
                mapping = {"invest": "投放费用", "profit": "净利润", "cpa": "CPA收入", "cps": "CPS收入",
                          "rev": "总收入", "salary": "工资", "convert": "转化", "company": "公司经营", "adBuyer": "投手费用"}
                v = f.get(mapping[key], 0)
                if isinstance(v, (int, float)): d[key] += v

        return week_data.get(sorted_weeks[0]), week_data.get(sorted_weeks[1]), sorted_weeks
    except Exception as e:
        return None, None, []

# ═══════════════════════════════════════
# 3. 本周注册目标进度
# ═══════════════════════════════════════
def get_weekly_target_progress():
    # 找本周的周一
    now = datetime.now(CST)
    monday = now - timedelta(days=now.weekday())
    sunday = monday + timedelta(days=6)
    week_key = f"{monday.strftime('%m/%d')}~{sunday.strftime('%m/%d')}"

    targets = query_db('SELECT "guildAlias", "plannedRegistrations" FROM guild_targets WHERE "weekKey" = %s', (week_key,))
    if not targets:
        # 试全角波浪线
        week_key2 = week_key.replace("~", "～")
        targets = query_db('SELECT "guildAlias", "plannedRegistrations" FROM guild_targets WHERE "weekKey" = %s', (week_key2,))

    # 本周已注册数
    ts_mon = ts_range(monday.strftime("%Y-%m-%d"))
    ts_today = ts_range(TODAY)

    reg_sql = """
    SELECT gc."guildAlias" as guild, COUNT(DISTINCT h.sid) as reg
    FROM guild_config gc
    JOIN hosts h ON h.guildname IN (
        SELECT TRIM(u) FROM unnest(string_to_array(gc."dbGuildNames", ',')) AS u
    )
    WHERE gc."isActive" = 1 AND h.registrationdate >= %s AND h.registrationdate < %s
    GROUP BY gc."guildAlias"
    """
    try:
        regs = query_db(reg_sql, (ts_mon[0], ts_today[1]))
    except:
        regs = []

    target_map = {t["guildAlias"]: t["plannedRegistrations"] for t in targets}
    reg_map = {r["guild"]: r["reg"] for r in regs}

    days_passed = (now - monday).days + 1
    return target_map, reg_map, days_passed, week_key

# ═══════════════════════════════════════
# 组装报告
# ═══════════════════════════════════════
def build_report():
    L = []
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    L.append(f"☀️ 老板早，今天{weekday_names[WEEKDAY]}（{TODAY}）")
    L.append("")

    # 昨日运营概况
    y, d, sq, guilds, agents = get_daily_ops()
    yd = y["diamonds"] or 0
    dd = d["diamonds"] or 0

    L.append("═══════════════════════════")
    L.append("【昨日概况】")
    L.append("═══════════════════════════")
    L.append(f"  总钻石: {wan(yd)} ({pct(dd, yd)})")
    L.append(f"  活跃主播: {y['online'] or 0} ({pct(d['online'] or 0, y['online'] or 0)})")

    if sq["total"] > 0:
        ok_rate = round((sq["total"] - max(sq["offline"], sq["lowReply"])) / sq["total"] * 100)
        L.append(f"  S女质量: {sq['total']}人，在线不达标{sq['offline']}人，回复不达标{sq['lowReply']}人")

    # 各公会简报
    if guilds:
        L.append("")
        L.append("  各公会产出:")
        for g in guilds[:8]:
            L.append(f"    {g['guild']}: {wan(g['diamonds'])} ({g['online']}人在线)")

    # 经纪人简报
    if agents:
        L.append("")
        L.append("  经纪人产出:")
        for a in agents:
            if a["agent"] == "未分配": continue
            L.append(f"    {a['agent']}: {wan(a['diamonds'])}")

    # 周度财务
    prev_week, this_week, week_labels = get_feishu_weekly()
    if prev_week and this_week and len(week_labels) >= 2:
        L.append("")
        L.append("═══════════════════════════")
        L.append(f"【本周财务】{week_labels[-1]}")
        L.append("═══════════════════════════")
        L.append(f"  收入: {rmb(this_week['rev'])} ({pct(prev_week['rev'], this_week['rev'])})")
        L.append(f"    CPA: {rmb(this_week['cpa'])} | CPS: {rmb(this_week['cps'])}")
        L.append(f"  投放: {rmb(this_week['invest'])} ({pct(prev_week['invest'], this_week['invest'])})")
        L.append(f"  净利润: {rmb(this_week['profit'])} ({pct(prev_week['profit'], this_week['profit'])})")

        # 异常检查
        alerts = []
        if this_week["convert"] > prev_week["convert"] * 1.2 and this_week["convert"] - prev_week["convert"] > 3000:
            alerts.append(f"转化成本涨了{rmb(this_week['convert'] - prev_week['convert'])}")
        if this_week["company"] > prev_week["company"] * 1.2 and this_week["company"] - prev_week["company"] > 5000:
            alerts.append(f"公司经营涨了{rmb(this_week['company'] - prev_week['company'])}")
        if this_week["salary"] > prev_week["salary"] * 1.05:
            alerts.append(f"经纪人工资涨了{pct(prev_week['salary'], this_week['salary'])}")
        inv_chg = (this_week["invest"] - prev_week["invest"]) / max(prev_week["invest"], 1)
        buy_chg = (this_week["adBuyer"] - prev_week["adBuyer"]) / max(prev_week["adBuyer"], 1)
        if inv_chg < -0.1 and buy_chg > -0.03:
            alerts.append("投放砍了但投手工资没降")
        if this_week["cps"] < prev_week["cps"] * 0.9:
            alerts.append(f"CPS收入降了{pct(prev_week['cps'], this_week['cps'])}")

        if alerts:
            L.append("")
            L.append("  ⚠️ 异常:")
            for a in alerts:
                L.append(f"    · {a}")

    # 注册目标进度
    target_map, reg_map, days, week_key = get_weekly_target_progress()
    if target_map:
        L.append("")
        L.append("═══════════════════════════")
        L.append(f"【注册目标进度】第{days}天/7天")
        L.append("═══════════════════════════")
        total_target = sum(target_map.values())
        total_reg = sum(reg_map.values())
        expected_rate = days / 7
        actual_rate = total_reg / max(total_target, 1)
        status = "✅达标" if actual_rate >= expected_rate * 0.8 else "⚠️滞后"
        L.append(f"  总体: {total_reg}/{total_target} ({actual_rate*100:.0f}%) {status}")
        for guild, target in sorted(target_map.items()):
            reg = reg_map.get(guild, 0)
            rate = reg / max(target, 1)
            flag = "✅" if rate >= expected_rate * 0.8 else "⚠️"
            L.append(f"    {flag} {guild}: {reg}/{target} ({rate*100:.0f}%)")

    # 需要决策的事
    decisions = []

    # 连续亏损公会
    loss_sql = """
    SELECT gc.guildAlias as guild
    FROM guild_config gc WHERE gc.isActive = 1
    """
    # 简化：从飞书利润表找连续亏损的
    if prev_week and this_week:
        if this_week["profit"] < prev_week["profit"] * 0.5 and prev_week["profit"] > 10000:
            decisions.append(f"净利润从{rmb(prev_week['profit'])}腰斩到{rmb(this_week['profit'])}，需要分析原因")

    if decisions:
        L.append("")
        L.append("═══════════════════════════")
        L.append("【需要你决策的事】")
        L.append("═══════════════════════════")
        for i, d in enumerate(decisions, 1):
            L.append(f"  {i}. {d}")

    return "\n".join(L)

# ═══════════════════════════════════════
# 发送
# ═══════════════════════════════════════
def send_feishu(text):
    c = json.load(open(CONFIG_PATH))
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": c["app_id"], "app_secret": c["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"}
    )
    token = json.loads(urllib.request.urlopen(req).read())["tenant_access_token"]
    msg = {"receive_id": c["chat_id"], "msg_type": "text", "content": json.dumps({"text": text})}
    req2 = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(msg).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    resp = json.loads(urllib.request.urlopen(req2).read())
    return resp.get("code") == 0

if __name__ == "__main__":
    report = build_report()
    print(report)
    print()
    if send_feishu(report):
        print("[CEO日报] 已推送到飞书")
    else:
        print("[CEO日报] 推送失败")
