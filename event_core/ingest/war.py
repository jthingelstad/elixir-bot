"""War (River Race) ingest.

Two raw sources land on the RiverRace aggregate:

- `currentriverrace` -> RiverRace.observe_current_state (war_current_state)
- `clan_war_log`     -> RiverRace.observe_log_standing  (war_participation + war_races)

Field extraction and the dedup content hash mirror storage.war_ingest exactly:

* `_war_current_content_hash` reproduces `upsert_war_current_state`'s
  `_hash_payload([...])` slide key, so the same polls dedup the same way.
* season-id inference for live state reproduces
  `infer_current_season_id_from_live_state`, reading prior logged races out of
  the event store (the RiverRace aggregates already ingested from the war log)
  rather than the legacy war_races table — keeping the rebuild self-contained.

These functions take an `Application` (ObservedWorld) instance and perform
get-or-create + save themselves; they do not require the app to expose war
methods (see WIRING NOTE for the optional convenience methods to add later).
"""
from __future__ import annotations

import hashlib
import json

from eventsourcing.application import AggregateNotFoundError

from event_core.domain.riverrace import (
    PARTICIPANT_FIELDS,
    RACE_SUMMARY_FIELDS,
    RiverRace,
    canon_tag,
    riverrace_id,
    tag_key,
)


# --------------------------------------------------------------------------
# content hashing (mirror legacy db._hash_payload over the same field list)
# --------------------------------------------------------------------------
def _hash(parts) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def war_current_content_hash(war_data: dict) -> str:
    """Reproduce upsert_war_current_state's dedup slide key exactly."""
    clan = (war_data or {}).get("clan", {}) or {}
    return _hash([
        war_data.get("state"),
        canon_tag(clan.get("tag")),
        clan.get("fame"),
        clan.get("repairPoints"),
        clan.get("periodPoints"),
        clan.get("clanScore"),
        war_data.get("sectionIndex"),
        war_data.get("periodIndex"),
        war_data.get("periodType"),
    ])


def build_current_state_observation(war_data: dict) -> dict:
    """Extract war_current_state columns from a currentriverrace payload."""
    clan = (war_data or {}).get("clan", {}) or {}
    return {
        "war_state": war_data.get("state"),
        "clan_tag": canon_tag(clan.get("tag")),
        "clan_name": clan.get("name"),
        "fame": clan.get("fame"),
        "repair_points": clan.get("repairPoints"),
        "period_points": clan.get("periodPoints"),
        "clan_score": clan.get("clanScore"),
        "section_index": war_data.get("sectionIndex"),
        "period_index": war_data.get("periodIndex"),
        "period_type": war_data.get("periodType"),
    }


# --------------------------------------------------------------------------
# season inference (mirror _war_shared.infer_current_season_id_from_live_state)
# --------------------------------------------------------------------------
def _latest_logged_race_for_clan(app, clan_tag: str):
    """Return (season_id, section_index) of the most recently logged race for
    this clan, scanning RiverRace aggregates already ingested from the war log.

    Equivalent to legacy get_latest_logged_race (ORDER BY season DESC, section
    DESC), but sourced from the event store so the rebuild is self-contained.
    """
    best = None  # (season_id, section_index)
    for notif in app.recorder.select_notifications(start=1, limit=1_000_000):
        ev = app.mapper.to_domain_event(notif)
        if type(ev).__name__ != "LogStandingObserved":
            continue
        try:
            agg = app.repository.get(ev.originator_id)
        except AggregateNotFoundError:
            continue
        if tag_key(agg.clan_tag) != tag_key(clan_tag):
            continue
        key = (agg.season_id, agg.section_index)
        if best is None or key > best:
            best = key
    return best


def infer_live_season_id(war_data: dict, latest_logged) -> int | None:
    """Reproduce legacy live-season inference."""
    live_season_id = (war_data or {}).get("seasonId")
    if live_season_id is not None:
        return live_season_id
    if not latest_logged:
        return None
    logged_season, logged_section = latest_logged
    live_section = (war_data or {}).get("sectionIndex")
    if (
        live_section is not None
        and logged_section is not None
        and live_section < logged_section
    ):
        return logged_season + 1
    return logged_season


