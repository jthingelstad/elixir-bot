"""Mind-layer tests: granular events, Detection aggregate, Followers."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from event_core import config


@pytest.fixture()
def world():
    d = tempfile.mkdtemp()
    config.configure_eventstore_env(os.path.join(d, "events.db"))
    from event_core.application import ObservedWorld

    return ObservedWorld()


def test_milestones_helper():
    from event_core.mind.detectors import _milestones

    assert _milestones(9, 12, 5) == [10]
    assert _milestones(8, 23, 5) == [10, 15, 20]
    assert _milestones(10, 10, 5) == []
    assert _milestones(None, 12, 5) == []
    assert _milestones(0, 500, 100) == []  # no baseline -> no burst of milestones


def test_member_left_and_promotion_detectors(world):
    from event_core import db
    from event_core.mind.detectors import MemberLeftDetector, MemberRoleChangeDetector

    # baseline observation -> no lifecycle events
    world.observe_clan_roster("#CLN", {"#A": "member", "#B": "elder", "#C": "coLeader"}, "t0")
    # #A promoted (member->elder), #C demoted (coLeader->member), #B left, #D joined
    world.observe_clan_roster("#CLN", {"#A": "elder", "#C": "member", "#D": "member"}, "t1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        assert MemberLeftDetector(world, conn).run() == 1  # #B departed
        # #A promotion posts; #C demotion is intentionally NOT posted
        assert MemberRoleChangeDetector(world, conn).run() == 1
    finally:
        conn.close()


def _detection_rows(world, conn):
    from event_core.projections.detections import DetectionsProjection
    dp = DetectionsProjection(world, conn)
    dp.setup()
    dp.run()
    return {r["detection_type"]: json.loads(r["payload_json"] or "{}")
            for r in conn.execute("SELECT detection_type, payload_json FROM detections")}


def test_path_of_legend_detector(world):
    from event_core import db
    from event_core.mind.detectors import PathOfLegendDetector

    # baseline: league 8, no global rank
    world.observe_player_profile("#POL", {"name": "x", "pol_league_number": 8, "pol_trophies": 1500, "pol_rank": None}, "t0", "h0")
    # promote to Ultimate Champion (league 10) and attain global rank 500
    world.observe_player_profile("#POL", {"name": "x", "pol_league_number": 10, "pol_trophies": 2000, "pol_rank": 500}, "t1", "h1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        PathOfLegendDetector(world, conn).run()
        types = _detection_rows(world, conn)
        assert "path_of_legend_promotion" in types
        assert "ultimate_champion_reached" in types
        assert "path_of_legend_global_rank_attained" in types
        assert types["path_of_legend_promotion"]["to_league"] == 10
        assert types["path_of_legend_global_rank_attained"]["to_rank"] == 500
    finally:
        conn.close()


def _insert_ranked_telemetry(conn, tag: str, when: str, outcome: str, opponent: str) -> None:
    crowns_for = 2 if outcome == "W" else 1
    crowns_against = 1 if outcome == "W" else 2
    conn.execute(
        "INSERT OR IGNORE INTO battle_telemetry("
        "player_tag,battle_time,battle_type,opponent_tag,crowns_for,crowns_against,"
        "mode_group,outcome,is_competitive,observed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            tag,
            when,
            "pathOfLegend",
            opponent,
            crowns_for,
            crowns_against,
            "ranked",
            outcome,
            1,
            when,
        ),
    )


def test_ranked_activity_pulse_detector_emits_strong_named_path_of_legend_run(world):
    from datetime import datetime
    from unittest.mock import patch

    from event_core import db
    from event_core.ingest.battles import BATTLE_TELEMETRY_DDL
    from event_core.mind import detectors
    from event_core.mind.detectors import RankedActivityPulseDetector

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        conn.execute(BATTLE_TELEMETRY_DDL)
        conn.execute("CREATE TABLE members (player_tag TEXT PRIMARY KEY, current_name TEXT)")
        conn.execute("INSERT INTO members VALUES ('#POL', 'Atternam')")
        conn.execute("INSERT INTO members VALUES ('#EVEN', 'Busy Climber')")
        for i, outcome in enumerate(["W"] * 11 + ["L"] * 3):
            _insert_ranked_telemetry(
                conn,
                "#POL",
                f"20260624T{i:02d}0000.000Z",
                outcome,
                f"#OP{i}",
            )
        for i, outcome in enumerate(["W"] * 7 + ["L"] * 7):
            _insert_ranked_telemetry(
                conn,
                "#EVEN",
                f"20260623T{i:02d}0000.000Z",
                outcome,
                f"#EV{i}",
            )
        conn.commit()

        with patch.object(detectors, "_utc_now", return_value=datetime(2026, 6, 25, 12, 0, 0)):
            assert RankedActivityPulseDetector(world, conn).run() == 1

        rows = _detection_rows(world, conn)
        payload = rows["ranked_activity_pulse"]
        assert payload["player_name"] == "Atternam"
        assert payload["mode_label"] == "Ranked / Path of Legends"
        assert payload["battle_count"] == 14
        assert payload["wins"] == 11
        assert payload["losses"] == 3
        assert payload["win_rate"] == 0.786
        assert "Ranked1v1_NewArena2" not in json.dumps(payload)
    finally:
        conn.close()


def test_ranked_activity_pulse_detector_suppresses_raw_ranked_volume(world):
    from datetime import datetime
    from unittest.mock import patch

    from event_core import db
    from event_core.ingest.battles import BATTLE_TELEMETRY_DDL
    from event_core.mind import detectors
    from event_core.mind.detectors import RankedActivityPulseDetector
    from event_core.projections.detections import DetectionsProjection

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        conn.execute(BATTLE_TELEMETRY_DDL)
        conn.execute("CREATE TABLE members (player_tag TEXT PRIMARY KEY, current_name TEXT)")
        conn.execute("INSERT INTO members VALUES ('#GRIND', 'Raw Volume')")
        for i, outcome in enumerate(["W"] * 8 + ["L"] * 8):
            _insert_ranked_telemetry(
                conn,
                "#GRIND",
                f"20260624T{i:02d}0000.000Z",
                outcome,
                f"#RV{i}",
            )
        conn.commit()

        with patch.object(detectors, "_utc_now", return_value=datetime(2026, 6, 25, 12, 0, 0)):
            assert RankedActivityPulseDetector(world, conn).run() == 0

        dp = DetectionsProjection(world, conn)
        dp.setup()
        dp.run()
        rows = conn.execute(
            "SELECT detection_type FROM detections WHERE detection_type = 'ranked_activity_pulse'"
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


def test_member_left_enriches_and_suppresses_kicks(world):
    from event_core import db
    from event_core.mind.detectors import MemberLeftDetector

    # voluntary leave (#V) + kicked leave (#K)
    world.observe_clan_roster("#CLN2", {"#V": "member", "#K": "member"}, "t0")
    world.observe_clan_roster("#CLN2", {}, "2026-06-21T12:00:00Z")  # both gone

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        # seed the operational tables the detector reads (consolidated DB in prod)
        conn.execute("CREATE TABLE members (member_id INTEGER PRIMARY KEY, player_tag TEXT, current_name TEXT, status TEXT)")
        conn.execute("CREATE TABLE member_current_state (member_id INTEGER, role TEXT, trophies INT, best_trophies INT, clan_rank INT, last_seen_api TEXT)")
        conn.execute("CREATE TABLE leader_action_recommendations (action_type TEXT, target_player_tag TEXT, status TEXT, is_test INT, decided_at TEXT, proposed_at TEXT)")
        conn.executemany("INSERT INTO members(member_id, player_tag, current_name, status) VALUES (?,?,?,?)",
                         [(1, "#V", "Vera", "active"), (2, "#K", "Kade", "active")])
        conn.execute("INSERT INTO member_current_state(member_id, role, trophies) VALUES (1, 'member', 6100)")
        # #K was kicked (accepted leader-action) just before leaving
        conn.execute("INSERT INTO leader_action_recommendations(action_type, target_player_tag, status, is_test, decided_at) "
                     "VALUES ('kick_recommendation', '#K', 'done', 0, '2026-06-20T10:00:00')")
        conn.commit()

        assert MemberLeftDetector(world, conn).run() == 1  # only #V (kick #K suppressed)
        rows = _detection_rows(world, conn)
        assert "member_left" in rows
        assert rows["member_left"]["name"] == "Vera"  # enriched
        assert rows["member_left"]["trophies"] == 6100
    finally:
        conn.close()


def test_cake_day_and_weekly_donation_detectors(world):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from event_core import db
    from event_core.mind.detectors import CakeDayDetector, WeeklyDonationLeaderDetector

    today = datetime.now(ZoneInfo("America/Chicago")).date()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday if days_since_sunday else 7)

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        conn.execute("CREATE TABLE members (member_id INTEGER PRIMARY KEY, player_tag TEXT, current_name TEXT, status TEXT)")
        conn.execute("CREATE TABLE member_metadata (member_id INTEGER, birth_month INT, birth_day INT, joined_at TEXT)")
        conn.execute("CREATE TABLE member_daily_metrics (member_id INTEGER, metric_date TEXT, donations_week INT)")
        conn.execute("CREATE TABLE clan_daily_metrics (metric_date TEXT, clan_name TEXT)")
        conn.execute("INSERT INTO members VALUES (1, '#BD', 'Birthday Person', 'active')")
        # birthday is today
        conn.execute("INSERT INTO member_metadata(member_id, birth_month, birth_day, joined_at) VALUES (1, ?, ?, NULL)",
                     (today.month, today.day))
        # top donor for the just-completed week
        conn.execute("INSERT INTO member_daily_metrics(member_id, metric_date, donations_week) VALUES (1, ?, 768)",
                     (last_sunday.isoformat(),))
        conn.commit()

        CakeDayDetector(world, conn).run()
        WeeklyDonationLeaderDetector(world, conn).run()
        rows = _detection_rows(world, conn)
        assert "member_birthday" in rows and rows["member_birthday"]["name"] == "Birthday Person"
        assert "weekly_donation_leader" in rows
        assert rows["weekly_donation_leader"]["leaders"][0]["donations"] == 768
    finally:
        conn.close()


def test_war_update_detector_daily_evening_and_complete(world):
    """Scan-style war detector: one evening standing per battle day (quiet in the
    morning / on training days), and a single result post when the race finishes."""
    from datetime import datetime
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from event_core import db
    from event_core.mind import detectors
    from event_core.mind.detectors import WarUpdateDetector

    morning = datetime(2026, 6, 20, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
    evening = datetime(2026, 6, 20, 19, 0, tzinfo=ZoneInfo("America/Chicago"))
    battle_day = {
        "clan_tag": "#CLN", "battle_phase_active": True, "final_battle_day_active": False,
        "race_completed": False, "war_day_key": "s00133-w03-p018",
        "race_rank": 1, "fame": 6870, "clan_score": 700, "battle_day_number": 3,
        "season_week_label": "Season 133 Week 3",
        "race_standings": [
            {"rank": 1, "clan_name": "POAP KINGS", "fame": 6870, "is_us": True},
            {"rank": 2, "clan_name": "55 club", "fame": 3600, "is_us": False},
        ],
    }
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        # Morning on a battle day: too early — stays quiet.
        with patch.object(detectors, "_chicago_now", return_value=morning), \
             patch("db.get_current_war_status", return_value=battle_day):
            assert WarUpdateDetector(world, conn).run() == 0

        # Evening: one standing, then idempotent.
        with patch.object(detectors, "_chicago_now", return_value=evening), \
             patch("db.get_current_war_status", return_value=battle_day):
            assert WarUpdateDetector(world, conn).run() == 1
            assert WarUpdateDetector(world, conn).run() == 0
        rows = _detection_rows(world, conn)
        assert rows["war_update"]["our_rank"] == 1
        assert rows["war_update"]["standings"][1]["clan"] == "55 club"

        # Active battle-day scoring can arrive as periodPoints while fame remains
        # zero; the generated war_update facts must carry the nonzero score.
        period_points_day = {
            **battle_day,
            "war_day_key": "s00133-w03-p019",
            "fame": 0,
            "period_points": 9125,
            "race_standings": [
                {
                    "rank": 1,
                    "clan_name": "POAP KINGS",
                    "fame": 0,
                    "period_points": 9125,
                    "is_us": True,
                },
                {
                    "rank": 2,
                    "clan_name": "55 club",
                    "fame": 0,
                    "period_points": 0,
                    "is_us": False,
                },
            ],
        }
        with patch.object(detectors, "_chicago_now", return_value=evening), \
             patch("db.get_current_war_status", return_value=period_points_day):
            assert WarUpdateDetector(world, conn).run() == 1
        _detection_rows(world, conn)
        row = conn.execute(
            "SELECT payload_json FROM detections WHERE dedup_key = ?",
            ("war_update:s00133-w03-p019",),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["our_fame"] == 0
        assert payload["our_period_points"] == 9125
        assert payload["our_active_score"] == 9125
        assert payload["score_source"] == "period_points"
        assert payload["score_label"] == "period points"
        assert payload["standings"][0]["fame"] == 0
        assert payload["standings"][0]["active_score"] == 9125
        assert payload["standings"][0]["score_label"] == "period points"

        # Training/off-season: quiet even in the evening.
        training = {**battle_day, "battle_phase_active": False, "war_day_key": "s00133-w04-p021"}
        with patch.object(detectors, "_chicago_now", return_value=evening), \
             patch("db.get_current_war_status", return_value=training):
            assert WarUpdateDetector(world, conn).run() == 0

        # Race finished: a single result post, regardless of hour.
        done = {**battle_day, "race_completed": True, "season_id": 133, "section_index": 2, "trophy_change": 30}
        with patch.object(detectors, "_chicago_now", return_value=morning), \
             patch("db.get_current_war_status", return_value=done):
            assert WarUpdateDetector(world, conn).run() == 1
        rows = _detection_rows(world, conn)
        assert "war_complete" in rows and rows["war_complete"]["final_rank"] == 1
    finally:
        conn.close()


def test_granular_level_change_emitted_after_baseline(world):
    from event_core.domain.player import player_id

    # baseline observation: no granular events
    world.observe_player_profile("#LVL", {"exp_level": 9, "name": "x"}, "t0", "h0")
    # level jump 9 -> 12 should emit PlayerLevelChanged
    world.observe_player_profile("#LVL", {"exp_level": 12, "name": "x"}, "t1", "h1")

    p = world.repository.get(player_id("#LVL"))
    assert p.profile["exp_level"] == 12
    topics = [
        n.topic.rsplit(".", 1)[-1]
        for n in world.recorder.select_notifications(start=1, limit=100)
    ]
    assert "PlayerLevelChanged" in topics


def test_career_wins_milestones_emit_after_baseline(world):
    from event_core import db
    from event_core.domain.player import player_id
    from event_core.mind.detectors import CareerWinsMilestoneDetector
    from event_core.projections.detections import DetectionsProjection

    world.observe_player_profile("#WIN", {"wins": 950, "name": "x"}, "t0", "h0")
    world.observe_player_profile(
        "#WIN",
        {"wins": 2050, "name": "x"},
        "2026-06-24T12:00:00Z",
        "h1",
    )

    p = world.repository.get(player_id("#WIN"))
    assert p.profile["wins"] == 2050
    topics = [
        n.topic.rsplit(".", 1)[-1]
        for n in world.recorder.select_notifications(start=1, limit=100)
    ]
    assert "PlayerWinsChanged" in topics

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        det = CareerWinsMilestoneDetector(world, conn)
        det.reset()
        assert det.run() == 2
        assert CareerWinsMilestoneDetector(world, conn).run() == 0

        dp = DetectionsProjection(world, conn)
        dp.setup()
        dp.run()
        rows = conn.execute(
            "SELECT dedup_key, payload_json FROM detections "
            "WHERE detection_type = 'career_wins_milestone' ORDER BY dedup_key"
        ).fetchall()
        assert [json.loads(row["payload_json"])["milestone"] for row in rows] == [1000, 2000]
    finally:
        conn.close()


def test_collection_level_milestones_emit_after_baseline(world):
    from event_core import db
    from event_core.mind.detectors import CollectionLevelMilestoneDetector
    from event_core.projections.detections import DetectionsProjection

    world.observe_player_collections(
        "#COLL",
        cards_json="[]",
        support_cards_json="[]",
        cards_hash="cards0",
        badges_json=json.dumps([{"name": "CollectionLevel", "level": 8, "progress": 1597}]),
        badges_hash="badges0",
        achievements_json="[]",
        achievements_hash="ach0",
        observed_at="t0",
    )
    world.observe_player_collections(
        "#COLL",
        cards_json="[]",
        support_cards_json="[]",
        cards_hash="cards0",
        badges_json=json.dumps([{"name": "CollectionLevel", "level": 8, "progress": 1705}]),
        badges_hash="badges1",
        achievements_json="[]",
        achievements_hash="ach0",
        observed_at="2026-06-24T12:00:00Z",
    )

    topics = [
        n.topic.rsplit(".", 1)[-1]
        for n in world.recorder.select_notifications(start=1, limit=100)
    ]
    assert "BadgeLevelChanged" in topics

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        det = CollectionLevelMilestoneDetector(world, conn)
        det.reset()
        assert det.run() == 2
        assert CollectionLevelMilestoneDetector(world, conn).run() == 0

        dp = DetectionsProjection(world, conn)
        dp.setup()
        dp.run()
        rows = conn.execute(
            "SELECT dedup_key, payload_json FROM detections "
            "WHERE detection_type = 'collection_level_milestone' ORDER BY dedup_key"
        ).fetchall()
        assert [json.loads(row["payload_json"])["milestone"] for row in rows] == [1600, 1700]
    finally:
        conn.close()


def test_detector_emits_and_is_idempotent(world):
    from event_core import db
    from event_core.mind.detectors import PlayerLevelUpDetector

    world.observe_player_profile("#D", {"exp_level": 9}, "t0", "h0")
    world.observe_player_profile("#D", {"exp_level": 12}, "2026-06-21T00:00:00Z", "h1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    det = PlayerLevelUpDetector(world, conn)
    det.reset()
    emitted = det.run()
    assert emitted == 1  # crossed level 10

    # idempotent: a fresh detector resuming sees nothing new; even a full reset
    # re-run emits 0 because the Detection id is deterministic (get-or-create)
    det2 = PlayerLevelUpDetector(world, conn)
    det2.reset()
    assert det2.run() == 0
    conn.close()


def test_detection_id_deterministic():
    from event_core.domain.detection import detection_id

    assert detection_id("player_level_up:#A:10") == detection_id("player_level_up:#A:10")
    assert detection_id("a") != detection_id("b")


legacy_missing = not os.path.exists(config.LEGACY_DB)


@pytest.mark.skipif(legacy_missing, reason="frozen legacy DB not present")
def test_mind_build_against_legacy():
    from event_core.mind.build import build_and_validate

    res = build_and_validate()
    # best_trophies detector fires in the archive window and overlaps legacy dates
    bt = res["validation"]["by_type"]["best_trophies_peak"]
    assert res["detector_emitted"]["detector:best_trophies_peak"] > 0
    assert bt["overlap"] > 0
    # (battle_hot_streak was retired as a posting signal — see roadmap item 3;
    # best_trophies_peak above already exercises the detector->legacy overlap path.)
