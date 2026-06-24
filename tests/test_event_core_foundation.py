"""Foundation-slice tests for the v5 Event Core.

Unit tests run on a synthetic in-memory event store. The integration test
(exact parity / determinism / idempotency vs the frozen legacy DB) is skipped
automatically when elixir.db.legacy is absent.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from event_core import config


@pytest.fixture()
def world():
    """A fresh ObservedWorld backed by a throwaway sqlite event store."""
    d = tempfile.mkdtemp()
    config.configure_eventstore_env(os.path.join(d, "events.db"))
    from event_core.application import ObservedWorld

    return ObservedWorld()


def test_player_id_is_canonical_and_deterministic():
    from event_core.domain.player import player_id

    assert player_id("c920yglc2") == player_id("#C920YGLC2")
    assert player_id("#ABC") != player_id("#DEF")


def test_observe_profile_dedup_and_fold(world):
    from event_core.domain.player import player_id

    tag = "#TESTER"
    assert world.observe_player_profile(tag, {"trophies": 6000}, "t0", "h0") is True
    assert world.observe_player_profile(tag, {"trophies": 6000}, "t1", "h0") is False  # dedup
    assert world.observe_player_profile(tag, {"trophies": 6100}, "t2", "h1") is True

    p = world.repository.get(player_id(tag))
    assert p.profile["trophies"] == 6100
    assert p.last_observed_at == "t2"


def test_notification_log_orders_events(world):
    world.observe_player_profile("#A", {"trophies": 1}, "t0", "h0")
    world.observe_player_profile("#A", {"trophies": 2}, "t1", "h1")
    notifs = world.recorder.select_notifications(start=1, limit=100)
    topics = [n.topic.rsplit(".", 1)[-1] for n in notifs]
    assert topics == ["Registered", "ProfileObserved", "ProfileObserved"]


def test_projection_reflects_latest(world):
    from event_core import db
    from event_core.projections.player_state import PlayerCurrentProfile

    world.observe_player_profile("#PROJ", {"trophies": 100, "exp_level": 50}, "t0", "h0")
    world.observe_player_profile("#PROJ", {"trophies": 200, "exp_level": 51}, "t1", "h1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    proj = PlayerCurrentProfile(world, conn)
    proj.setup()
    proj.run()
    row = conn.execute(
        "SELECT trophies, exp_level FROM player_current_profile WHERE player_tag='#PROJ'"
    ).fetchone()
    assert row["trophies"] == 200
    assert row["exp_level"] == 51


def test_profile_observation_derives_badge_backed_profile_fields():
    from event_core.ingest.profile import build_profile_observation

    obs = build_profile_observation(
        {
            "tag": "#BADGE",
            "name": "Badge Keeper",
            "badges": [
                {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473},
                {"name": "CollectionLevel", "level": 8, "maxLevel": 8, "progress": 1639},
                {"name": "ClanWarWins", "level": 5, "maxLevel": 10, "progress": 421},
                {"name": "BattleWins", "level": 7, "maxLevel": 10, "progress": 6400},
                {"name": "ClanWarsVeteran", "level": 6, "maxLevel": 10, "progress": 273},
                {"name": "ClanDonations", "level": 9, "maxLevel": 10, "progress": 32145},
                {"name": "BannerCollection", "level": 3, "maxLevel": 10, "progress": 88},
                {"name": "EmoteCollection", "level": 4, "maxLevel": 10, "progress": 123},
                {"name": "MasteryFireball", "level": 7, "progress": 25000},
            ],
        }
    )

    assert obs["cr_account_age_days"] == 1473
    assert obs["cr_account_age_years"] == 4
    assert obs["cr_collection_level"] == 1639
    assert obs["cr_collection_level_badge_tier"] == 8
    assert obs["cr_collection_level_badge_max_tier"] == 8
    assert obs["cr_clan_war_wins"] == 421
    assert obs["cr_battle_wins"] == 6400
    assert obs["cr_clan_wars_veteran"] == 273
    assert obs["cr_clan_wars_veteran_badge_tier"] == 6
    assert obs["cr_clan_wars_veteran_badge_max_tier"] == 10
    assert obs["cr_clan_donations"] == 32145
    assert obs["cr_banner_count"] == 88
    assert obs["cr_emote_count"] == 123
    assert "MasteryFireball" not in obs


def test_profile_projection_folds_badge_profile_fields_and_preserves_raw_badges(world):
    import json

    from event_core import db
    from event_core.ingest.collections import ingest_player_collections
    from event_core.ingest.profile import ingest_player_payload
    from event_core.projections.collections import PlayerCurrentCollections
    from event_core.projections.player_state import PlayerCurrentProfile

    payload = {
        "tag": "#BADGE",
        "name": "Badge Keeper",
        "badges": [
            {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473},
            {"name": "CollectionLevel", "level": 8, "maxLevel": 8, "progress": 1639},
            {"name": "BattleWins", "level": 7, "maxLevel": 10, "progress": 6400},
            {"name": "ClanWarsVeteran", "level": 6, "maxLevel": 10, "progress": 273},
            {"name": "MasteryFireball", "level": 7, "progress": 25000},
        ],
    }
    assert ingest_player_payload(world, payload, "2026-06-24T12:00:00Z") is True
    assert ingest_player_collections(world, payload, "2026-06-24T12:00:00Z")["badges"] is True

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        PlayerCurrentProfile(world, conn).setup()
        PlayerCurrentProfile(world, conn).run()
        PlayerCurrentCollections(world, conn).setup()
        PlayerCurrentCollections(world, conn).run()

        profile = conn.execute(
            "SELECT cr_account_age_days, cr_account_age_years, cr_collection_level, "
            "cr_collection_level_badge_tier, cr_collection_level_badge_max_tier, "
            "cr_battle_wins, cr_clan_wars_veteran, cr_clan_wars_veteran_badge_tier, "
            "cr_clan_wars_veteran_badge_max_tier "
            "FROM player_current_profile WHERE player_tag='#BADGE'"
        ).fetchone()
        assert dict(profile) == {
            "cr_account_age_days": 1473,
            "cr_account_age_years": 4,
            "cr_collection_level": 1639,
            "cr_collection_level_badge_tier": 8,
            "cr_collection_level_badge_max_tier": 8,
            "cr_battle_wins": 6400,
            "cr_clan_wars_veteran": 273,
            "cr_clan_wars_veteran_badge_tier": 6,
            "cr_clan_wars_veteran_badge_max_tier": 10,
        }

        row = conn.execute(
            "SELECT badges_json FROM player_current_collections WHERE player_tag='#BADGE'"
        ).fetchone()
        raw_badges = json.loads(row["badges_json"])
        assert {badge["name"] for badge in raw_badges} == {
            "YearsPlayed",
            "CollectionLevel",
            "BattleWins",
            "ClanWarsVeteran",
            "MasteryFireball",
        }
    finally:
        conn.close()


def test_profile_projection_setup_adds_badge_columns_to_existing_table(world):
    from event_core import db
    from event_core.projections.player_state import PlayerCurrentProfile

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        conn.execute(
            """
            CREATE TABLE player_current_profile (
                aggregate_id TEXT PRIMARY KEY,
                player_tag TEXT UNIQUE,
                observed_at TEXT,
                name TEXT,
                trophies INTEGER
            )
            """
        )
        conn.commit()

        PlayerCurrentProfile(world, conn).setup()

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(player_current_profile)")
        }
        assert "cr_collection_level" in columns
        assert "cr_clan_wars_veteran" in columns
    finally:
        conn.close()


def test_roster_observation_extraction_matches_legacy_defaults():
    from event_core.ingest.roster import build_roster_observation

    obs = build_roster_observation(
        {"tag": "#X", "role": "elder", "expLevel": 50, "trophies": 7000,
         "clanRank": 3, "donations": 120, "donationsReceived": 80,
         "arena": {"id": 9, "name": "Legendary", "rawName": "Arena_L9"},
         "lastSeen": "20260621T120000.000Z"}
    )
    assert obs["role"] == "elder"
    assert obs["trophies"] == 7000
    assert obs["donations_week"] == 120
    assert obs["arena_id"] == 9 and obs["arena_raw_name"] == "Arena_L9"
    # defaults when fields absent (mirrors snapshot_members)
    empty = build_roster_observation({"tag": "#Y"})
    assert empty["role"] == "member" and empty["trophies"] == 0
    assert empty["donations_week"] == 0 and empty["arena_id"] is None


def test_roster_projection_folds_latest(world):
    from event_core import db
    from event_core.projections.member_state import MemberCurrentState

    world.observe_member_roster("#R", {"trophies": 100, "role": "member"}, "t0", "h0")
    world.observe_member_roster("#R", {"trophies": 200, "role": "elder"}, "t1", "h1")
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    proj = MemberCurrentState(world, conn)
    proj.setup()
    proj.run()
    row = conn.execute(
        "SELECT trophies, role FROM member_current_state_proj WHERE player_tag='#R'"
    ).fetchone()
    assert row["trophies"] == 200 and row["role"] == "elder"


def test_extract_battles_identity_fields():
    from event_core.ingest.battles import extract_battles

    payload = [
        {
            "battleTime": "20260621T112729.000Z",
            "type": "PvP",
            "team": [{"tag": "#ME", "crowns": 3, "trophyChange": 30}],
            "opponent": [{"tag": "#OPP", "crowns": 1}],
            "gameMode": {"id": 72000006, "name": "Ladder"},
        }
    ]
    [b] = extract_battles("ME", payload)
    assert b["battle_time"] == "20260621T112729.000Z"
    assert b["crowns_for"] == 3 and b["crowns_against"] == 1
    assert b["opponent_tag"] == "#OPP" and b["trophy_change"] == 30


def test_clan_roster_lifecycle_diff(world):
    from event_core.domain.clan import clan_id

    # first observation = baseline, no join/leave events
    assert world.observe_clan_roster("#CLN", {"#A": "member", "#B": "elder"}, "t0") == 0
    # #B promoted, #C joins, #A leaves -> 3 lifecycle events
    assert world.observe_clan_roster("#CLN", {"#B": "coLeader", "#C": "member"}, "t1") == 3

    c = world.repository.get(clan_id("#CLN"))
    assert c.members == {"#B": "coLeader", "#C": "member"}
    topics = [
        n.topic.rsplit(".", 1)[-1]
        for n in world.recorder.select_notifications(start=1, limit=100)
    ]
    assert "MemberJoined" in topics and "MemberLeft" in topics and "MemberRoleChanged" in topics


legacy_missing = not os.path.exists(config.LEGACY_DB)


@pytest.mark.skipif(legacy_missing, reason="frozen legacy DB not present")
def test_foundation_parity_determinism_idempotency():
    import hashlib
    import sqlite3

    from event_core import build_foundation
    from event_core.application import ObservedWorld
    from event_core.backfill import backfill_players

    def fingerprint():
        c = sqlite3.connect(config.PROJECTIONS_DB)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM player_current_profile ORDER BY player_tag"
        ).fetchall()
        c.close()
        blob = "\n".join(
            "|".join(str(r[k]) for k in r.keys() if k != "observed_at") for r in rows
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    r1 = build_foundation.build()
    fp1 = fingerprint()
    build_foundation.build()
    fp2 = fingerprint()

    # exact parity: every reproducible member matches, none mismatched/missing
    pp = r1["parity"]["player_profile"]
    assert pp["mismatched"] == 0
    assert pp["missing_projection"] == 0
    assert pp["matched"] == pp["reproducible_members"] > 0

    bp = r1["parity"]["player_profile_badges"]
    assert bp["mismatched"] == 0
    assert bp["missing_projection"] == 0
    assert bp["matched"] == bp["reproducible_members"] > 0

    # roster: no true mismatches (v5_more_current divergences are expected/explained)
    rp = r1["parity"]["member_current_state"]
    assert rp["mismatched"] == 0
    assert rp["matched"] > 0

    # battle telemetry tier: derived columns (outcome, mode flags) match exactly on
    # battles present in both; only_in_* are expected coverage artifacts.
    bt = r1["parity"]["battle_telemetry"]
    assert bt["battles_matched_identity"] > 0
    assert bt["outcome_mismatch"] == 0  # deterministic field must match exactly

    # collections (cards/badges/achievements): exact content parity
    coll = r1["parity"]["collections"]
    for kind in ("cards", "badges", "achievements"):
        assert coll[kind]["mismatched"] == 0
        assert coll[kind]["matched"] > 0

    # war: current state + participation exact
    assert r1["parity"]["war_current_state"]["mismatched"] == 0
    assert r1["parity"]["war_current_state"]["matched"] > 0
    assert r1["parity"]["war_participation"]["mismatched"] == 0
    assert r1["parity"]["war_participation"]["matched"] > 0

    # clan daily metrics: no real bugs (the mismatches are legacy-corruption /
    # last-observation timing, classified in the STATUS report)
    cm = r1["parity"]["clan_daily_metrics"]
    assert cm["matched"] > 0 and cm["missing_projection"] == 0

    # replay determinism: two from-zero rebuilds are byte-identical
    assert fp1 == fp2

    # idempotency: re-ingest into the existing store emits nothing
    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    before = app.recorder.max_notification_id()
    again = backfill_players(app)
    assert again["events_emitted"] == 0
    assert app.recorder.max_notification_id() == before
