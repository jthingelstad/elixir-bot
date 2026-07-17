"""The emitter contract — architecture.md §8, runtime.md §2 step 3.

emit(entity, aspect, previous_state | None, observed_state, observed_at) -> [Event]

Deterministic, no side effects beyond the event inserts + baseline roll, all
inside the caller's transaction. First sight emits nothing; unchanged payloads
just roll observed_at; dedup keys make re-processing safe.
"""

from __future__ import annotations

import json

from engine.baselines import baseline_payload, get_baseline, set_baseline
from engine.db import payload_hash, utcnow


def insert_stream_event(
    conn,
    table: str,
    *,
    dedup_key: str,
    event_type: str,
    subject_cols: dict,
    observed_at: str,
    window_start: str | None,
    payload: dict,
    evidence: dict | None = None,
    timing: str = "estimated",
    scope: str = "public",
) -> int:
    """INSERT OR IGNORE one event row into player_events / clan_events /
    war_events (shared §8 envelope). Returns rows inserted (0 on dedup)."""
    from engine.event_contracts import validate_event

    _, observed_at = validate_event(
        table=table,
        event_type=event_type,
        payload=payload,
        observed_at=observed_at,
    )
    cols = {
        "dedup_key": dedup_key,
        "event_type": event_type,
        **subject_cols,
        "observed_at": observed_at,
        "timing": timing,
        "window_start": window_start,
        "evidence_json": json.dumps(evidence, separators=(",", ":")) if evidence else None,
        "payload_json": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        "scope": scope,
        "created_at": utcnow(),
    }
    names = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})", list(cols.values())
    )
    return cur.rowcount


def emit(
    conn,
    entity_kind: str,
    entity_tag: str,
    aspect: str,
    new_payload: dict,
    observed_at: str,
) -> int:
    """Run one (entity, aspect) observation through its emitter.

    First sight (§8): record the baseline, emit nothing. Hash-unchanged: roll
    observed_at only. Changed: diff via the registered emitter, then roll the
    baseline — same transaction (the caller owns commit/rollback).
    """
    from engine.emitters import clan, player, war

    dispatch = {
        ("player", "profile"): player.emit_profile,
        ("player", "cards"): player.emit_cards,
        ("player", "ranked"): player.emit_ranked,
        ("clan", "roster"): clan.emit_roster,
        ("clan", "clan_entity"): clan.emit_clan_entity,
        ("clan", "award_races"): war.emit_award_races,
        ("riverrace", "race"): war.emit_race,
    }
    fn = dispatch.get((entity_kind, aspect))
    if fn is None:
        raise ValueError(f"no emitter registered for ({entity_kind}, {aspect})")

    row = get_baseline(conn, entity_kind, entity_tag, aspect)
    if row is None:
        set_baseline(conn, entity_kind, entity_tag, aspect, new_payload, observed_at)
        return 0  # first-sight emits nothing (§8)
    if row["payload_hash"] == payload_hash(new_payload):
        set_baseline(conn, entity_kind, entity_tag, aspect, new_payload, observed_at)
        return 0
    old_payload = baseline_payload(row)
    window_start = row["observed_at"]
    emitted = fn(conn, entity_tag, old_payload, new_payload, observed_at, window_start)
    to_store = new_payload
    if (entity_kind, aspect) == ("riverrace", "race"):
        # #166: don't let the API's post-battle reset snapshot overwrite the
        # peak race baseline, or the season/week rollover finalizes from zeros.
        to_store = war.merge_baseline(old_payload, new_payload)
    set_baseline(conn, entity_kind, entity_tag, aspect, to_store, observed_at)
    return emitted