# --------------------------------------------------------------------------
# ingest entrypoints
# --------------------------------------------------------------------------
def _get_or_create(app, clan_tag, season_id, section_index) -> RiverRace:
    try:
        return app.repository.get(riverrace_id(clan_tag, season_id, section_index))
    except AggregateNotFoundError:
        return RiverRace(
            clan_tag=canon_tag(clan_tag),
            season_id=season_id,
            section_index=section_index,
        )


def ingest_currentriverrace_payload(app, entity_key: str, payload: dict, observed_at: str) -> bool:
    """Ingest one currentriverrace payload. Returns True if state changed.

    `entity_key` is the polled clan tag; the live payload's `clan` object is the
    same clan, so we key the aggregate by that clan + inferred season + section.
    """
    war_data = payload or {}
    clan = war_data.get("clan") or {}
    clan_tag = clan.get("tag") or entity_key
    latest_logged = _latest_logged_race_for_clan(app, clan_tag)
    season_id = infer_live_season_id(war_data, latest_logged)
    section_index = war_data.get("sectionIndex")

    obs = build_current_state_observation(war_data)
    content_hash = war_current_content_hash(war_data)

    rr = _get_or_create(app, clan_tag, season_id, section_index)
    changed = rr.observe_current_state(obs, observed_at, content_hash)
    if changed:
        app.save(rr)
    return changed


def _build_log_standing(entity_key: str, entry: dict):
    """Extract (clan_tag, season_id, section_index, race_summary, participants)
    for our clan from one riverracelog item, mirroring store_war_log.
    """
    season_id = entry.get("seasonId")
    section_index = entry.get("sectionIndex")
    standings = entry.get("standings") or []
    our = None
    for standing in standings:
        clan = standing.get("clan", {}) or {}
        if tag_key(clan.get("tag")) == tag_key(entity_key):
            our = standing
            break
    if our is None:
        return None
    clan = our.get("clan", {}) or {}
    race_summary = {
        "created_date": entry.get("createdDate"),
        "our_rank": our.get("rank"),
        "trophy_change": our.get("trophyChange"),
        "our_fame": clan.get("fame"),
        "our_clan_score": clan.get("clanScore"),
        "total_clans": len(standings),
        "finish_time": clan.get("finishTime"),
    }
    participants = []
    for p in clan.get("participants", []) or []:
        ptag = canon_tag(p.get("tag"))
        if not ptag:
            continue
        participants.append({
            "player_tag": ptag,
            "player_name": p.get("name"),
            "fame": p.get("fame", 0),
            "repair_points": p.get("repairPoints", 0),
            "boat_attacks": p.get("boatAttacks", 0),
            "decks_used": p.get("decksUsed", 0),
            "decks_used_today": p.get("decksUsedToday", 0),
        })
    return canon_tag(clan.get("tag")) or canon_tag(entity_key), season_id, section_index, race_summary, participants


def log_standing_content_hash(race_summary: dict, participants: list[dict]) -> str:
    parts = [race_summary[c] for c in RACE_SUMMARY_FIELDS]
    parts.append([[p[f] for f in PARTICIPANT_FIELDS] for p in
                  sorted(participants, key=lambda x: tag_key(x["player_tag"]))])
    return _hash(parts)


def ingest_clan_war_log_payload(app, entity_key: str, payload: dict, observed_at: str) -> int:
    """Ingest one riverracelog payload. Returns count of races whose log changed.

    Iterates items oldest->newest so season inference for any later live ingest
    sees races accumulate in chronological order.
    """
    items = list((payload or {}).get("items", []) or [])
    # The API returns newest-first; process oldest-first for deterministic
    # accumulation (mirrors how the log grows over time).
    items.reverse()
    changed = 0
    for entry in items:
        built = _build_log_standing(entity_key, entry)
        if built is None:
            continue
        clan_tag, season_id, section_index, race_summary, participants = built
        content_hash = log_standing_content_hash(race_summary, participants)
        rr = _get_or_create(app, clan_tag, season_id, section_index)
        if rr.observe_log_standing(race_summary, participants, observed_at, content_hash):
            app.save(rr)
            changed += 1
    return changed
