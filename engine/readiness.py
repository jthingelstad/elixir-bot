"""Durable materialization and source-freshness contracts.

Management is a judgment layer, not merely another projection.  It therefore
must be able to distinguish "the member is inactive" from "the evidence is
missing or stale."  This module owns that distinction and the per-tick record
that lets capabilities explain which materialization produced a verdict.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from engine.db import canon_tag
from engine.normalize import canonical_utc_timestamp, parse_cr_time

CLAN_MAX_AGE_MINUTES = 30
BATTLELOG_MAX_AGE_MINUTES = 360
PROFILE_MAX_AGE_MINUTES = 1440


def _utc(value) -> datetime:
    parsed = parse_cr_time(value)
    if parsed is None:
        raise ValueError(f"invalid readiness timestamp: {value!r}")
    return parsed.astimezone(timezone.utc)


def _age_minutes(now: datetime, value) -> float | None:
    parsed = parse_cr_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _freshness(value, *, now: datetime, max_age_minutes: int) -> dict:
    age = _age_minutes(now, value)
    return {
        "observed_at": canonical_utc_timestamp(value),
        "age_minutes": round(age, 1) if age is not None else None,
        "max_age_minutes": max_age_minutes,
        "fresh": age is not None and age <= max_age_minutes,
    }


def evaluate_source_freshness(conn, *, now, home_clan: str) -> dict:
    """Return the readiness decision for every active member.

    Clan freshness is a global prerequisite because roster membership and role
    come from that snapshot. Battlelog freshness gates each member's inactivity
    judgment. Profile freshness is reported for capability honesty, but it does
    not gate kick-state evaluation because that policy deliberately measures
    engagement from battles rather than API ``lastSeen``.
    """
    now_dt = _utc(now)
    as_of = canonical_utc_timestamp(now)
    clan_row = conn.execute(
        "SELECT MAX(observed_at) AS observed_at FROM state_baselines "
        "WHERE entity_kind = 'clan' AND entity_tag = ?",
        (canon_tag(home_clan),),
    ).fetchone()
    clan = _freshness(
        clan_row["observed_at"] if clan_row else None,
        now=now_dt,
        max_age_minutes=CLAN_MAX_AGE_MINUTES,
    )
    battle_stream_ready = conn.execute("SELECT 1 FROM battle_events LIMIT 1").fetchone() is not None
    rows = conn.execute(
        """SELECT cm.player_tag, ps.last_battlelog_poll, ps.last_profile_poll
           FROM clan_memberships cm
           LEFT JOIN poll_state ps ON ps.player_tag = cm.player_tag
           WHERE cm.left_at IS NULL
           ORDER BY cm.player_tag"""
    ).fetchall()
    members: dict[str, dict] = {}
    ready_count = 0
    for row in rows:
        battlelog = _freshness(
            row["last_battlelog_poll"],
            now=now_dt,
            max_age_minutes=BATTLELOG_MAX_AGE_MINUTES,
        )
        profile = _freshness(
            row["last_profile_poll"],
            now=now_dt,
            max_age_minutes=PROFILE_MAX_AGE_MINUTES,
        )
        reasons = []
        if not clan["fresh"]:
            reasons.append("clan_snapshot_stale")
        if not battlelog["fresh"]:
            reasons.append("battlelog_stale")
        if not battle_stream_ready:
            reasons.append("battle_stream_empty")
        status = "ready" if not reasons else "held"
        if status == "ready":
            ready_count += 1
        members[row["player_tag"]] = {
            "status": status,
            "reasons": reasons,
            "evidence_as_of": battlelog["observed_at"],
            "battlelog": battlelog,
            "profile": profile,
        }
    return {
        "as_of": as_of,
        "ready": bool(clan["fresh"]) and battle_stream_ready and ready_count == len(rows),
        "clan": clan,
        "battle_stream": {"ready": battle_stream_ready},
        "members_total": len(rows),
        "members_ready": ready_count,
        "members_held": len(rows) - ready_count,
        "members": members,
    }


def start_materialization(conn, *, started_at: str, run_kind: str = "scheduled") -> int:
    cur = conn.execute(
        "INSERT INTO materialization_runs (started_at, run_kind) VALUES (?, ?)",
        (canonical_utc_timestamp(started_at), run_kind),
    )
    return int(cur.lastrowid)


def add_materialization_input(conn, materialization_id: int, observation) -> int:
    """Link an admitted observation and its closest raw receipt to a run."""
    raw_key = str(observation.entity_key).lstrip("#").upper()
    receipt = conn.execute(
        """SELECT receipt_id FROM api_observation_receipts
           WHERE endpoint = ? AND UPPER(LTRIM(entity_key, '#')) = ?
             AND payload_hash = ?
           ORDER BY receipt_id DESC LIMIT 1""",
        (observation.endpoint, raw_key, observation.payload_hash),
    ).fetchone()
    receipt_id = int(receipt["receipt_id"]) if receipt else None
    cur = conn.execute(
        """INSERT OR IGNORE INTO materialization_inputs
               (materialization_id, receipt_id, endpoint, entity_key,
                observed_at, payload_hash, source, applied_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            materialization_id,
            receipt_id,
            observation.endpoint,
            observation.entity_key,
            observation.observed_at,
            observation.payload_hash,
            observation.source,
            observation.observed_at,
        ),
    )
    if receipt_id is not None:
        conn.execute(
            """UPDATE api_observation_receipts
               SET admission_status = 'accepted', admission_errors_json = '[]'
               WHERE receipt_id = ?""",
            (receipt_id,),
        )
    if cur.rowcount:
        return int(cur.lastrowid)
    row = conn.execute(
        """SELECT materialization_input_id FROM materialization_inputs
           WHERE materialization_id = ? AND endpoint = ? AND entity_key = ?
             AND observed_at = ? AND payload_hash = ?""",
        (
            materialization_id,
            observation.endpoint,
            observation.entity_key,
            observation.observed_at,
            observation.payload_hash,
        ),
    ).fetchone()
    return int(row["materialization_input_id"])


