"""Source-normalization regression: a MEMBER's War Race contribution is
labeled **points**, never **fame** (fame is a CLAN-only concept — the boat).

These pin the member-facing key contract at the two read surfaces that used to
leak "fame" onto individual members:

  (a) get_war_season(aspect="standings") — the War Champ leaderboard.
  (b) the live river-race engagement day-state (top points leaders + per-member
      participant rows).

If either regresses to a member `fame`/`total_fame` key, the bot will once again
tell a player "you have N fame," which is wrong.
"""
from __future__ import annotations

from unittest.mock import patch

import db
from agent import tool_exec
from storage import war_status

SEASON = 140

# Any member-facing key that would (re)label a member's contribution as fame.
_FORBIDDEN_MEMBER_KEYS = {
    "fame",
    "total_fame",
    "avg_fame",
    "fame_today",
    "finalized_fame",
    "in_progress_fame",
}


def _seed_standings(conn):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-06', 1)"
    )
    conn.execute(
        "INSERT INTO war_seasons (season_id, started_at, ended_at, final_rank, weeks) "
        "VALUES (?, '2026-06-01', '2026-06-30', 1, 2)",
        (SEASON,),
    )
    for tag, name in (("#A", "Alpha"), ("#B", "Bravo"), ("#C", "Carol")):
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-02-01', '2026-07-06')",
            (tag, name),
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, '2026-03-01', 'test')",
            (tag,),
        )
    for section in (0, 1):
        conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, created_date, finish_time) "
            "VALUES (?, ?, ?, ?)",
            (SEASON, section, f"2026-06-1{section}", f"2026-06-2{section}"),
        )
        for tag, fame in (("#A", 3000), ("#B", 2500), ("#C", 2000)):
            conn.execute(
                "INSERT INTO war_participation (season_id, section_index, player_tag, "
                "fame, decks_used, observed_at) VALUES (?, ?, ?, ?, 16, '2026-06-20T10:00:00Z')",
                (SEASON, section, tag, fame),
            )
    conn.commit()


def test_get_war_season_standings_emits_points_not_fame():
    conn = db.get_connection()
    try:
        _seed_standings(conn)
    finally:
        conn.close()

    result = tool_exec._execute_get_war_season(
        {"aspect": "standings", "season_id": SEASON}
    )

    # The metric name itself is points, not fame.
    assert result["metric"] == "points"

    members = result["members"]
    assert members, "expected seeded War Champ standings"
    for m in members:
        assert "total_points" in m
        assert m["total_points"] > 0
        for bad in _FORBIDDEN_MEMBER_KEYS:
            assert bad not in m, f"member dict leaked '{bad}': {m}"


def test_get_war_season_standings_accepts_fame_alias_but_returns_points():
    """`metric="fame"` stays accepted (back-compat) but normalizes to points."""
    conn = db.get_connection()
    try:
        _seed_standings(conn)
    finally:
        conn.close()

    result = tool_exec._execute_get_war_season(
        {"aspect": "standings", "season_id": SEASON, "metric": "fame"}
    )
    assert result["metric"] == "points"
    assert result["members"]


def _live_projection(participants):
    return {
        "season_id": SEASON,
        "section_index": 0,
        "period_index": 4,
        "period_type": "warDay",
        "our_tag": "#HOME",
        "our_fame": 3435,
        "clans": {"#HOME": {"name": "POAP KINGS", "fame": 3435, "period_points": 900}},
        "participants": participants,
    }


def test_engagement_day_state_keys_members_as_points():
    participants = {
        "#A": {"name": "Alpha", "fame": 300, "repair_points": 10,
               "boat_attacks": 2, "decks_used": 8, "decks_used_today": 4},
        "#B": {"name": "Bravo", "fame": 150, "repair_points": 5,
               "boat_attacks": 1, "decks_used": 4, "decks_used_today": 0},
    }
    with patch.object(
        war_status, "_live_race",
        return_value=(_live_projection(participants), "2026-07-11T07:06:00Z"),
    ):
        state = war_status.get_war_day_state(conn=None)

    assert state is not None
    # The season points leaderboard the engagement tool forwards verbatim.
    leaders = state["top_points_total"]
    assert leaders, "expected top points leaders"
    assert not any(k.startswith("top_fame") for k in state)

    for member in leaders:
        assert "points" in member
        assert "fame" not in member
        assert "fame_today" not in member
        assert "points_today" in member

    # Every participant row is keyed points, never fame.
    for member in state["participants"]:
        assert "points" in member
        assert "fame" not in member

    # The leader is the highest-points member.
    assert leaders[0]["points"] == 300
