import json, urllib.request, time, sys

c = json.load(open("/home/ubuntu/feishu-sync/config.json"))

def get_token():
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": c["app_id"], "app_secret": c["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req).read())["tenant_access_token"]

def fetch_all(token, tid):
    bt = "V1LNbTEv1aBvpXsRLU8cWzuhn6d"
    all_items, pt, more = [], "", True
    while more:
        time.sleep(0.3)
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bt}/tables/{tid}/records?page_size=500"
        if pt: url += f"&page_token={pt}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        resp = json.loads(urllib.request.urlopen(req).read())
        all_items.extend(resp.get("data",{}).get("items",[]))
        more = resp.get("data",{}).get("has_more", False)
        pt = resp.get("data",{}).get("page_token","")
    return all_items

def gf(f, key):
    v = f.get(key)
    if isinstance(v, list) and v and isinstance(v[0], dict): return v[0].get("text","")
    return str(v) if v is not None else ""

def nf(f, key):
    v = f.get(key, 0)
    if isinstance(v, list): return 0
    try: return float(v or 0)
    except: return 0
def usd(n): return f"${n:,.0f}"
def pct(a, b): return f"{(b-a)/a*100:.1f}%" if a else "—"

token = get_token()
rows = fetch_all(token, "tblWCN0GSZ2mNLsS")

# 按周汇总
weeks_set = set()
for r in rows:
    w = gf(r["fields"], "对应周")
    if w: weeks_set.add(w)
weeks = sorted(weeks_set)[-3:]

# 汇总数据
data = {}
for w in weeks:
    data[w] = {"invest":0, "adBuyer":0, "convert":0, "salary":0, "company":0, "ops":0,
               "traffic":0, "cpa":0, "cps":0, "totalRev":0, "totalCost":0, "profit":0,
               "backendReg":0, "settleReg":0, "guilds":{}}

for r in rows:
    f = r["fields"]
    w = gf(f, "对应周")
    if w not in data: continue
    d = data[w]
    guild = str(f.get("公会",""))

    invest = nf(f, "投放费用")
    adBuyer = nf(f, "投手费用")
    convert = nf(f, "转化")
    salary = nf(f, "工资")
    company = nf(f, "公司经营")
    ops = nf(f, "运营")
    cpa = nf(f, "CPA收入")
    cps = nf(f, "CPS收入")
    totalRev = nf(f, "总收入")
    totalCost = nf(f, "总支出")
    profit = nf(f, "净利润")
    backendReg = int(nf(f, "后台注册量"))
    settleReg = int(nf(f, "结算注册量"))

    d["invest"] += invest
    d["adBuyer"] += adBuyer
    d["convert"] += convert
    d["salary"] += salary
    d["company"] += company
    d["ops"] += ops
    d["traffic"] += invest + adBuyer
    d["cpa"] += cpa
    d["cps"] += cps
    d["totalRev"] += totalRev
    d["totalCost"] += totalCost
    d["profit"] += profit
    d["backendReg"] += backendReg
    d["settleReg"] += settleReg

    d["guilds"][guild] = {
        "invest": invest, "adBuyer": adBuyer, "convert": convert,
        "salary": salary, "company": company, "ops": ops,
        "cpa": cpa, "cps": cps, "totalRev": totalRev,
        "totalCost": totalCost, "profit": profit,
        "backendReg": backendReg, "settleReg": settleReg,
    }

w0, w1, w2 = [data[w] for w in weeks]
L = []
L.append("📊 Nova 周度精算分析")
L.append(f"{weeks[0]} → {weeks[1]} → {weeks[2]}\n")

L.append("=" * 45)
L.append(f"净利润: {usd(w0['profit'])} → {usd(w1['profit'])} → {usd(w2['profit'])}")
L.append("=" * 45)

# 支出5大块
L.append("\n【支出拆解 - 钱花在哪了？】\n")
cost_items = [
    ("流量(投放+投手)", "traffic", lambda d: d["invest"] + d["adBuyer"]),
    ("  其中投放", "invest", lambda d: d["invest"]),
    ("  其中投手工资", "adBuyer", lambda d: d["adBuyer"]),
    ("转化(中台)", "convert", lambda d: d["convert"]),
    ("运营(经纪人工资)", "salary", lambda d: d["salary"]),
    ("公司经营", "company", lambda d: d["company"]),
    ("运营其他", "ops", lambda d: d["ops"]),
]

for label, key, getter in cost_items:
    v0, v1, v2 = getter(w0), getter(w1), getter(w2)
    trend = ""
    if v2 > v1 > v0 and v0 > 0: trend = " ⚠️连涨"
    elif v2 > v1 * 1.15 and v1 > 0: trend = " ⚠️突增"
    L.append(f"  {label}: {usd(v0)} → {usd(v1)} → {usd(v2)} ({pct(v1,v2)}){trend}")

