#!/usr/bin/env python3
"""Apply an evidence-bound direct-ID reconciliation with an exact preimage backup."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def load_evidence(path: Path, expected_scope: str, expected_guild_count: int) -> dict[str, Any]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    checksum = str(evidence.pop("evidenceChecksum", ""))
    if evidence.get("schemaVersion") != 1 or evidence.get("sourceScope") != expected_scope:
        raise ValueError(f"evidence_contract_invalid:{path}")
    if evidence.get("configuredGuildCount") != expected_guild_count:
        raise ValueError(f"evidence_guild_scope_invalid:{path}")
    if canonical_hash(evidence) != checksum:
        raise ValueError(f"evidence_checksum_invalid:{path}")
    generated_at = dt.datetime.fromisoformat(str(evidence["generatedAt"]).replace("Z", "+00:00"))
    if generated_at.tzinfo is None or dt.datetime.now(dt.timezone.utc) - generated_at > dt.timedelta(hours=24):
        raise ValueError(f"evidence_stale:{path}")
    results = evidence.get("results")
    if not isinstance(results, list) or len(results) != evidence.get("targetCount"):
        raise ValueError(f"evidence_results_invalid:{path}")
    target_ids = sorted(str(row.get("subjectId", "")) for row in results)
    if any(not subject_id.isdigit() for subject_id in target_ids):
        raise ValueError(f"evidence_subject_invalid:{path}")
    if canonical_hash(target_ids) != evidence.get("targetChecksum"):
        raise ValueError(f"evidence_target_checksum_invalid:{path}")
    evidence["evidenceChecksum"] = checksum
    return evidence


def build_plan(active_subject_ids: list[str], linky: dict[str, Any], timo: dict[str, Any]) -> dict[str, Any]:
    active = sorted(set(active_subject_ids))
    if len(active) != len(active_subject_ids):
        raise ValueError("active_subject_ids_duplicate")
    linky_rows = {str(row["subjectId"]): row for row in linky["results"]}
    timo_rows = {str(row["subjectId"]): row for row in timo["results"]}
    expected_linky = sorted(subject_id for subject_id in active if len(subject_id) == 8)
    expected_timo = sorted(subject_id for subject_id in active if len(subject_id) == 12)
    if not set(expected_linky).issubset(linky_rows) or not set(expected_timo).issubset(timo_rows):
        raise ValueError("active_target_set_drift")
    plan = {
        "invalid": [], "correctLinky": [], "deleteLinkyNotFound": [],
        "deleteTimoNotFound": [], "ambiguous": [],
    }
    for subject_id in active:
        if len(subject_id) not in (8, 12):
            plan["invalid"].append(subject_id)
            continue
        row = linky_rows[subject_id] if len(subject_id) == 8 else timo_rows[subject_id]
        status = str(row.get("status"))
        matches = row.get("matches") if isinstance(row.get("matches"), list) else []
        if status == "FOUND" and len(matches) == 1 and len(subject_id) == 8:
            plan["correctLinky"].append({"subjectId": subject_id, **matches[0]})
        elif status == "NOT_FOUND" and not matches:
            key = "deleteLinkyNotFound" if len(subject_id) == 8 else "deleteTimoNotFound"
            plan[key].append(subject_id)
        else:
            plan["ambiguous"].append({"subjectId": subject_id, "status": status, "matches": matches})
    plan["summary"] = {key: len(value) for key, value in plan.items() if key != "summary"}
    plan["activeTargetCount"] = len(active)
    plan["activeTargetChecksum"] = canonical_hash(active)
    return plan


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def query_active_subject_ids(connection: Any, *, lock: bool) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT d.subject_id
          FROM fan_direct_ownerships d
          LEFT JOIN fan_subject_identities i USING(platform,subject_id)
          WHERE d.platform='LINKY' AND d.ended_at IS NULL
            AND (i.joined_guild_date IS NULL OR length(d.subject_id) NOT IN (8,12))
          ORDER BY d.subject_id
        """ + (" FOR UPDATE OF d" if lock else ""))
        return [str(row[0]) for row in cursor.fetchall()]


