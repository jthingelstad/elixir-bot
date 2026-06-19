"""storage.member_ranks — in-clan comparative ranks for active members.

Computes 1-indexed ranks (donation_rank_week, donation_rank_season,
war_fame_rank_current_race, war_fame_rank_season) and Elder-board booleans
(elder_eligible, elder_eligible_crossed_this_week) for every active member
in a single pass. Consumers (via _member_reference_fields) look up by
member_id rather than re-deriving from raw rows.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import managed_connection


__all__ = [
    "ELDER_ELIGIBILITY_DEFAULTS",
    "evaluate_elder_eligibility",
    "compute_member_ranks",
    "RANK_FIELDS",
]


# Same gate defaults used by storage.war_analytics.get_promotion_candidates.
ELDER_ELIGIBILITY_DEFAULTS = {
    "min_tenure_days": 0,
    "active_within_days": 7,
    "min_war_races": 1,
    "rolling_donation_weeks": 4,
}

# Field names every active member reference picks up. Inactive members get
# all of these as None.
RANK_FIELDS = (
    "donation_rank_week",
    "donation_rank_season",
    "war_fame_rank_current_race",
    "war_fame_rank_season",
    "elder_eligible",
    "elder_eligible_crossed_this_week",
)


def evaluate_elder_eligibility(
    *,
    tenure_days: Optional[int] = None,
    days_since_battle: Optional[int] = None,
    days_inactive: Optional[int] = None,
    war_races_played: Optional[int] = None,
    season_id: Optional[int] = None,
    min_tenure_days: int = ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
    active_within_days: int = ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
    min_war_races: int = ELDER_ELIGIBILITY_DEFAULTS["min_war_races"],
    donations_week: Optional[int] = None,
    min_donations_week: Optional[int] = None,
) -> dict:
    """Pure activity gate predicate. Returns ``{checks, all_passed}``.

    Donation volume is intentionally not an absolute check here. Elder
    recommendations are relative to the smoothed donation leaderboard.
    """
    activity_days = days_since_battle if days_since_battle is not None else days_inactive
    checks = {
        "tenure": min_tenure_days <= 0 or (tenure_days is not None and tenure_days >= min_tenure_days),
        "activity": activity_days is not None and activity_days <= active_within_days,
        "war": season_id is None or (war_races_played or 0) >= min_war_races,
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
    }


@managed_connection
def compute_member_ranks(conn: Optional[sqlite3.Connection] = None) -> dict[int, dict]:
    """Return ``{member_id: {rank_fields...}}`` for every active member.

    Single-pass: one query per rank dimension, results joined by member_id.
    Inactive members are omitted entirely; the calling enricher fills None
    for any member_id not present.
    """
    from storage.war_analytics import _member_activity_anchor
    from storage.war_status import get_current_season_id

    active_ids = [
        row["member_id"]
        for row in conn.execute(
            "SELECT member_id FROM members WHERE status = 'active'"
        ).fetchall()
    ]
    if not active_ids:
        return {}

    ranks: dict[int, dict] = {
        mid: {field: None for field in RANK_FIELDS} for mid in active_ids
    }

    season_id = get_current_season_id(conn=conn)
    today = _member_activity_anchor(conn).date()

    _populate_donation_rank_week(conn, ranks)
    _populate_donation_rank_season(conn, ranks, season_id)
    current_race_id = _current_war_race_id(conn, season_id)
    _populate_war_fame_rank_current_race(conn, ranks, current_race_id)
    _populate_war_fame_rank_season(conn, ranks, season_id)
    _populate_elder_eligibility(conn, ranks, today, season_id)
    return ranks


# -- per-rank populators ----------------------------------------------------

def _populate_donation_rank_week(conn, ranks):
    """1-indexed rank by ``member_current_state.donations_week`` DESC.

    Tie-break: clan_rank ASC (game's in-clan position), then name ASC.
    Members without a current_state row get ``None``.
    """
    rows = conn.execute(
        "SELECT m.member_id, cs.donations_week, cs.clan_rank, m.current_name "
        "FROM members m "
        "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active' AND cs.donations_week IS NOT NULL "
        "ORDER BY cs.donations_week DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE"
    ).fetchall()
    for i, row in enumerate(rows):
        if row["member_id"] in ranks:
            ranks[row["member_id"]]["donation_rank_week"] = i + 1


def _populate_donation_rank_season(conn, ranks, season_id):
    """1-indexed rank by season-to-date donations.

    Sums per-member weekly peaks of ``member_daily_metrics.donations_week``
    within the season window — same shape as
    ``get_season_donation_leaderboard`` so mid-season standings line up
    with the season-end Donation Champ result.
    """
    if season_id is None:
        return
    from storage.awards import _season_donation_rows
    rows = _season_donation_rows(conn, season_id)
    for i, row in enumerate(rows):
        if row["member_id"] in ranks:
            ranks[row["member_id"]]["donation_rank_season"] = i + 1


def _current_war_race_id(conn, season_id):
    """Most recent race in the current season — in-progress if one exists,
    else the latest completed race. None when no season."""
    if season_id is None:
        return None
    row = conn.execute(
        "SELECT war_race_id FROM war_races WHERE season_id = ? "
        "ORDER BY section_index DESC LIMIT 1",
        (season_id,),
    ).fetchone()
    return row["war_race_id"] if row else None


def _populate_war_fame_rank_current_race(conn, ranks, war_race_id):
    """1-indexed rank by fame in the current/most-recent race.

    Tie-break: more decks_used wins; then name. Active members with no
    participation row stay at ``None``.
    """
    if war_race_id is None:
        return
    rows = conn.execute(
        "SELECT wp.member_id, wp.fame, wp.decks_used, m.current_name "
        "FROM war_participation wp "
        "JOIN members m ON m.member_id = wp.member_id "
        "WHERE wp.war_race_id = ? AND m.status = 'active' "
        "AND COALESCE(wp.fame, 0) > 0 "
        "ORDER BY wp.fame DESC, COALESCE(wp.decks_used, 0) DESC, m.current_name COLLATE NOCASE",
        (war_race_id,),
    ).fetchall()
    for i, row in enumerate(rows):
        if row["member_id"] in ranks:
            ranks[row["member_id"]]["war_fame_rank_current_race"] = i + 1


def _populate_war_fame_rank_season(conn, ranks, season_id):
    """1-indexed rank by season-to-date war fame.

    Mirrors ``get_war_champ_standings`` ordering so mid-season standings
    align with the season-end War Champ result.
    """
    if season_id is None:
        return
    rows = conn.execute(
        "SELECT wp.member_id, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
        "       COUNT(*) AS races_participated, m.current_name "
        "FROM war_participation wp "
        "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "JOIN members m ON m.member_id = wp.member_id "
        "WHERE wr.season_id = ? AND m.status = 'active' "
        "AND COALESCE(wp.fame, 0) > 0 "
        "GROUP BY wp.member_id "
        "ORDER BY total_fame DESC, races_participated DESC, m.current_name COLLATE NOCASE",
        (season_id,),
    ).fetchall()
    for i, row in enumerate(rows):
        if row["member_id"] in ranks:
            ranks[row["member_id"]]["war_fame_rank_season"] = i + 1


def _populate_elder_eligibility(conn, ranks, today, season_id):
    """Set member-facing Elder-board flags from the shared role review."""
    from storage.war_analytics import _elder_role_review

    review = _elder_role_review(conn=conn, enrich=False)
    for item in review.get("reviewed") or []:
        member_id = item.get("member_id")
        if member_id not in ranks:
            continue
        if item.get("role") != "member":
            continue
        ranks[member_id]["elder_eligible"] = bool(item.get("in_elder_target"))
        # The old threshold-crossing field no longer has a reliable meaning in
        # a relative leaderboard model, so keep it conservative.
        ranks[member_id]["elder_eligible_crossed_this_week"] = False
