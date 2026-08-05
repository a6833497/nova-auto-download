"""Independent database consumers for a complete Linky FetchBundle."""

from __future__ import annotations

import datetime as dt
from typing import Any

from psycopg2.extras import execute_values


def bundle_date(bundle: Any) -> dt.date:
    return dt.datetime.strptime(bundle.business_date, "%Y%m%d").date()


def validate_complete_bundle(bundle: Any) -> None:
    for scan in (bundle.streamer_scan, bundle.voice_room_scan):
        if not scan.scan_complete or scan.raw_row_count != scan.reported_total:
            raise ValueError(f"incomplete Linky bundle: {scan.endpoint}")


def build_ledger_rows(bundle: Any) -> list[tuple[Any, ...]]:
    validate_complete_bundle(bundle)
    stat_date = bundle_date(bundle)
    settled = stat_date < dt.datetime.now(dt.timezone.utc).date()
    chat_by_sid = {int(row["sid"]): row for row in bundle.streamer_rows}
    room_by_sid = {int(row["sid"]): row for row in bundle.voice_room_rows}
    values = []
    for sid in sorted(set(chat_by_sid) | set(room_by_sid)):
        chat = chat_by_sid.get(sid, {})
        voice = room_by_sid.get(sid, {})
        values.append((bundle.source_guild, sid, stat_date,
            chat.get("chat_earns") or 0, chat.get("voice_call_earns") or 0,
            chat.get("text_earns") or 0, chat.get("unlock_image_earns") or 0,
            chat.get("task_earns") or 0, chat.get("other_earns") or 0,
            voice.get("receive_diamonds") or 0, chat.get("online_time") or 0,
            float(voice.get("on_mic_time") or 0), voice.get("new_fans") or 0,
            chat.get("ten_minutes_reply_ratio") or 0, chat.get("new_level4_num") or 0, settled))
    return values


def write_daily_ledger(connection: Any, bundle: Any) -> int:
    values = build_ledger_rows(bundle)
    stat_date = bundle_date(bundle)
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM linke_streamer_daily WHERE stat_date=%s AND guild=%s AND settled=true LIMIT 1",
            (stat_date, bundle.source_guild))
        locked = cursor.fetchone() is not None and (dt.datetime.now(dt.timezone.utc).date() - stat_date).days > 2
        if locked:
            raise RuntimeError(f"historical ledger is write-protected: {bundle.source_guild} {stat_date}")
        if values:
            execute_values(cursor, """INSERT INTO linke_streamer_daily
              (guild,sid,stat_date,chat_earns,voice_call_earns,text_earns,unlock_image_earns,task_earns,other_earns,
               room_diamonds,online_time,on_mic_time,new_fans,ten_min_reply,new_level4,settled) VALUES %s
              ON CONFLICT (sid,stat_date) DO UPDATE SET
               chat_earns=EXCLUDED.chat_earns,voice_call_earns=EXCLUDED.voice_call_earns,
               text_earns=EXCLUDED.text_earns,unlock_image_earns=EXCLUDED.unlock_image_earns,
               task_earns=EXCLUDED.task_earns,other_earns=EXCLUDED.other_earns,
               room_diamonds=EXCLUDED.room_diamonds,online_time=EXCLUDED.online_time,
               on_mic_time=EXCLUDED.on_mic_time,new_fans=EXCLUDED.new_fans,
               ten_min_reply=EXCLUDED.ten_min_reply,new_level4=EXCLUDED.new_level4,
               settled=EXCLUDED.settled,fetched_at=now()""", values)
    return len(values)


def build_live_rows(bundle: Any) -> list[tuple[Any, ...]]:
    validate_complete_bundle(bundle)
    chat_by_sid = {int(row["sid"]): row for row in bundle.streamer_rows}
    room_by_sid = {int(row["sid"]): row for row in bundle.voice_room_rows}
    online_sids = set(bundle.online_anchor_sids or [])
    rows = []
    for sid in sorted(set(chat_by_sid) | set(room_by_sid) | online_sids):
        chat = chat_by_sid.get(sid, {})
        voice = room_by_sid.get(sid, {})
        rows.append((bundle.source_guild, sid, bundle_date(bundle),
            chat.get("nickname") or voice.get("nickname") or None,
            chat.get("chat_earns") or 0, chat.get("online_time") or 0,
            chat.get("ten_minutes_reply_ratio") or 0, voice.get("receive_diamonds") or 0,
            float(voice.get("on_mic_time") or 0), voice.get("new_fans") or 0, sid in online_sids))
    return rows


def write_live_views(connection: Any, bundle: Any, snapshot_slot: dt.datetime) -> int:
    rows = build_live_rows(bundle)
    if not rows:
        return 0
    with connection.cursor() as cursor:
        execute_values(cursor, """INSERT INTO linke_live_today
          (guild,sid,snap_date,nickname,chat_earns,online_time,ten_min_reply,room_diamonds,on_mic_time,new_fans,online_now)
          VALUES %s ON CONFLICT (sid,snap_date) DO UPDATE SET
          nickname=COALESCE(EXCLUDED.nickname,linke_live_today.nickname),chat_earns=EXCLUDED.chat_earns,
          online_time=EXCLUDED.online_time,ten_min_reply=EXCLUDED.ten_min_reply,
          room_diamonds=EXCLUDED.room_diamonds,on_mic_time=EXCLUDED.on_mic_time,
          new_fans=EXCLUDED.new_fans,online_now=EXCLUDED.online_now,fetched_at=now()""", rows)
        nicknames = [(row[1], str(row[3] or "").strip()) for row in rows if str(row[3] or "").strip()]
        if nicknames:
            execute_values(cursor, """UPDATE hosts AS h SET name=v.nickname,updatedat=now()
              FROM (VALUES %s) AS v(sid,nickname)
              WHERE h.sid=v.sid::text AND (h.name IS NULL OR trim(h.name)='' OR trim(h.name)=trim(h.sid))""", nicknames)
        cursor.execute("SELECT source_timezone,source_utc_offset_minutes FROM data_source_config "
            "WHERE source_key='linke_realtime' AND enabled=true")
        source_timezone, source_offset = cursor.fetchone() or ("UTC+0", 0)
        source_local = snapshot_slot + dt.timedelta(minutes=int(source_offset or 0))
        previous = source_local.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
        generated = (previous - dt.timedelta(minutes=int(source_offset or 0))).replace(tzinfo=dt.timezone.utc)
        execute_values(cursor, """INSERT INTO linke_live_snapshot (sid,guild,snapshot_at,online_now) VALUES %s
          ON CONFLICT (sid,snapshot_at) DO NOTHING""",
          [(row[1], bundle.source_guild, snapshot_slot.replace(tzinfo=None), row[10]) for row in rows])
        metrics = [(row[1], bundle.source_guild, snapshot_slot, generated, source_timezone, "MINUTE",
            row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10]) for row in rows]
        execute_values(cursor, """INSERT INTO linke_live_metric_snapshot
          (sid,guild,snapshot_at_utc,income_generated_at_utc,source_timezone,time_precision,nickname,chat_earns,
           online_time,ten_min_reply,room_diamonds,on_mic_time,new_fans,online_now) VALUES %s
          ON CONFLICT (sid,guild,snapshot_at_utc) DO NOTHING""", metrics)
    return len(rows)
