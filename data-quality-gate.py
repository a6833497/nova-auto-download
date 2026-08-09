#!/usr/bin/env python3
"""Fail-closed quality gate for one BI business day.

The downloader may return a valid file for the wrong report, guild or date.
This gate proves identity/date/guild/schema before daily-sync may publish it.
"""

import argparse
import glob
import json
import os
import re
import sys


EXPECTED = {
    "印尼1-Nova": ("Nova",),
    "印尼2-Carote": ("Carote",),
    "印尼3-宝石": ("宝石", "Permata"),
    "巴西1-Nova": ("BR-HotBR", "BR-HotSozinha"),
    "巴西2-Evian": ("Evian",),
    "巴西3-Wisky": ("Wisky", "Whisky"),
    "巴西4-Doce": ("Doce",),
    "西语1-Nova": ("Nova",),
    "西语2-Evian": ("Evian",),
}
CORE = (
    {"sid"},
    {"create_date(day)", "active_date(day)", "date"},
    {"guild_name"},
    {"level"},
    {"online_minute"},
    {"all_diamond_amount"},
)


def norm(value):
    return re.sub(r"\s+", "", str(value or "")).lower()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("date")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    failures = []
    reports = []
    for label, guild_tokens in EXPECTED.items():
        candidates = [
            path
            for path in glob.glob(os.path.join(args.directory, label + "_*.json"))
            if not any(
                marker in os.path.basename(path)
                for marker in ("公会数据", "薪资奖励", "语音房", "summary")
            )
        ]
        if len(candidates) != 1:
            failures.append(f"{label}: 主播日明细应为1份，实际{len(candidates)}份")
            continue

        path = candidates[0]
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            failures.append(f"{label}: JSON无法读取({exc})")
            continue

        headers = {norm(value) for value in data.get("headers", [])}
        rows = data.get("rows", [])
        missing = [
            "/".join(sorted(fields))
            for fields in CORE
            if not {norm(value) for value in fields} & headers
        ]
        if missing:
            failures.append(f"{label}: 报表类型错误/缺核心字段 {','.join(missing)}")
        if not rows:
            failures.append(f"{label}: 主播日明细0行")

        date_keys = [
            key
            for key in data.get("headers", [])
            if norm(key) in {norm(value) for value in CORE[1]}
        ]
        guild_key = next(
            (key for key in data.get("headers", []) if norm(key) == "guild_name"),
            None,
        )
        dates = set()
        guilds = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in date_keys:
                raw = str(row.get(key) or "").strip()
                if re.match(r"^\d{8}$", raw):
                    value = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                else:
                    value = raw[:10]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                    dates.add(value)
            if guild_key and row.get(guild_key):
                guilds.add(str(row[guild_key]))

        if rows and args.date not in dates:
            failures.append(
                f"{label}: 内容没有目标日期{args.date}，实际日期样例={sorted(dates)[:3]}"
            )
        if guilds and not any(
            any(norm(token) in norm(guild) for token in guild_tokens)
            for guild in guilds
        ):
            failures.append(f"{label}: 文件内公会不匹配，实际={sorted(guilds)[:3]}")
        reports.append(
            {
                "label": label,
                "file": os.path.basename(path),
                "rows": len(rows),
                "dates": sorted(dates),
                "guilds": sorted(guilds),
            }
        )

    result = {
        "date": args.date,
        "status": "PASS" if not failures else "FAIL",
        "reports": reports,
        "failures": failures,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
