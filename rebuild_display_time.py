#!/usr/bin/env python3
"""Build the shadow display-time fact without mutating raw source facts."""
import datetime as dt
import hashlib
import json
import sys
from collections import defaultdict
from decimal import Decimal
import psycopg2
from psycopg2.extras import execute_values, Json

from linky_runtime import database_url_from_environment

VERSION = 'DISPLAY_TIME_V1'
today_utc = dt.datetime.now(dt.timezone.utc).date()
date_from = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else today_utc - dt.timedelta(days=6)
date_to = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else today_utc
if date_from > date_to:
    raise SystemExit('from date must be <= to date')
DSN = (database_url_from_environment() or '').split('?')[0]
if not DSN:
    raise SystemExit('DATABASE_URL is required')
conn = psycopg2.connect(DSN)
cur = conn.cursor()

def validate_allocation_manifests(cursor, start_date, end_date):
    """Fail closed before commit when a settled source scope is only partly rebuilt."""
    cursor.execute("""
      WITH expected AS (
        SELECT m.date::date AS business_date, COALESCE(h.guildname,'') AS guild,
               COUNT(*) AS subject_count,
               SUM(COALESCE(m.paiddiamondtotal,0))::numeric AS total
        FROM metrics_daily m JOIN hosts h ON h.id=m.hostid
        WHERE m.date::date BETWEEN %s AND %s AND COALESCE(m.paiddiamondtotal,0)>0
        GROUP BY 1,2
      ), actual AS (
        SELECT business_date, guild, COUNT(DISTINCT subject_id) AS subject_count,
               SUM(allocated_amount)::numeric AS total
        FROM diamond_income_time_allocation
        WHERE business_date BETWEEN %s AND %s AND allocation_version=%s
          AND source='LINKY_BI'
        GROUP BY 1,2
      )
      SELECT COALESCE(e.business_date,a.business_date), COALESCE(e.guild,a.guild),
             e.subject_count, a.subject_count, e.total, a.total
      FROM expected e FULL OUTER JOIN actual a USING (business_date,guild)
      WHERE e.business_date IS NULL OR a.business_date IS NULL
         OR e.subject_count<>a.subject_count OR ABS(e.total-a.total)>0.000001
      ORDER BY 1,2
    """, (start_date,end_date,start_date,end_date,VERSION))
    linky_mismatches = cursor.fetchall()

    cursor.execute("""
      WITH expected AS (
        SELECT t.stat_date_bj AS business_date, t.country, TRIM(t.guild_name) AS guild,
               COUNT(DISTINCT t.timo_id) AS subject_count,
               SUM(t.total_income)::numeric AS total,
               BOOL_AND(NOT t.provisional) AS settled
        FROM external_timo_revenue_daily_staging t
        WHERE t.stat_date_bj BETWEEN %s AND %s AND t.total_income>0
          AND EXISTS (
            SELECT 1 FROM guild_source_dictionary d
            WHERE d.active AND d.source_key='TIMO'
              AND d.raw_country=t.country AND d.raw_guild=TRIM(t.guild_name)
              AND t.stat_date_bj>=d.effective_from
              AND (d.effective_to IS NULL OR t.stat_date_bj<=d.effective_to)
          )
        GROUP BY 1,2,3
      ), actual AS (
        SELECT business_date, COALESCE(metadata->>'country','') AS country, TRIM(guild) AS guild,
               COUNT(DISTINCT subject_id) AS subject_count,
               SUM(allocated_amount)::numeric AS total,
               BOOL_AND(is_settled) AS settled
        FROM diamond_income_time_allocation
        WHERE business_date BETWEEN %s AND %s AND allocation_version=%s AND source='TIMO'
        GROUP BY 1,2,3
      )
      SELECT COALESCE(e.business_date,a.business_date), COALESCE(e.country,a.country),
             COALESCE(e.guild,a.guild), e.subject_count, a.subject_count,
             e.total, a.total, e.settled, a.settled
      FROM expected e FULL OUTER JOIN actual a USING (business_date,country,guild)
      WHERE e.business_date IS NULL OR a.business_date IS NULL
         OR e.subject_count<>a.subject_count OR ABS(e.total-a.total)>0.000001
         OR e.settled IS DISTINCT FROM a.settled
      ORDER BY 1,2,3
    """, (start_date,end_date,start_date,end_date,VERSION))
    timo_mismatches = cursor.fetchall()
    if linky_mismatches or timo_mismatches:
        raise RuntimeError(json.dumps({
            'error': 'allocation_manifest_mismatch',
            'linky': [[str(value) for value in row] for row in linky_mismatches[:20]],
            'timo': [[str(value) for value in row] for row in timo_mismatches[:20]],
        }, ensure_ascii=False, separators=(',',':')))

