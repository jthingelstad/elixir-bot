"""engine.emitters.game — the game-level stream's emission side (events + badges).

New cards emit `card_added` straight from the daily catalog sync
(storage.card_catalog). This module handles the other two sources, both riding
the API sentinel's observation store: new live events (`/events`) and genuinely-
new event badges — a non-`Mastery` badge key never seen before, attributed to
the first member wearing it (the Chaos_S2 → Aaqib case).

It reads `api_sentinel_observations` past an `emit:game` cursor, so only
observations recorded after go-live surface. On first run the cursor is seeded
to the tip: everything already in the store is history (the backfill records it
as `backfilled=1`), so the live stream never floods with the bootstrap batch.
"""

from __future__ import annotations

import json

from engine.db import cursor_get, cursor_set
from storage import game_events as ge


def _sample(obs) -> dict:
    try:
        return json.loads(obs["sample_json"] or "{}")
    except TypeError, ValueError:
        return {}


def _event_started(conn, obs, now: str) -> int:
    sample = _sample(obs)
    tag = obs["name"]
    payload = {
        "event_tag": tag,
        "title": sample.get("title") or tag,
        "description": sample.get("description"),
    }
    return ge.insert_game_event(
        conn,
        dedup_key=f"event_started:{tag}",
        event_type="event_started",
        change_key=f"event:{tag}",
        observed_at=obs["first_seen_at"] or now,
        payload=payload,
    )


def _event_badge(conn, obs, now: str) -> int:
    name = obs["name"] or ""
    if not name or name.startswith("Mastery"):
        # Mastery badges are per-card player achievements (already posted to
        # #player-highlights as badge_earned), not game news.
        return 0
    from engine.normalize import humanize_badge
    from storage._formatting import preferred_display_name

    sample = _sample(obs)
    badge = sample.get("badge") or {}
    image_url = (badge.get("iconUrls") or {}).get("large")
    # Attribute to the FIRST member seen wearing it — first_entity_key is
    # preserved across touches; entity_key drifts to the latest observer.
    keys = obs.keys()
    entity_key = (
        obs["first_entity_key"]
        if "first_entity_key" in keys and obs["first_entity_key"]
        else obs["entity_key"]
    )
    subject_tag = f"#{entity_key.lstrip('#')}" if entity_key else None
    # Attribution is best-effort: preferred_display_name falls back to the raw
    # tag when the member has left / isn't resolvable — treat that as unattributed.
    member_name = preferred_display_name(conn, subject_tag) if subject_tag else None
    if member_name in (None, "", subject_tag):
        member_name = None
    payload = {
        "badge_name": name,
        "badge_label": humanize_badge(name),
        "member_name": member_name,
        "member_tag": subject_tag,
        "image_url": image_url,
    }
    return ge.insert_game_event(
        conn,
        dedup_key=f"event_badge_earned:{name}",
        event_type="event_badge_earned",
        change_key=f"badge:{name}",
        observed_at=obs["first_seen_at"] or now,
        payload=payload,
        subject_tag=subject_tag,
    )


def emit_game_from_sentinel(conn, now: str) -> int:
    """New `/events` entries and event badges from the sentinel store →
    game_events. Advances the `emit:game` cursor. Returns rows written."""
    ge.ensure_schema(conn)
    pos = cursor_get(conn, "emit:game")
    max_id = conn.execute(
        "SELECT COALESCE(MAX(observation_id), 0) FROM api_sentinel_observations"
    ).fetchone()[0]
    if pos == 0 and max_id > 0:
        # Go-live: everything already observed is history (recorded by the
        # backfill). Start the live cursor at the tip and emit nothing now.
        cursor_set(conn, "emit:game", max_id)
        return 0
    rows = conn.execute(
        "SELECT * FROM api_sentinel_observations WHERE observation_id > ? ORDER BY observation_id",
        (pos,),
    ).fetchall()
    written = 0
    for obs in rows:
        st = obs["sentinel_type"]
        if st == "event":
            written += _event_started(conn, obs, now)
        elif st == "badge_name":
            written += _event_badge(conn, obs, now)
    if rows:
        cursor_set(conn, "emit:game", rows[-1]["observation_id"])
    return written