def record_admission_decision(conn, decision, payload) -> None:
    """Attach an endpoint contract decision to its latest network receipt."""
    if payload is None:
        return
    from engine.db import payload_hash

    raw_key = str(decision.entity_key).lstrip("#").upper()
    row = conn.execute(
        """SELECT receipt_id FROM api_observation_receipts
           WHERE endpoint = ? AND UPPER(LTRIM(entity_key, '#')) = ?
             AND payload_hash = ?
           ORDER BY receipt_id DESC LIMIT 1""",
        (decision.endpoint, raw_key, payload_hash(payload)),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """UPDATE api_observation_receipts
           SET admission_status = ?, admission_errors_json = ?
           WHERE receipt_id = ?""",
        (
            "accepted" if decision.accepted else "rejected",
            json.dumps(list(decision.errors), separators=(",", ":")),
            row["receipt_id"],
        ),
    )


def update_materialization(
    conn,
    materialization_id: int,
    *,
    status: str,
    completed_at: str | None = None,
    poll_ok: bool | None = None,
    apply_ok: bool | None = None,
    manage_ok: bool | None = None,
    derivations_ok: bool | None = None,
    source_freshness: dict | None = None,
    counters: dict | None = None,
    errors: dict | None = None,
) -> None:
    fields = ["status = ?"]
    values: list = [status]
    for column, value in (
        ("completed_at", canonical_utc_timestamp(completed_at)),
        ("poll_ok", None if poll_ok is None else int(poll_ok)),
        ("apply_ok", None if apply_ok is None else int(apply_ok)),
        ("manage_ok", None if manage_ok is None else int(manage_ok)),
        ("derivations_ok", None if derivations_ok is None else int(derivations_ok)),
        (
            "source_freshness_json",
            None
            if source_freshness is None
            else json.dumps(source_freshness, sort_keys=True, separators=(",", ":")),
        ),
        (
            "counters_json",
            None if counters is None else json.dumps(counters, sort_keys=True),
        ),
        ("error_json", None if errors is None else json.dumps(errors, sort_keys=True)),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            values.append(value)
    values.append(materialization_id)
    conn.execute(
        f"UPDATE materialization_runs SET {', '.join(fields)} WHERE materialization_id = ?",
        values,
    )


def latest_materialization(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM materialization_runs ORDER BY materialization_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("source_freshness_json", "counters_json", "error_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key, None) or "{}")
    return result


def generation_snapshot(conn) -> dict | None:
    """Return the exact applied generation visible to the current DB snapshot."""
    row = conn.execute(
        """SELECT mr.*, COUNT(mi.materialization_input_id) AS input_count
           FROM materialization_runs mr
           LEFT JOIN materialization_inputs mi
             ON mi.materialization_id = mr.materialization_id
           WHERE mr.apply_ok = 1 AND mr.status IN ('complete', 'partial')
           GROUP BY mr.materialization_id
           ORDER BY mr.materialization_id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    return {
        "materialization_id": result["materialization_id"],
        "run_kind": result["run_kind"],
        "status": result["status"],
        "started_at": result["started_at"],
        "completed_at": result["completed_at"],
        "input_count": result["input_count"],
        "data_consistent": bool(result["derivations_ok"])
        and (result["run_kind"] == "interactive" or bool(result["manage_ok"])),
    }


__all__ = [
    "BATTLELOG_MAX_AGE_MINUTES",
    "CLAN_MAX_AGE_MINUTES",
    "PROFILE_MAX_AGE_MINUTES",
    "evaluate_source_freshness",
    "add_materialization_input",
    "generation_snapshot",
    "record_admission_decision",
    "latest_materialization",
    "start_materialization",
    "update_materialization",
]