def stable_int(text):
    return int(hashlib.sha256(text.encode()).hexdigest()[:16],16)

def utc_for_local_hour(day, hour, offset_minutes):
    local = dt.datetime.combine(day,dt.time(hour),tzinfo=dt.timezone.utc)
    return local - dt.timedelta(minutes=offset_minutes)

def config(key, default_offset=0):
    cur.execute("SELECT source_timezone,source_utc_offset_minutes FROM data_source_config WHERE source_key=%s AND enabled",(key,))
    row=cur.fetchone()
    return (row[0],int(row[1] or 0)) if row else (f'UTC{default_offset/60:+g}',default_offset)

linky_tz,linky_offset=config('linke_realtime',0)
timo_tz,timo_offset=config('timo_daily',-180)
alloc=[]

def add(source,key,subject,guild,bdate,when,amount,quality,method,tz,settled,meta=None):
    amount=Decimal(amount or 0)
    if amount <= 0: return
    alloc.append((source,key,str(subject),guild,bdate,when,amount,quality,method,tz,VERSION,settled,Json(meta or {})))

# Linky: BI is authoritative whenever the day is settled.
cur.execute("""
 SELECT m.id,h.sid,COALESCE(h.guildname,''),m.date::date,COALESCE(m.paiddiamondtotal,0)
 FROM metrics_daily m JOIN hosts h ON h.id=m.hostid
 WHERE m.date::date BETWEEN %s AND %s AND COALESCE(m.paiddiamondtotal,0)>0
 ORDER BY m.date::date,m.id
""",(date_from,date_to))
bi_by_day=defaultdict(list)
for row in cur.fetchall(): bi_by_day[row[3]].append(row)

