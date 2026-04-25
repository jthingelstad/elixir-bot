"""storage.member_ranks — in-clan comparative ranks for active members.

Computes 1-indexed ranks (donation_rank_week, donation_rank_season,
war_fame_rank_current_race, war_fame_rank_season) and elder-eligibility
booleans (elder_eligible, elder_eligible_crossed_this_week) for every
active member in a single pass. Consumers (via _member_reference_fields)
look up by member_id rather than re-deriving from raw rows.

The eligibility predicate here is the single source of truth: storage.
war_analytics.get_promotion_candidates calls evaluate_elder_eligibility
so the boolean and the candidate list can never disagree.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from db import managed_connection


__all__ = [
    "ELDER_ELIGIBILITY_DEFAULTS",
    "evaluate_elder_eligibility",
    "compute_member_ranks",
    "RANK_FIELDS",
]


# Same defaults used by storage.war_analytics.get_promotion_candidates.
ELDER_ELIGIBILITY_DEFAULTS = {
    "min_donations_week": 50,
    "min_tenure_days": 21,
    "active_within_days": 7,
    "min_war_races": 1,
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
    donations_week: Optional[int],
    tenure_days: Optional[int],
    days_inactive: Optional[int],
    war_races_played: Optional[int],
    season_id: Optional[int],
    min_donations_week: int = ELDER_ELIGIBILITY_DEFAULTS["min_donations_week"],
    min_tenure_days: int = ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
    active_within_days: int = ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
    min_war_races: int = ELDER_ELIGIBILITY_DEFAULTS["min_war_races"],
) -> dict:
    """Pure eligibility predicate. Returns ``{checks, all_passed}``.

    The single source of truth for "is this member elder-eligible right now."
    War check is skipped (passed) when ``season_id`` is None.
    """
    checks = {
        "donations": (donations_week or 0) >= min_donations_week,
        "tenure": tenure_days is not None and tenure_days >= min_tenure_days,
        "activity": days_inactive is not None and days_inactive <= active_within_days,
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
    from db import _current_joined_at, _parse_cr_time
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
    from storage.awards import _season_metric_date_bounds
    start, end = _season_metric_date_bounds(conn, season_id)
    if not start or not end:
        return
    rows = conn.execute(
        """
        WITH weekly_peaks AS (
            SELECT d.member_id,
                   strftime('%Y-%W', d.metric_date) AS iso_week,
                   MAX(COALESCE(d.donations_week, 0)) AS week_peak
            FROM member_daily_metrics d
            WHERE d.metric_date BETWEEN ? AND ?
            GROUP BY d.member_id, iso_week
        )
        SELECT wp.member_id, SUM(wp.week_peak) AS total_donations,
               m.current_name
        FROM weekly_peaks wp
        JOIN members m ON m.member_id = wp.member_id
        WHERE m.status = 'active'
        GROUP BY wp.member_id
        HAVING total_donations > 0
        ORDER BY total_donations DESC, m.current_name COLLATE NOCASE
        """,
        (start, end),
    ).fetchall()
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
    """Set elder_eligible + elder_eligible_crossed_this_week for active
    members in the role 'member'. Leaders, co-leaders, and existing elders
    stay at ``None`` — the predicate does not apply to them.
    """
    from db import _current_joined_at, _parse_cr_time

    rows = conn.execute(
        "SELECT m.member_id, cs.role, cs.donations_week, cs.last_seen_api "
        "FROM members m "
        "JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active' AND cs.role = 'member'"
    ).fetchall()
    for row in rows:
        member_id = row["member_id"]
        if member_id not in ranks:
            continue
        joined_date = _current_joined_at(conn, member_id)
        tenure_days = None
        if joined_date:
            try:
                tenure_days = (today - datetime.strptime(joined_date[:10], "%Y-%m-%d").date()).days
            except ValueError:
                tenure_days = None
        last_seen = _parse_cr_time(row["last_seen_api"])
        days_inactive = (today - last_seen.date()).days if last_seen else None
        war_races_played = 0
        if season_id is not None:
            war_races_played = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_participation wp "
                "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (season_id, member_id),
            ).fetchone()["cnt"]

        eligibility = evaluate_elder_eligibility(
            donations_week=row["donations_week"],
            tenure_days=tenure_days,
            days_inactive=days_inactive,
            war_races_played=war_races_played,
            season_id=season_id,
        )
        ranks[member_id]["elder_eligible"] = eligibility["all_passed"]
        ranks[member_id]["elder_eligible_crossed_this_week"] = (
            _crossed_this_week(conn, member_id, today, eligibility["all_passed"], tenure_days)
        )


def _crossed_this_week(
    conn, member_id, today, currently_eligible: bool, tenure_days: Optional[int]
) -> bool:
    """Conservative: True only when there's strong evidence the member was
    NOT eligible 7 days ago. Returns False whenever evidence is missing.

    Strong evidence sources:
    - Tenure: tenure_now < 28 means tenure was < 21 seven days ago.
    - Donations: a daily-metrics row from 7±1 days ago shows
      donations_week below the threshold.
    """
    if not currently_eligible:
        return False
    threshold = ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"]
    if tenure_days is not None and tenure_days < threshold + 7:
        return True
    seven_days_ago = today - timedelta(days=7)
    # Allow ±1 day for snapshot-cadence drift (some days may be missing).
    window_start = (seven_days_ago - timedelta(days=1)).isoformat()
    window_end = (seven_days_ago + timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT donations_week FROM member_daily_metrics "
        "WHERE member_id = ? AND metric_date BETWEEN ? AND ? "
        "ORDER BY metric_date DESC LIMIT 1",
        (member_id, window_start, window_end),
    ).fetchone()
    if not row or row["donations_week"] is None:
        return False
    return row["donations_week"] < ELDER_ELIGIBILITY_DEFAULTS["min_donations_week"]
