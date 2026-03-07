"""Representative question coverage tests for the V2 data model."""

from datetime import datetime, timedelta, timezone

import db


def _seed_core_fixture(conn):
    db.snapshot_members(
        [
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "role": "leader",
                "expLevel": 66,
                "trophies": 11429,
                "bestTrophies": 11433,
                "clanRank": 1,
                "donations": 150,
                "donationsReceived": 80,
                "arena": {"id": 54000131, "name": "Musketeer Street"},
            },
            {
                "tag": "#DEF456",
                "name": "Vijay",
                "role": "member",
                "expLevel": 64,
                "trophies": 9020,
                "bestTrophies": 9300,
                "clanRank": 2,
                "donations": 75,
                "donationsReceived": 40,
                "arena": {"id": 54000130, "name": "Boot Camp"},
            },
            {
                "tag": "#GHI789",
                "name": "Finn",
                "role": "member",
                "expLevel": 62,
                "trophies": 8700,
                "bestTrophies": 8900,
                "clanRank": 3,
                "donations": 20,
                "donationsReceived": 10,
                "arena": {"id": 54000129, "name": "Silent Sanctuary"},
            },
        ],
        conn=conn,
    )
    db.set_member_join_date("#ABC123", "King Levy", "2024-01-15", conn=conn)
    db.set_member_join_date("#DEF456", "Vijay", "2025-10-01", conn=conn)
    recent_joined = (datetime.now(timezone.utc).date() - timedelta(days=10)).strftime("%Y-%m-%d")
    db.set_member_join_date("#GHI789", "Finn", recent_joined, conn=conn)
    db.link_discord_user_to_member("123", "#ABC123", username="jamie", display_name="King Levy", conn=conn)
    db.link_discord_user_to_member("456", "#DEF456", username="vijay", display_name="Vijay", conn=conn)

    member_id = conn.execute("SELECT member_id FROM members WHERE player_tag = '#DEF456'").fetchone()["member_id"]
    conn.execute(
        "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
        "VALUES (?, ?, 'competitive_10', 10, 2, 8, 0, 4, 'L', 0.2, -1.3, -18.0, 'cold', '2-8 over the last 10 battles (cold).')",
        (member_id, "2026-03-07T12:00:00"),
    )
    conn.commit()

    db.upsert_war_current_state(
        {
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 9000,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 140,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 1},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                    {"tag": "#GHI789", "name": "Finn", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                ],
            },
        },
        conn=conn,
    )
    db.store_war_log(
        {
            "items": [
                {
                    "seasonId": 129,
                    "sectionIndex": 3,
                    "createdDate": "20260302T095140.000Z",
                    "standings": [
                        {
                            "rank": 1,
                            "trophyChange": 100,
                            "clan": {
                                "tag": "#J2RGCRVG",
                                "name": "POAP KINGS",
                                "fame": 12850,
                                "finishTime": "20260302T180000.000Z",
                                "participants": [
                                    {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    {"tag": "#DEF456", "name": "Vijay", "fame": 1800, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 0},
                                ],
                            },
                        }
                    ],
                }
            ]
        },
        "J2RGCRVG",
        conn=conn,
    )


def test_leader_question_views_are_covered():
    conn = db.get_connection(":memory:")
    try:
        _seed_core_fixture(conn)

        roster = db.list_members(conn=conn)
        assert roster[0]["joined_date"] == "2024-01-15"

        missing = db.get_members_without_war_participation(season_id=129, conn=conn)
        assert [m["tag"] for m in missing["members"]] == ["#GHI789"]

        summary = db.get_clan_roster_summary(conn=conn)
        assert summary["active_members"] == 3
        assert summary["open_slots"] == 47

        tenure = db.list_longest_tenure_members(conn=conn)
        assert tenure[0]["tag"] == "#ABC123"

        war = db.get_current_war_status(conn=conn)
        assert war["season_id"] == 129
        assert war["week"] == 4
    finally:
        conn.close()


def test_member_question_views_are_covered():
    conn = db.get_connection(":memory:")
    try:
        _seed_core_fixture(conn)

        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "riverRacePvP",
                    "battleTime": "20260302T100000.000Z",
                    "gameMode": {"id": 72000061, "name": "River Race PvP"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#ZZZ111", "name": "Opp 1", "crowns": 1, "cards": []}],
                }
            ],
            conn=conn,
        )

        war_status = db.get_member_war_status("#ABC123", conn=conn)
        assert war_status["current_day"]["decks_left_today"] == 3

        recent = db.get_member_recent_form("#DEF456", conn=conn)
        assert recent["losses"] == 8

        comparison = db.compare_member_war_to_clan_average("#ABC123", season_id=129, conn=conn)
        assert comparison["member"]["total_fame"] == 3600
        assert comparison["clan_average"]["avg_total_fame"] == 2700.0

        slumping = db.get_members_on_losing_streak(min_streak=3, conn=conn)
        assert slumping[0]["tag"] == "#DEF456"

        resolved = db.resolve_member("@jamie", conn=conn)
        assert resolved[0]["member_ref_with_handle"] == "King Levy (@jamie)"

        attendance = db.get_member_war_attendance("#ABC123", conn=conn)
        assert attendance["season"]["races_played"] == 1

        record = db.get_member_war_battle_record("#ABC123", conn=conn)
        assert record["wins"] == 1
    finally:
        conn.close()


def test_additional_leader_views_are_covered():
    conn = db.get_connection(":memory:")
    try:
        _seed_core_fixture(conn)
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 128,
                        "sectionIndex": 1,
                        "createdDate": "20260215T120000.000Z",
                        "standings": [
                            {
                                "rank": 2,
                                "trophyChange": -50,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 9000,
                                    "finishTime": "20260215T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 2000, "repairPoints": 0, "boatAttacks": 1, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 1800, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        conn.execute(
            "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES "
            "('2026-02-10T10:00:00', 'full', '#J2RGCRVG', 'POAP KINGS', 5000, 0, 0, 120, '{}'), "
            "('2026-03-05T10:00:00', 'full', '#J2RGCRVG', 'POAP KINGS', 7000, 0, 0, 150, '{}')"
        )
        conn.commit()
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "boatBattle",
                    "battleTime": "20260301T150000.000Z",
                    "gameMode": {"id": 72000062, "name": "Boat Battle"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": []}],
                }
            ],
            conn=conn,
        )

        trend = db.get_war_score_trend(days=30, conn=conn)
        assert trend["direction"] == "up"

        fame_compare = db.compare_fame_per_member_to_previous_season(season_id=129, conn=conn)
        assert fame_compare["previous_season_id"] == 128

        boat = db.get_clan_boat_battle_record(wars=2, conn=conn)
        assert boat["wins"] == 1
    finally:
        conn.close()
