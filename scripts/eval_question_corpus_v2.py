#!/usr/bin/env python3
"""Evaluate representative V2 leader/member questions.

Modes:
- fixture: seed an in-memory database with deterministic test data
- live: refresh from the Clash Royale API using the local .env token
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cr_api
import db


def _seed_fixture(conn):
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

    member_id = conn.execute("SELECT member_id FROM members WHERE player_tag = '#DEF456'").fetchone()["member_id"]
    conn.execute(
        "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
        "VALUES (?, ?, 'competitive_10', 10, 2, 8, 0, 4, 'L', 0.2, -1.3, -18.0, 'cold', '2-8 over the last 10 battles (cold).')",
        (member_id, "2026-03-07T12:00:00"),
    )
    conn.execute(
        "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
        "VALUES ((SELECT member_id FROM members WHERE player_tag = '#ABC123'), ?, 'competitive_10', 10, 7, 3, 0, 2, 'W', 0.7, 1.6, 12.0, 'strong', '7-3 over the last 10 battles (strong).')",
        ("2026-03-07T12:00:00",),
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
                },
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "createdDate": "20260301T120000.000Z",
                    "standings": [
                        {
                            "rank": 1,
                            "trophyChange": 100,
                            "clan": {
                                "tag": "#J2RGCRVG",
                                "name": "POAP KINGS",
                                "fame": 12000,
                                "finishTime": "20260301T180000.000Z",
                                "participants": [
                                    {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 1, "decksUsed": 4, "decksUsedToday": 0},
                                    {"tag": "#DEF456", "name": "Vijay", "fame": 1800, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 0},
                                ],
                            },
                        }
                    ],
                },
            ]
        },
        "J2RGCRVG",
        conn=conn,
    )
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
            },
            {
                "type": "boatBattle",
                "battleTime": "20260301T150000.000Z",
                "gameMode": {"id": 72000062, "name": "Boat Battle"},
                "arena": {"id": 1, "name": "Arena 1"},
                "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "cards": [], "supportCards": []}],
                "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": []}],
            },
        ],
        conn=conn,
    )
    conn.execute(
        "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES "
        "('2026-02-10T10:00:00', 'full', '#J2RGCRVG', 'POAP KINGS', 5000, 0, 0, 120, '{}'), "
        "('2026-03-05T10:00:00', 'full', '#J2RGCRVG', 'POAP KINGS', 7000, 0, 0, 150, '{}')"
    )
    conn.commit()
    return "#ABC123"


def _refresh_live(conn, sample_limit: int):
    clan = cr_api.get_clan() or {}
    members = clan.get("memberList", [])
    if not members:
        raise RuntimeError("No clan member data returned from CR API.")
    db.snapshot_members(members, conn=conn)
    war = cr_api.get_current_war() or {}
    if war:
        db.upsert_war_current_state(war, conn=conn)
    war_log = cr_api.get_river_race_log() or {}
    if war_log:
        db.store_war_log(war_log, cr_api.CLAN_TAG, conn=conn)
    targets = db.get_player_intel_refresh_targets(limit=sample_limit, stale_after_hours=0, conn=conn)
    for target in targets:
        tag = target["tag"]
        profile = cr_api.get_player(tag)
        if profile:
            db.snapshot_player_profile(profile, conn=conn)
        battle_log = cr_api.get_player_battle_log(tag)
        if battle_log:
            db.snapshot_player_battlelog(tag, battle_log, conn=conn)
    return targets[0]["tag"] if targets else members[0]["tag"]


def _build_cases(member_ref):
    return [
        ("leader", "Who are the members of the clan and when did they join?", lambda conn: db.list_members(conn=conn)),
        ("leader", "Which members haven't played any war decks this season?", lambda conn: db.get_members_without_war_participation(conn=conn)),
        ("leader", "What is our clan's win/loss record in boat battles over the last 3 wars?", lambda conn: db.get_clan_boat_battle_record(wars=3, conn=conn)),
        ("leader", "Has our clan's war rating trended up or down over the past month?", lambda conn: db.get_war_score_trend(days=30, conn=conn)),
        ("leader", "How does our clan's fame-per-member compare to last season?", lambda conn: db.compare_fame_per_member_to_previous_season(conn=conn)),
        ("leader", "Which members recently earned promotions or demotions in role?", lambda conn: db.get_recent_role_changes(conn=conn)),
        ("member", "How many war decks do I have left to play this war week?", lambda conn: db.get_member_war_status(member_ref, conn=conn)),
        ("member", "What is my current war participation rate over the last 4 weeks?", lambda conn: db.get_member_war_attendance(member_ref, conn=conn)),
        ("member", "What is my win/loss record in war battles this season?", lambda conn: db.get_member_war_battle_record(member_ref, conn=conn)),
        ("member", "Did I miss any war days last season?", lambda conn: db.get_member_missed_war_days(member_ref, conn=conn)),
    ]


def main():
    parser = argparse.ArgumentParser(description="Evaluate representative V2 question coverage.")
    parser.add_argument("--mode", choices=["fixture", "live"], default="fixture")
    parser.add_argument("--db-path", default=":memory:")
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()

    load_dotenv()
    conn = db.get_connection(args.db_path)
    try:
        if args.mode == "fixture":
            member_ref = _seed_fixture(conn)
        else:
            member_ref = _refresh_live(conn, args.sample_limit)

        cases = _build_cases(member_ref)
        for audience, question, fn in cases:
            payload = fn(conn)
            print(f"\n[{audience}] {question}")
            print(json.dumps(payload, indent=2, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