for day,bi_rows in bi_by_day.items():
    start=utc_for_local_hour(day,0,linky_offset)
    end=start+dt.timedelta(days=1)
    cur.execute("""
      SELECT sid,hour_utc,COALESCE(chat_earns_delta,0)
      FROM linke_host_hourly_utc
      WHERE hour_utc >= %s AND hour_utc < %s AND COALESCE(chat_earns_delta,0)>0
      ORDER BY sid,hour_utc
    """,(start,end))
    real=defaultdict(list)
    for sid,hour,amount in cur.fetchall(): real[str(sid)].append((hour,Decimal(amount)))
    cur.execute("""
      SELECT sid,income_generated_at_utc,adjustment_diamond,attribution_method,anchor_sid,confidence
      FROM linky_bi_income_hour_attribution
      WHERE stat_date=%s AND income_generated_at_utc IS NOT NULL AND adjustment_diamond>0
    """,(day,))
    inferred={str(r[0]):r[1:] for r in cur.fetchall()}
    unresolved=[]
    for mid,sid,guild,bdate,bi_amount in bi_rows:
        sid=str(sid); bi_amount=Decimal(bi_amount); used=Decimal(0)
        events=real.get(sid,[])
        real_total=sum((x[1] for x in events),Decimal(0))
        scale=min(Decimal(1),bi_amount/real_total) if real_total>0 else Decimal(1)
        for n,(hour,amount) in enumerate(events):
            value=amount*scale; used+=value
            add('LINKY_BI',f'md:{mid}:real:{n}',sid,guild,bdate,hour,value,'REAL',
                'REALTIME_DELTA_BI_CAPPED',linky_tz,True,{'bi_total':str(bi_amount)})
        remaining=max(bi_amount-used,Decimal(0))
        inf=inferred.get(sid)
        if remaining>0 and inf:
            hour,known_adjustment,method,anchor_sid,confidence=inf
            value=min(remaining,Decimal(known_adjustment)); remaining-=value
            add('LINKY_BI',f'md:{mid}:inferred',sid,guild,bdate,hour,value,'INFERRED',
                method,linky_tz,True,{'anchor_sid':anchor_sid,'confidence':str(confidence)})
        if remaining>0: unresolved.append((mid,sid,guild,bdate,remaining,bi_amount))
    # Fixed shuffle removes BI source-order/revenue-sort bias, then balances row count across 24 hours.
    unresolved.sort(key=lambda r:stable_int(f'LINKY|{r[3]}|{r[1]}|{r[0]}'))
    n=len(unresolved)
    for idx,(mid,sid,guild,bdate,remaining,bi_amount) in enumerate(unresolved):
        hour=min(23,(idx*24)//max(n,1))
        when=utc_for_local_hour(bdate,hour,linky_offset)
        add('LINKY_BI',f'md:{mid}:simulated',sid,guild,bdate,when,remaining,'SIMULATED',
            'STABLE_SHUFFLE_24H',linky_tz,True,{'bi_total':str(bi_amount),'rank':idx+1,'population':n})

# Linky provisional day(s) without BI: real deltas plus the first cumulative baseline distributed only into elapsed hours.
day=date_from
while day<=date_to:
    if day not in bi_by_day:
        start=utc_for_local_hour(day,0,linky_offset); end=start+dt.timedelta(days=1)
        cur.execute("""
          SELECT sid,guild,hour_utc,chat_earns_cumulative,chat_earns_delta,
                 MIN(hour_utc) OVER(PARTITION BY sid,guild) first_hour,
                 MAX(chat_earns_cumulative) OVER(PARTITION BY sid,guild) latest_total
          FROM linke_host_hourly_utc WHERE hour_utc >= %s AND hour_utc < %s
          ORDER BY sid,guild,hour_utc
        """,(start,end))
        rows=cur.fetchall(); by_sid=defaultdict(list)
        for r in rows: by_sid[(str(r[0]),r[1])].append(r)
        for (sid,guild),events in by_sid.items():
            delta_total=Decimal(0)
            for n,r in enumerate(events):
                delta=Decimal(r[4] or 0)
                if delta>0:
                    delta_total+=delta
                    add('LINKY_REALTIME',f'{sid}:{guild}:{r[2].isoformat()}',sid,guild,day,r[2],delta,'REAL',
                        'SNAPSHOT_DELTA',linky_tz,False)
            latest=Decimal(events[-1][6] or 0)
            baseline=max(latest-delta_total,Decimal(0))
            if baseline>0:
                first_local=events[0][5]+dt.timedelta(minutes=linky_offset)
                elapsed=max(1,first_local.hour+1)
                hour=stable_int(f'LINKY_BASELINE|{day}|{sid}|{guild}')%elapsed
                add('LINKY_REALTIME',f'{sid}:{guild}:{day}:baseline',sid,guild,day,
                    utc_for_local_hour(day,hour,linky_offset),baseline,'SIMULATED','FIRST_SNAPSHOT_BASELINE_ELAPSED_HOURS',
                    linky_tz,False,{'elapsed_hours':elapsed})
    day+=dt.timedelta(days=1)

# Timo: each day's first cumulative snapshot is a baseline; later snapshot differences are REAL.
cur.execute("""
 SELECT stat_date,country,guild_name,timo_id,downloaded_at_utc,income_generated_at_utc,
        source_timezone,total_income,
        LAG(total_income) OVER(PARTITION BY stat_date,country,guild_name,timo_id ORDER BY downloaded_at_utc) prev,
        LAG(downloaded_at_utc) OVER(PARTITION BY stat_date,country,guild_name,timo_id ORDER BY downloaded_at_utc) prev_downloaded_at_utc
 FROM external_timo_revenue_metric_snapshot s
 WHERE s.stat_date BETWEEN %s AND %s
   AND EXISTS (
     SELECT 1 FROM guild_source_dictionary d
     WHERE d.active AND d.source_key='TIMO'
       AND d.raw_country=s.country AND d.raw_guild=trim(s.guild_name)
       AND s.stat_date>=d.effective_from
       AND (d.effective_to IS NULL OR s.stat_date<=d.effective_to)
   )
 ORDER BY stat_date,country,guild_name,timo_id,downloaded_at_utc
""",(date_from,date_to))
timo=defaultdict(list)
for r in cur.fetchall(): timo[(r[0],r[1],r[2],r[3])].append(r)
cur.execute("""
 SELECT stat_date_bj,country,guild_name,timo_id,total_income,provisional
 FROM external_timo_revenue_daily_staging t
 WHERE t.stat_date_bj BETWEEN %s AND %s AND t.total_income>0
   AND EXISTS (
     SELECT 1 FROM guild_source_dictionary d
     WHERE d.active AND d.source_key='TIMO'
       AND d.raw_country=t.country AND d.raw_guild=trim(t.guild_name)
       AND t.stat_date_bj>=d.effective_from
       AND (d.effective_to IS NULL OR t.stat_date_bj<=d.effective_to)
   )
""",(date_from,date_to))
timo_daily={(r[0],r[1],r[2],r[3]):r for r in cur.fetchall()}
for key,daily in timo_daily.items():
    day,country,guild,tid=key
    events=timo.get(key,[])
    # A non-zero snapshot baseline is better than the daily fallback; zero provisional snapshots do not erase finalized daily facts.
    if events and Decimal(events[-1][7] or 0)>0:
        first=events[0]; baseline=Decimal(first[7] or 0)
        if baseline>0:
            first_local=first[4]+dt.timedelta(minutes=timo_offset)
            local_today=dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=timo_offset)
            elapsed=max(1,first_local.hour+1) if day==local_today.date() else 24
            hour=stable_int(f'TIMO_BASELINE|{day}|{country}|{guild}|{tid}')%elapsed
            add('TIMO',f'{country}:{guild}:{tid}:{day}:baseline',tid,guild,day,
                utc_for_local_hour(day,hour,timo_offset),baseline,'SIMULATED','FIRST_SNAPSHOT_BASELINE_ELAPSED_HOURS',
                timo_tz,not bool(daily[5]),{'country':country,'elapsed_hours':elapsed})
        for r in events[1:]:
            delta=max(Decimal(r[7] or 0)-Decimal(r[8] or 0),Decimal(0))
            if delta>0:
                interval_minutes=max(0,int((r[4]-r[9]).total_seconds()//60))
                # The source labels the previous source hour. It is a real hourly
                # delta only while downloads are approximately hourly; a wider
                # cumulative window is anchored evidence, but its exact hour is inferred.
                quality='REAL' if interval_minutes<=90 else 'INFERRED'
                method='SNAPSHOT_DELTA' if quality=='REAL' else 'MULTI_HOUR_SNAPSHOT_DELTA_PREVIOUS_HOUR'
                add('TIMO',f'{country}:{guild}:{tid}:{r[4].isoformat()}',tid,guild,day,r[5],delta,quality,
                    method,r[6],not bool(daily[5]),{'country':country,'snapshot_interval_minutes':interval_minutes})
    else:
        total=Decimal(daily[4] or 0)
        hour=stable_int(f'TIMO_DAILY|{day}|{country}|{guild}|{tid}')%24
        add('TIMO',f'{country}:{guild}:{tid}:{day}:daily',tid,guild,day,
            utc_for_local_hour(day,hour,timo_offset),total,'SIMULATED','STABLE_HASH_24H_DAILY',
            timo_tz,not bool(daily[5]),{'country':country})

cur.execute("DELETE FROM diamond_income_time_allocation WHERE business_date BETWEEN %s AND %s AND allocation_version=%s",
            (date_from,date_to,VERSION))
if alloc:
    execute_values(cur,"""
      INSERT INTO diamond_income_time_allocation
      (source,source_record_key,subject_id,guild,business_date,display_time_utc,allocated_amount,
       time_quality,allocation_method,source_timezone,allocation_version,is_settled,metadata)
      VALUES %s ON CONFLICT DO NOTHING
    """,alloc,page_size=5000)
try:
    validate_allocation_manifests(cur,date_from,date_to)
except Exception:
    conn.rollback()
    cur.close()
    conn.close()
    raise
conn.commit()
cur.execute("""
 SELECT source,time_quality,COUNT(*),ROUND(SUM(allocated_amount))
 FROM diamond_income_time_allocation
 WHERE business_date BETWEEN %s AND %s AND allocation_version=%s
 GROUP BY source,time_quality ORDER BY source,time_quality
""",(date_from,date_to,VERSION))
print(json.dumps({'from':str(date_from),'to':str(date_to),'version':VERSION,'allocations':len(alloc),
                  'summary':[(a,b,c,str(d)) for a,b,c,d in cur.fetchall()]},ensure_ascii=False))
cur.close();conn.close()