def guild_mappings(connection: Any) -> dict[str, tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT DISTINCT ON (raw_guild) raw_guild,guild_alias,country
          FROM guild_source_dictionary
          WHERE source_key='LINKY' AND active AND effective_from<=CURRENT_DATE
            AND (effective_to IS NULL OR effective_to>=CURRENT_DATE)
          ORDER BY raw_guild,effective_from DESC""")
        return {str(raw): (str(alias), str(country)) for raw, alias, country in cursor.fetchall()}


def rows_as_dicts(connection: Any, sql: str, values: list[str]) -> list[dict[str, Any]]:
    from psycopg2.extras import RealDictCursor
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(sql, (values,))
        return [dict(row) for row in cursor.fetchall()]


def backup_preimage(connection: Any, plan: dict[str, Any], output: Path) -> None:
    ids = sorted({
        *plan["invalid"], *plan["deleteLinkyNotFound"], *plan["deleteTimoNotFound"],
        *(row["subjectId"] for row in plan["correctLinky"]),
    })
    tables = {}
    for table in (
        "fan_direct_ownerships", "fan_subject_identities", "fan_subject_contact_resolution",
        "fan_subject_join_date_lookup_state", "fan_invalid_subject_quarantine",
    ):
        tables[table] = rows_as_dicts(
            connection, f"SELECT * FROM {table} WHERE platform='LINKY' AND subject_id=ANY(%s::text[]) ORDER BY subject_id", ids)
    payload = {
        "schemaVersion": 1,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan": plan,
        "tables": tables,
    }
    payload["backupChecksum"] = canonical_hash(payload)
    atomic_json(output, payload)


def assert_no_delete_dependencies(connection: Any, ids: list[str]) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
          SELECT
            (SELECT count(*) FROM fan_first_deposit_registrations WHERE platform='LINKY' AND subject_id=ANY(%s::text[])),
            (SELECT count(*) FROM fan_direct_transfer_requests WHERE platform='LINKY' AND subject_id=ANY(%s::text[])),
            (SELECT count(*) FROM fan_direct_ownerships WHERE platform='LINKY' AND predecessor_ownership_id IN (
              SELECT id FROM fan_direct_ownerships WHERE platform='LINKY' AND subject_id=ANY(%s::text[])))
        """, (ids, ids, ids))
        counts = tuple(int(value) for value in cursor.fetchone())
    if counts != (0, 0, 0):
        raise RuntimeError(f"delete_dependencies_present:{counts}")


