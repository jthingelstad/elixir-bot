"""War slice: standalone build + parity validation against frozen legacy.

Self-contained (does NOT touch the shared elixir-v5*.db files). Builds a throwaway
event store + projection DB from the frozen legacy archive, runs the war
projections, and checks parity against the legacy war tables:

  war_current_state  — latest observed live war-state per clan
  war_participation  — finalized per-participant race standings

Run:  ./venv/bin/python -m event_core.war_validate

Both parity checks are scoped to what the ~2-week raw archive can reproduce; rows
whose legacy state predates the archive horizon are reported separately, never as
failures (mirrors event_core.parity's handling).
"""
from __future__ import annotations

import json
import os
import sqlite3

DEFAULT_EVENTS_DB = "/tmp/war_events.db"
DEFAULT_PROJ_DB = "/tmp/war_proj.db"
LEGACY_DB = "/Users/otto/Projects/elixir-bot/elixir.db.legacy"


def _tag_key(tag) -> str:
    t = (tag or "").strip().upper()
    return t.lstrip("#")


def _rm(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build(events_db=DEFAULT_EVENTS_DB, proj_db=DEFAULT_PROJ_DB, legacy=LEGACY_DB) -> dict:
    _rm(events_db)
    _rm(proj_db)

    # Point the event store + projections at the throwaway paths.
    os.environ["PERSISTENCE_MODULE"] = "eventsourcing.sqlite"
    os.environ["SQLITE_DBNAME"] = events_db
    os.environ["ELIXIR_V5_EVENTS_DB"] = events_db
    os.environ["ELIXIR_V5_DB"] = proj_db
    os.environ["ELIXIR_LEGACY_DB"] = legacy

    from eventsourcing.application import Application

    from event_core import db
    from event_core.domain.riverrace import RiverRace
    from event_core.projections.war import (
        WarCurrentStateProjection,
        WarParticipationProjection,
    )
    from event_core.war_backfill import backfill_currentriverrace, backfill_war_log

    class WarWorld(Application):
        snapshotting_intervals = {RiverRace: 100}

    app = WarWorld()
    bf_log = backfill_war_log(app, legacy_path=legacy, projections_path=proj_db)
    bf_cur = backfill_currentriverrace(app, legacy_path=legacy, projections_path=proj_db)

    conn = db.connect(proj_db)
    cur_proj = WarCurrentStateProjection(app, conn)
    cur_proj.reset()
    cur_applied = cur_proj.run()
    part_proj = WarParticipationProjection(app, conn)
    part_proj.reset()
    part_applied = part_proj.run()
    conn.close()

    return {
        "backfill": {"war_log": bf_log, "currentriverrace": bf_cur},
        "projection_events_applied": {
            "war_current_state": cur_applied,
            "war_participation": part_applied,
        },
    }


# --------------------------------------------------------------------------
# parity: war_current_state
# --------------------------------------------------------------------------
# Clan-summary columns present in both projection and legacy war_current_state.
WCS_COLUMNS = [
    "war_state", "clan_tag", "clan_name", "fame", "repair_points",
    "period_points", "clan_score",
]


def check_war_current_state_parity(legacy=LEGACY_DB, proj_db=DEFAULT_PROJ_DB) -> dict:
    """Compare the latest observed war-state per clan.

    Legacy war_current_state is a global slide table; its semantically meaningful
    parity row is the most-recent one per clan_tag. The projection's most-recent
    row per clan must match it on the clan-summary fields. observed_at is excluded
    from the comparison: legacy observed_at is wall-clock ingest time (_utcnow at
    upsert), while backfill uses the raw payload's fetched_at — different clocks,
    same observation. The content_hash slide key is reproduced exactly and used as
    a cross-check.
    """
    lg = sqlite3.connect(legacy)
    lg.row_factory = sqlite3.Row
    pr = sqlite3.connect(proj_db)
    pr.row_factory = sqlite3.Row
    try:
        # latest legacy row per clan_tag (max observed_at, tiebreak max war_id)
        legacy_latest: dict[str, sqlite3.Row] = {}
        for r in lg.execute(
            "SELECT * FROM war_current_state ORDER BY observed_at ASC, war_id ASC"
        ):
            legacy_latest[_tag_key(r["clan_tag"])] = r  # last wins == latest

        # archive horizon: clans that have at least one currentriverrace payload
        archive_clans = {
            _tag_key(r["entity_key"])
            for r in lg.execute(
                "SELECT DISTINCT entity_key FROM raw_api_payloads WHERE endpoint='currentriverrace'"
            )
        }

        proj_latest: dict[str, sqlite3.Row] = {}
        for r in pr.execute(
            "SELECT * FROM war_current_state_proj WHERE observed_at IS NOT NULL "
            "ORDER BY observed_at ASC"
        ):
            proj_latest[_tag_key(r["clan_tag"])] = r
    finally:
        lg.close()
        pr.close()

    matched, mismatches, outside, missing = [], [], [], []
    for ck, leg in legacy_latest.items():
        if ck not in archive_clans:
            outside.append(ck)
            continue
        prr = proj_latest.get(ck)
        if prr is None:
            missing.append(ck)
            continue
        diffs = {}
        for col in WCS_COLUMNS:
            lv = _tag_key(leg[col]) if col == "clan_tag" else leg[col]
            pv = _tag_key(prr[col]) if col == "clan_tag" else prr[col]
            if lv != pv:
                diffs[col] = {"legacy": leg[col], "projection": prr[col]}
        if diffs:
            mismatches.append({"clan": ck, "diffs": diffs})
        else:
            matched.append(ck)

    return {
        "reproducible_clans": len(matched) + len(mismatches) + len(missing),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "missing_projection": len(missing),
        "outside_archive_horizon": len(outside),
        "mismatch_detail": mismatches[:25],
    }


# --------------------------------------------------------------------------
# parity: war_participation
# --------------------------------------------------------------------------
WP_COLUMNS = ["fame", "repair_points", "boat_attacks", "decks_used", "decks_used_today"]


def check_war_participation_parity(legacy=LEGACY_DB, proj_db=DEFAULT_PROJ_DB) -> dict:
    """Compare finalized per-participant race standings.

    Identity = (season_id, section_index, player_tag). Legacy war_participation
    keys on (war_race_id, player_tag); we join war_participation -> war_races to
    recover (season_id, section_index) so the comparison is archive-independent of
    autoincrement ids. Reproducible set = races present in the projection (i.e.
    covered by an archived clan_war_log). Legacy races outside that set are
    reported as outside_archive_horizon.
    """
    lg = sqlite3.connect(legacy)
    lg.row_factory = sqlite3.Row
    pr = sqlite3.connect(proj_db)
    pr.row_factory = sqlite3.Row
    try:
        legacy_rows: dict[tuple, sqlite3.Row] = {}
        for r in lg.execute(
            "SELECT wr.season_id AS season_id, wr.section_index AS section_index, wp.* "
            "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id"
        ):
            key = (r["season_id"], r["section_index"], _tag_key(r["player_tag"]))
            legacy_rows[key] = r

        proj_rows: dict[tuple, sqlite3.Row] = {}
        proj_races = set()
        for r in pr.execute("SELECT * FROM war_participation_proj"):
            key = (r["season_id"], r["section_index"], _tag_key(r["player_tag"]))
            proj_rows[key] = r
            proj_races.add((r["season_id"], r["section_index"]))
    finally:
        lg.close()
        pr.close()

    matched, mismatches, missing_in_legacy = [], [], []
    for key, prr in proj_rows.items():
        leg = legacy_rows.get(key)
        if leg is None:
            missing_in_legacy.append(key)
            continue
        diffs = {}
        for col in WP_COLUMNS:
            if leg[col] != prr[col]:
                diffs[col] = {"legacy": leg[col], "projection": prr[col]}
        if diffs:
            mismatches.append({"key": list(key), "diffs": diffs})
        else:
            matched.append(key)

    # legacy participations whose race is not in the archived-log set
    outside = [
        list(k) for k in legacy_rows
        if (k[0], k[1]) not in proj_races
    ]
    return {
        "reproducible_participations": len(matched) + len(mismatches),
        "matched": len(matched),
        "mismatched": len(mismatches),
        "missing_in_legacy": len(missing_in_legacy),  # proj rows w/o legacy match
        "outside_archive_horizon": len(outside),
        "reproducible_races": sorted(
            {(k[0], k[1]) for k in proj_rows}
        ),
        "mismatch_detail": mismatches[:25],
        "missing_detail": [list(k) for k in missing_in_legacy[:25]],
    }


def validate(events_db=DEFAULT_EVENTS_DB, proj_db=DEFAULT_PROJ_DB, legacy=LEGACY_DB) -> dict:
    build_report = build(events_db, proj_db, legacy)
    return {
        "build": build_report,
        "parity": {
            "war_current_state": check_war_current_state_parity(legacy, proj_db),
            "war_participation": check_war_participation_parity(legacy, proj_db),
        },
    }


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2, default=str))
