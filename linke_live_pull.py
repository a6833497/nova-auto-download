#!/usr/bin/env python3
"""Linky realtime display refresh. This path never declares historical finality."""
import base64, datetime, hashlib, hmac, json, sys, time, urllib.request
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_values
from linke_ledger_pull import database_url_from_environment
from linky_api_pagination import pull_pages

GUILD = sys.argv[1] if len(sys.argv) > 1 else "Permata-Indonesia"
CFG = json.loads(Path("/home/ubuntu/nova-auto-download/guild-tokens.json").read_text())
g = CFG["guilds"][GUILD]; TOK, SEC = g["oauth_token"], g["oauth_token_secret"]
today = datetime.datetime.now(datetime.timezone.utc).date()
D = today.strftime("%Y%m%d"); D_ISO = today.isoformat()
if len(sys.argv) > 2: D = sys.argv[2]; D_ISO = f"{D[:4]}-{D[4:6]}-{D[6:]}"

def call(fp, timeout=15):
    ts = str(int(time.time()*1000))
    sig = base64.b64encode(hmac.new(SEC.encode(), (fp+"&"+ts).encode(), hashlib.sha1).digest()).decode()
    req = urllib.request.Request("https://api.linke.ai"+fp, headers={"X-Auth-Token":TOK,"X-Auth-Timestamp":ts,
        "X-Auth-Signature":sig,"X-App-Language":"en","Country":"US","User-Agent":"Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

print(f"拉 {GUILD} {D_ISO} ...")
s1_rows, _ = pull_pages(call, "/api/guild/streamer_stat", D, "total_earns")
room_rows, _ = pull_pages(call, "/api/guild/live_room_stat", D, "receive_diamonds")
s1 = {int(row["sid"]): row for row in s1_rows}; rm = {int(row["sid"]): row for row in room_rows}
now_sids = set()
if D_ISO == today.isoformat():
    for attempt in range(2):
        try:
            page = 1
            while page <= 40:
                data = call(f"/api/guild/online_anchors?page={page}&page_size=200", timeout=25)
                items = data.get("items") or []
                now_sids.update(int(row["sid"]) for row in items)
                if not data.get("next_page") or not items: break
                page += 1
            break
        except Exception as error:
            if attempt == 1: print(f"  [warn] 此刻在线接口跳过({str(error)[:40]})")

database_url = database_url_from_environment()
if not database_url: raise SystemExit("DATABASE_URL is required")
conn = psycopg2.connect(database_url); cur = conn.cursor()
cur.execute("SELECT source_timezone,source_utc_offset_minutes FROM data_source_config WHERE source_key='linke_realtime' AND enabled=true")
source_timezone, source_offset_minutes = cur.fetchone() or ('UTC+0',0)
sids = set(s1) | set(rm) | now_sids
rows=[]
for sid in sids:
    a=s1.get(sid,{}); b=rm.get(sid,{})
    rows.append((GUILD,sid,D_ISO,a.get("nickname") or b.get("nickname") or None,a.get("chat_earns") or 0,
      a.get("online_time") or 0,a.get("ten_minutes_reply_ratio") or 0,b.get("receive_diamonds") or 0,
      float(b.get("on_mic_time") or 0),b.get("new_fans") or 0,sid in now_sids))
if rows:
    execute_values(cur,"""INSERT INTO linke_live_today
      (guild,sid,snap_date,nickname,chat_earns,online_time,ten_min_reply,room_diamonds,on_mic_time,new_fans,online_now) VALUES %s
      ON CONFLICT (sid,snap_date) DO UPDATE SET nickname=COALESCE(EXCLUDED.nickname,linke_live_today.nickname),
      chat_earns=EXCLUDED.chat_earns,online_time=EXCLUDED.online_time,ten_min_reply=EXCLUDED.ten_min_reply,
      room_diamonds=EXCLUDED.room_diamonds,on_mic_time=EXCLUDED.on_mic_time,new_fans=EXCLUDED.new_fans,
      online_now=EXCLUDED.online_now,fetched_at=now()""",rows)
    nicknames=[(sid,(s1.get(sid,{}).get('nickname') or '').strip()) for sid in sids]
    nicknames=[item for item in nicknames if item[1]]
    if nicknames:
        execute_values(cur,"""UPDATE hosts AS h SET name=v.nickname,updatedat=now()
          FROM (VALUES %s) AS v(sid,nickname)
          WHERE h.sid=v.sid::text AND (h.name IS NULL OR trim(h.name)='' OR trim(h.name)=trim(h.sid))""",nicknames)
    snapshot_at_utc=datetime.datetime.now(datetime.timezone.utc)
    source_local=snapshot_at_utc+datetime.timedelta(minutes=int(source_offset_minutes or 0))
    previous=source_local.replace(minute=0,second=0,microsecond=0)-datetime.timedelta(hours=1)
    generated=(previous-datetime.timedelta(minutes=int(source_offset_minutes or 0))).replace(tzinfo=datetime.timezone.utc)
    execute_values(cur,"""INSERT INTO linke_live_snapshot (sid,guild,snapshot_at,online_now) VALUES %s
      ON CONFLICT (sid,snapshot_at) DO NOTHING""",
      [(sid,GUILD,snapshot_at_utc.replace(tzinfo=None),sid in now_sids) for sid in sids])
    metric_rows=[(sid,GUILD,snapshot_at_utc,generated,source_timezone,'MINUTE',
      s1.get(sid,{}).get('nickname') or rm.get(sid,{}).get('nickname'),s1.get(sid,{}).get('chat_earns') or 0,
      s1.get(sid,{}).get('online_time') or 0,s1.get(sid,{}).get('ten_minutes_reply_ratio') or 0,
      rm.get(sid,{}).get('receive_diamonds') or 0,float(rm.get(sid,{}).get('on_mic_time') or 0),
      rm.get(sid,{}).get('new_fans') or 0,sid in now_sids) for sid in sids]
    execute_values(cur,"""INSERT INTO linke_live_metric_snapshot
      (sid,guild,snapshot_at_utc,income_generated_at_utc,source_timezone,time_precision,nickname,chat_earns,online_time,
       ten_min_reply,room_diamonds,on_mic_time,new_fans,online_now) VALUES %s
      ON CONFLICT (sid,guild,snapshot_at_utc) DO NOTHING""",metric_rows)
    conn.commit()
cur.close();conn.close()
print(f"入库 {len(rows)} 行 ({GUILD}, {D_ISO}) ✅")