# 检查投手工资和投放是否挂钩
invest_chg = (w2["invest"] - w1["invest"]) / max(w1["invest"], 1)
buyer_chg = (w2["adBuyer"] - w1["adBuyer"]) / max(w1["adBuyer"], 1) if w1["adBuyer"] else 0
if invest_chg < -0.1 and buyer_chg > -0.05:
    L.append(f"\n  🔍 疑点：投放砍了{abs(invest_chg)*100:.0f}%，但投手工资只变{buyer_chg*100:.0f}%")
    L.append(f"     投手工资应和投放挂钩，需要检查")

# 收入结构
L.append("\n【收入结构】\n")
for label, getter in [("CPA收入", lambda d: d["cpa"]), ("CPS收入", lambda d: d["cps"]), ("总收入", lambda d: d["totalRev"])]:
    v0, v1, v2 = getter(w0), getter(w1), getter(w2)
    trend = ""
    if v2 < v1 < v0: trend = " ⚠️连降"
    L.append(f"  {label}: {usd(v0)} → {usd(v1)} → {usd(v2)} ({pct(v1,v2)}){trend}")

# CPA/CPS占比变化
if w2["totalRev"] > 0:
    cpa_pct = w2["cpa"] / w2["totalRev"] * 100
    L.append(f"\n  CPA占总收入: {cpa_pct:.0f}% (上上周{w0['cpa']/max(w0['totalRev'],1)*100:.0f}%)")

# 注册量
L.append("\n【注册量】\n")
L.append(f"  后台注册: {w0['backendReg']} → {w1['backendReg']} → {w2['backendReg']} ({pct(w1['backendReg'],w2['backendReg'])})")
L.append(f"  结算注册: {w0['settleReg']} → {w1['settleReg']} → {w2['settleReg']} ({pct(w1['settleReg'],w2['settleReg'])})")
if w2["backendReg"] > 0:
    rate = w2["settleReg"] / w2["backendReg"] * 100
    rate_prev = w1["settleReg"] / max(w1["backendReg"], 1) * 100
    L.append(f"  结算率: {rate_prev:.0f}% → {rate:.0f}%")

# 各公会
L.append("\n【各公会利润趋势】\n")
all_guilds = set()
for w in weeks: all_guilds.update(data[w]["guilds"].keys())
guild_list = []
for g in all_guilds:
    if not g: continue
    vals = [data[w]["guilds"].get(g, {}).get("profit", 0) for w in weeks]
    guild_list.append((g, vals))
guild_list.sort(key=lambda x: x[1][-1], reverse=True)

for g, vals in guild_list:
    trend = ""
    if all(v < 0 for v in vals if v != 0): trend = " 🔴连亏"
    elif vals[2] < vals[1] < vals[0] and vals[0] > 0: trend = " 📉连降"
    elif vals[2] > vals[1] > vals[0]: trend = " 📈连升"
    L.append(f"  {g}: {usd(vals[0])} → {usd(vals[1])} → {usd(vals[2])}{trend}")

# 异常预警汇总
L.append("\n" + "=" * 45)
L.append("【需要你关注的事】")
L.append("=" * 45 + "\n")

alerts = []
# 利润腰斩
if w2["profit"] < w1["profit"] * 0.6:
    alerts.append(f"1. 利润从{usd(w1['profit'])}降到{usd(w2['profit'])}，腰斩")

# 连续亏损公会
for g, vals in guild_list:
    if all(v < 0 for v in vals) and vals[-1] < -5000:
        alerts.append(f"   {g} 连续3周亏损，上周亏{usd(abs(vals[-1]))}")

# 支出异常
if w2["company"] > w1["company"] * 1.2 and w2["company"] - w1["company"] > 5000:
    alerts.append(f"2. 公司经营费用突增: {usd(w1['company'])}→{usd(w2['company'])} (+{usd(w2['company']-w1['company'])})")

if w2["salary"] > w1["salary"] * 1.05 and w1["salary"] > w0["salary"] * 1.05:
    alerts.append(f"3. 经纪人工资连续上涨: {usd(w0['salary'])}→{usd(w1['salary'])}→{usd(w2['salary'])}")

if invest_chg < -0.1 and buyer_chg > -0.05 and w1["adBuyer"] > 0:
    alerts.append(f"4. 投放砍了但投手工资没降，检查投手绩效挂钩")

# CPS连续下降
if w2["cps"] < w1["cps"] < w0["cps"]:
    alerts.append(f"5. CPS收入连续3周下降: {usd(w0['cps'])}→{usd(w1['cps'])}→{usd(w2['cps'])}，检查主播产出质量")

for a in alerts:
    L.append(a)

if not alerts:
    L.append("本周无重大异常")

print("\n".join(L))
