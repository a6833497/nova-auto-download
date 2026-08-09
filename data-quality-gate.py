#!/usr/bin/env python3
"""Fail-closed quality gate for one UTC+0 BI business day.

The downloader may return a valid file for the wrong report, guild or date.
This gate proves identity/date/guild/schema before daily-sync may publish it.
"""
import argparse, glob, json, os, re, sys

EXPECTED = {
    "印尼1-Nova": ("Nova",), "印尼2-Carote": ("Carote",), "印尼3-宝石": ("宝石", "Permata"),
    "巴西1-Nova": ("BR-HotBR", "BR-HotSozinha"), "巴西2-Evian": ("Evian",), "巴西3-Wisky": ("Wisky", "Whisky"),
    "巴西4-Doce": ("Doce",), "西语1-Nova": ("Nova",), "西语2-Evian": ("Evian",),
}
CORE = ({"sid"}, {"create_date(day)", "active_date(day)", "date"}, {"guild_name"},
        {"level"}, {"online_minute"}, {"all_diamond_amount"})

def norm(v): return re.sub(r"\s+", "", str(v or "")).lower()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("directory"); ap.add_argument("date")
    ap.add_argument("--json-out"); a = ap.parse_args()
    failures, reports = [], []
    for label, guild_tokens in EXPECTED.items():
        candidates = [p for p in glob.glob(os.path.join(a.directory, label + "_*.json"))
                      if not any(x in os.path.basename(p) for x in ("公会数据", "薪资奖励", "语音房", "summary"))]
        if len(candidates) != 1:
            failures.append(f"{label}: 主播日明细应为1份，实际{len(candidates)}份")
            continue
        p = candidates[0]
        try: d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            failures.append(f"{label}: JSON无法读取({e})"); continue
        headers = {norm(x) for x in d.get("headers", [])}; rows = d.get("rows", [])
        missing = ["/".join(sorted(x)) for x in CORE if not {norm(y) for y in x} & headers]
        if missing: failures.append(f"{label}: 报表类型错误/缺核心字段 {','.join(missing)}")
        if not rows and not label.startswith("土耳其"):
            failures.append(f"{label}: 主播日明细0行")
        date_keys = [x for x in d.get("headers", []) if norm(x) in {norm(y) for y in CORE[1]}]
        guild_key = next((x for x in d.get("headers", []) if norm(x)=="guild_name"), None)
        dates, guilds = set(), set()
        for r in rows:
            if not isinstance(r, dict): continue
            for k in date_keys:
                raw = str(r.get(k) or "").strip()
                if re.match(r"^\d{8}$", raw): v = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                else: v = raw[:10]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v): dates.add(v)
            if guild_key and r.get(guild_key): guilds.add(str(r[guild_key]))
        if rows and a.date not in dates: failures.append(f"{label}: 内容没有目标日期{a.date}，实际日期样例={sorted(dates)[:3]}")
        if guilds and not any(any(norm(t) in norm(g) for t in guild_tokens) for g in guilds):
            failures.append(f"{label}: 文件内公会不匹配，实际={sorted(guilds)[:3]}")
        reports.append({"label":label,"file":os.path.basename(p),"rows":len(rows),"dates":sorted(dates),"guilds":sorted(guilds)})
    result={"date":a.date,"status":"PASS" if not failures else "FAIL","reports":reports,"failures":failures}
    if a.json_out:
        with open(a.json_out,"w",encoding="utf-8") as f: json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if not failures else 2
if __name__ == "__main__": sys.exit(main())
