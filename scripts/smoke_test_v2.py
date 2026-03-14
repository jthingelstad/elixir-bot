#!/usr/bin/env python3
"""Live smoke harness for Elixir's data model.

Uses the local CR API key from .env, refreshes a small sample into the local DB,
and prints deterministic answers for representative leader/member questions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cr_api
import db


def _pretty(label, payload):
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2, default=str))


def _refresh_sample(conn, sample_limit: int):
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
    return clan, war, targets


def main():
    parser = argparse.ArgumentParser(description="Run a live smoke test against the Clash Royale API.")
    parser.add_argument("--db-path", default=":memory:", help="SQLite path to use. Default: in-memory.")
    parser.add_argument("--sample-limit", type=int, default=5, help="How many members to refresh with profile+battle data.")
    parser.add_argument("--member-tag", help="Optional member tag to use for member-specific questions. Defaults to first refreshed target.")
    args = parser.parse_args()

    load_dotenv()

    conn = db.get_connection(args.db_path)
    try:
        clan, war, targets = _refresh_sample(conn, args.sample_limit)
        active_members = clan.get("memberList", [])
        member_tag = args.member_tag or (targets[0]["tag"] if targets else active_members[0]["tag"])

        _pretty("roster_summary", db.get_clan_roster_summary(conn=conn))
        _pretty("longest_tenure", db.list_longest_tenure_members(limit=5, conn=conn))
        _pretty("recent_joins", db.list_recent_joins(days=30, conn=conn))
        _pretty("war_status", db.get_current_war_status(conn=conn))
        _pretty("war_season_summary", db.get_war_season_summary(conn=conn))
        _pretty("war_battle_win_rates", db.get_war_battle_win_rates(conn=conn))
        _pretty("boat_battle_record", db.get_clan_boat_battle_record(conn=conn))
        _pretty("war_score_trend", db.get_war_score_trend(conn=conn))
        _pretty("fame_per_member_vs_previous_season", db.compare_fame_per_member_to_previous_season(conn=conn))
        _pretty("members_without_war_participation", db.get_members_without_war_participation(conn=conn))
        _pretty("trending_war_contributors", db.get_trending_war_contributors(conn=conn))
        _pretty("recent_role_changes", db.get_recent_role_changes(conn=conn))
        _pretty("members_at_risk", db.get_members_at_risk(require_war_participation=True, season_id=db.get_current_season_id(conn=conn), conn=conn))
        _pretty("promotion_candidates", db.get_promotion_candidates(conn=conn))
        _pretty("losing_streaks", db.get_members_on_losing_streak(conn=conn))
        _pretty("member_overview", db.get_member_overview(member_tag, conn=conn))
        _pretty("member_war_attendance", db.get_member_war_attendance(member_tag, conn=conn))
        _pretty("member_war_battle_record", db.get_member_war_battle_record(member_tag, conn=conn))
        _pretty("member_missed_war_days", db.get_member_missed_war_days(member_tag, conn=conn))
        _pretty("member_war_comparison", db.compare_member_war_to_clan_average(member_tag, conn=conn))
        _pretty("member_next_chests", cr_api.get_player_chests(member_tag))

        print("\nSmoke test complete.")
        print(f"Clan members fetched: {len(active_members)}")
        print(f"Profile/battle refresh sample: {[t['tag'] for t in targets]}")
        print(f"Member-specific sample tag: {member_tag}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