def apply_plan(connection: Any, plan: dict[str, Any], linky: dict[str, Any], timo: dict[str, Any]) -> None:
    if plan["ambiguous"]:
        raise RuntimeError("ambiguous_subjects_present")
    mappings = guild_mappings(connection)
    for row in plan["correctLinky"]:
        raw_guild = str(row["rawGuild"])
        if raw_guild not in mappings:
            raise RuntimeError(f"formal_guild_mapping_missing:{raw_guild}")
        guild_name, country = mappings[raw_guild]
        joined_date = str(row["joinedGuildDate"])
        subject_id = str(row["subjectId"])
        with connection.cursor() as cursor:
            cursor.execute("""
              INSERT INTO fan_subject_identities(platform,subject_id,guild_id,guild_name,joined_guild_date,newcomer_revision)
              VALUES('LINKY',%s,%s,%s,%s::date,1)
              ON CONFLICT(platform,subject_id) DO UPDATE SET guild_id=EXCLUDED.guild_id,
                guild_name=EXCLUDED.guild_name,joined_guild_date=EXCLUDED.joined_guild_date,updated_at=now()
            """, (subject_id, raw_guild, guild_name, joined_date))
            cursor.execute("""
              UPDATE fan_direct_ownerships SET country=%s,guild_name=%s,
                source_snapshot=coalesce(source_snapshot,'{}'::jsonb) || jsonb_build_object(
                  'formalGuildLookup',jsonb_build_object(
                    'source','LINKY_OFFICIAL_ALL_GUILD_ROSTER','rawGuild',%s::text,
                    'guildName',%s::text,'country',%s::text,'joinedGuildDate',%s::text,
                    'evidenceChecksum',%s::text))
              WHERE platform='LINKY' AND subject_id=%s AND ended_at IS NULL
            """, (country, guild_name, raw_guild, guild_name, country, joined_date,
                    linky["evidenceChecksum"], subject_id))
            cursor.execute("""
              INSERT INTO fan_direct_audit_events(platform,subject_id,event_type,payload_json)
              VALUES('LINKY',%s,'SOURCE_BACKFILLED',jsonb_build_object(
                'sourceAction','ALL_GUILD_PROFILE_SYNCED','sourceRawGuild',%s::text,
                'guildName',%s::text,'country',%s::text,'joinedGuildDate',%s::text,
                'evidenceChecksum',%s::text))
            """, (subject_id, raw_guild, guild_name, country, joined_date, linky["evidenceChecksum"]))
    delete_ids = sorted({*plan["invalid"], *plan["deleteLinkyNotFound"], *plan["deleteTimoNotFound"]})
    assert_no_delete_dependencies(connection, delete_ids)
    invalid = set(plan["invalid"])
    not_found_linky = set(plan["deleteLinkyNotFound"])
    not_found_timo = set(plan["deleteTimoNotFound"])
    with connection.cursor() as cursor:
        for subject_id in delete_ids:
            if subject_id in invalid:
                reason = "INVALID_ID_LENGTH"
                evidence_checksum = canonical_hash({"subjectId": subject_id, "policy": "LINKY_8_TIMO_12"})
            elif subject_id in not_found_linky:
                reason = "OFFICIAL_NOT_IN_ANY_LINKY_GUILD"
                evidence_checksum = linky["evidenceChecksum"]
            else:
                reason = "OFFICIAL_NOT_IN_ANY_TIMO_GUILD"
                evidence_checksum = timo["evidenceChecksum"]
            cursor.execute("""
              INSERT INTO fan_direct_audit_events(platform,subject_id,event_type,payload_json)
              SELECT platform,subject_id,'INVALID_QUARANTINED',jsonb_build_object(
                'ownershipId',id,'agentId',agent_id,'reasonCode',%s::text,
                'sourceAction','DIRECT_ID_DELETED','evidenceChecksum',%s::text)
              FROM fan_direct_ownerships
              WHERE platform='LINKY' AND subject_id=%s AND ended_at IS NULL
            """, (reason, evidence_checksum, subject_id))
            if subject_id not in invalid:
                quarantine_platform = "LINKY" if subject_id in not_found_linky else "TIMO"
                source_scope = ("linky_official_all_configured_guild_rosters"
                                if subject_id in not_found_linky else "timo_official_all_three_guild_current_rosters")
                evidence = linky if subject_id in not_found_linky else timo
                cursor.execute("""
                  INSERT INTO fan_invalid_subject_quarantine(
                    platform,subject_id,reason_code,source_scope,source_guild_id,source_country,
                    source_snapshot_at,source_generation,source_checksum)
                  VALUES(%s,%s,'OFFICIAL_NOT_IN_TARGET_GUILD',%s,'ALL_FORMAL_GUILDS','ALL',
                    %s::timestamptz,%s,%s)
                  ON CONFLICT(platform,subject_id) DO UPDATE SET active=true,
                    reason_code=EXCLUDED.reason_code,source_scope=EXCLUDED.source_scope,
                    source_guild_id=EXCLUDED.source_guild_id,source_country=EXCLUDED.source_country,
                    source_snapshot_at=EXCLUDED.source_snapshot_at,
                    source_generation=EXCLUDED.source_generation,source_checksum=EXCLUDED.source_checksum,
                    last_confirmed_at=now(),resolved_at=NULL,resolution_code=NULL,updated_at=now()
                """, (quarantine_platform, subject_id, source_scope, evidence["generatedAt"],
                        "all-guild-roster-audit-v1", evidence_checksum))
        cursor.execute("DELETE FROM fan_subject_contact_resolution WHERE platform='LINKY' AND subject_id=ANY(%s::text[])", (delete_ids,))
        cursor.execute("DELETE FROM fan_subject_identities WHERE platform='LINKY' AND subject_id=ANY(%s::text[])", (delete_ids,))
        cursor.execute("DELETE FROM fan_direct_ownerships WHERE platform='LINKY' AND subject_id=ANY(%s::text[]) AND ended_at IS NULL", (delete_ids,))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--linky-evidence", type=Path, required=True)
    parser.add_argument("--timo-evidence", type=Path, required=True)
    parser.add_argument("--plan-output", type=Path, required=True)
    parser.add_argument("--backup-output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.backup_output:
        raise SystemExit("--backup-output is required with --apply")
    linky = load_evidence(args.linky_evidence, "linky_official_all_configured_guild_rosters", 10)
    timo = load_evidence(args.timo_evidence, "timo_official_all_three_guild_current_rosters", 3)
    import psycopg2
    with psycopg2.connect(os.environ["DATABASE_URL"]) as connection:
        if args.apply:
            active = query_active_subject_ids(connection, lock=True)
            plan = build_plan(active, linky, timo)
            backup_preimage(connection, plan, args.backup_output)
            apply_plan(connection, plan, linky, timo)
        else:
            connection.set_session(readonly=True)
            plan = build_plan(query_active_subject_ids(connection, lock=False), linky, timo)
    plan["linkyEvidenceChecksum"] = linky["evidenceChecksum"]
    plan["timoEvidenceChecksum"] = timo["evidenceChecksum"]
    plan["applied"] = args.apply
    atomic_json(args.plan_output, plan)
    print(json.dumps({"applied": args.apply, "summary": plan["summary"],
                      "planChecksum": canonical_hash(plan)}, ensure_ascii=False, separators=(",", ":")))
    return 0 if not plan["ambiguous"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
