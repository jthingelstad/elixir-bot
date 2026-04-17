"""storage.awards — season-scoped award grants and reads.

Awards are durable recognition records for clan members, scoped to a war
season. Grant queries compute candidates from existing war/member tables;
``insert_award`` writes one row per (award_type, season_id, section_index,
member_id) — the UNIQUE constraint keeps grants idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from db import (
    _canon_tag,
    _parse_cr_time,
    _utcnow,
    managed_connection,
)
from storage.war_status import _season_bounds, get_current_season_id


SEASON_WIDE_SECTION = -1


__all__ = [
    "SEASON_WIDE_SECTION",
    "insert_award",
    "get_member_trophy_case",
    "get_awards_by_season",
    "get_iron_king_candidates",
    "get_perfect_week_candidates",
    "get_season_donation_leaderboard",
    "get_rookie_mvp_candidates",
    "get_war_participant_candidates",
    "season_final_section_index",
    "season_is_complete",
]


# -- grant writer -----------------------------------------------------------

@managed_connection
def insert_award(
    award_type: str,
    season_id: int,
    member_id: int,
    player_tag: str,
    *,
    section_index: Optional[int] = None,
    rank: int = 1,
    metric_value: Optional[float] = None,
    metric_unit: Optional[str] = None,
    metadata: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Idempotently record an award. Returns True iff a new row was inserted."""
    section = SEASON_WIDE_SECTION if section_index is None else int(section_index)
    cur = conn.execute(
        "INSERT OR IGNORE INTO awards ("
        "award_type, season_id, section_index, member_id, player_tag, rank, "
        "metric_value, metric_unit, metadata_json, awarded_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            award_type,
            int(season_id),
            section,
            int(member_id),
            _canon_tag(player_tag),
            int(rank),
            float(metric_value) if metric_value is not None else None,
            metric_unit,
            json.dumps(metadata) if metadata else None,
            _utcnow(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


# -- reads ------------------------------------------------------------------

def _row_to_award(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_meta = data.pop("metadata_json", None)
    try:
        data["metadata"] = json.loads(raw_meta) if raw_meta else {}
    except (TypeError, ValueError):
        data["metadata"] = {}
    if data.get("section_index") == SEASON_WIDE_SECTION:
        data["section_index"] = None
    return data


@managed_connection
def get_member_trophy_case(
    member_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    rows = conn.execute(
        "SELECT award_id, award_type, season_id, section_index, player_tag, rank, "
        "metric_value, metric_unit, metadata_json, awarded_at "
        "FROM awards WHERE member_id = ? "
        "ORDER BY season_id DESC, award_type, rank, section_index",
        (int(member_id),),
    ).fetchall()
    return [_row_to_award(r) for r in rows]


@managed_connection
def get_awards_by_season(
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Return a list of season dicts, each with an ``awards`` list.

    Newest season first. Every award row carries player_name resolved from
    ``members.current_name`` at read time.
    """
    rows = conn.execute(
        "SELECT a.award_id, a.award_type, a.season_id, a.section_index, "
        "a.member_id, a.player_tag, a.rank, a.metric_value, a.metric_unit, "
        "a.metadata_json, a.awarded_at, m.current_name AS player_name "
        "FROM awards a JOIN members m ON m.member_id = a.member_id "
        "ORDER BY a.season_id DESC, a.award_type, a.rank, a.section_index"
    ).fetchall()
    seasons: dict[int, dict] = {}
    for raw in rows:
        award = _row_to_award(raw)
        season_id = award["season_id"]
        if season_id not in seasons:
            start, end = _season_bounds(conn, season_id)
            seasons[season_id] = {
                "season_id": season_id,
                "season_start": _cr_time_to_date(start),
                "season_end": _cr_time_to_date(end),
                "awards": [],
            }
        seasons[season_id]["awards"].append(award)
    return list(seasons.values())


def _cr_time_to_date(value: Optional[str]) -> Optional[str]:
    dt = _parse_cr_time(value)
    return dt.date().isoformat() if dt else None


# -- grant queries ----------------------------------------------------------

@managed_connection
def get_iron_king_candidates(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members who used 4/4 decks on every battle day observed in the season.

    Players who joined mid-season and therefore missed early battle days are
    excluded naturally — they simply don't have snapshots for those days, so
    their ``days_played`` is less than the season's ``total_battle_days``.
    """
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    total_row = conn.execute(
        "SELECT COUNT(DISTINCT war_day_key) AS total "
        "FROM war_participant_snapshots "
        "WHERE season_id = ? AND phase = 'battle'",
        (season_id,),
    ).fetchone()
    total_battle_days = total_row["total"] if total_row else 0
    if total_battle_days == 0:
        return []
    rows = conn.execute(
        """
        WITH final_values AS (
            SELECT wps1.player_tag, wps1.member_id, wps1.war_day_key,
                   wps1.decks_used_today
            FROM war_participant_snapshots wps1
            WHERE wps1.season_id = ? AND wps1.phase = 'battle'
              AND wps1.observed_at = (
                  SELECT MAX(wps2.observed_at)
                  FROM war_participant_snapshots wps2
                  WHERE wps2.season_id = wps1.season_id
                    AND wps2.phase = 'battle'
                    AND wps2.war_day_key = wps1.war_day_key
                    AND wps2.player_tag = wps1.player_tag
              )
        )
        SELECT fv.player_tag AS tag,
               m.current_name AS name,
               fv.member_id,
               COUNT(*) AS days_played,
               SUM(CASE WHEN fv.decks_used_today = 4 THEN 1 ELSE 0 END) AS perfect_days
        FROM final_values fv
        JOIN members m ON m.member_id = fv.member_id
        WHERE m.status = 'active'
        GROUP BY fv.player_tag, fv.member_id
        HAVING days_played = ? AND perfect_days = ?
        ORDER BY fv.player_tag
        """,
        (season_id, total_battle_days, total_battle_days),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "days_played": r["days_played"],
            "perfect_days": r["perfect_days"],
            "total_battle_days": total_battle_days,
        }
        for r in rows
    ]


@managed_connection
def get_perfect_week_candidates(
    season_id: Optional[int],
    section_index: int,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members who used 4/4 decks on every battle day of one war week."""
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    total_row = conn.execute(
        "SELECT COUNT(DISTINCT war_day_key) AS total "
        "FROM war_participant_snapshots "
        "WHERE season_id = ? AND section_index = ? AND phase = 'battle'",
        (season_id, section_index),
    ).fetchone()
    total_days = total_row["total"] if total_row else 0
    if total_days == 0:
        return []
    rows = conn.execute(
        """
        WITH final_values AS (
            SELECT wps1.player_tag, wps1.member_id, wps1.war_day_key,
                   wps1.decks_used_today
            FROM war_participant_snapshots wps1
            WHERE wps1.season_id = ? AND wps1.section_index = ?
              AND wps1.phase = 'battle'
              AND wps1.observed_at = (
                  SELECT MAX(wps2.observed_at)
                  FROM war_participant_snapshots wps2
                  WHERE wps2.season_id = wps1.season_id
                    AND wps2.section_index = wps1.section_index
                    AND wps2.phase = 'battle'
                    AND wps2.war_day_key = wps1.war_day_key
                    AND wps2.player_tag = wps1.player_tag
              )
        )
        SELECT fv.player_tag AS tag,
               m.current_name AS name,
               fv.member_id,
               COUNT(*) AS days_played,
               SUM(CASE WHEN fv.decks_used_today = 4 THEN 1 ELSE 0 END) AS perfect_days
        FROM final_values fv
        JOIN members m ON m.member_id = fv.member_id
        WHERE m.status = 'active'
        GROUP BY fv.player_tag, fv.member_id
        HAVING days_played = ? AND perfect_days = ?
        ORDER BY fv.player_tag
        """,
        (season_id, section_index, total_days, total_days),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "section_index": section_index,
            "total_battle_days": total_days,
        }
        for r in rows
    ]


@managed_connection
def get_season_donation_leaderboard(
    season_id: Optional[int] = None,
    limit: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Top-N donors for the season by summing the weekly peak per member.

    ``member_daily_metrics.donations_week`` is a rolling weekly counter that
    resets on Monday. For each member, we take the MAX per CR week within the
    season window, then sum across weeks. This gives a season total that is
    robust to snapshots landing on different days of the week.
    """
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    start, end = _season_metric_date_bounds(conn, season_id)
    if not start or not end:
        return []
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
        SELECT m.player_tag AS tag,
               m.current_name AS name,
               wp.member_id,
               SUM(wp.week_peak) AS total_donations
        FROM weekly_peaks wp
        JOIN members m ON m.member_id = wp.member_id
        WHERE m.status = 'active'
        GROUP BY wp.member_id
        HAVING total_donations > 0
        ORDER BY total_donations DESC
        LIMIT ?
        """,
        (start, end, limit),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "total_donations": r["total_donations"],
            "rank": i + 1,
        }
        for i, r in enumerate(rows)
    ]


@managed_connection
def get_rookie_mvp_candidates(
    season_id: Optional[int] = None,
    limit: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Top-N fame among members whose current membership began during the season.

    Uses the most recent ``clan_memberships.joined_at`` (left_at IS NULL) — a
    returning member who rejoined mid-season counts as a rookie for this season.
    """
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    start, end = _season_bounds(conn, season_id)
    if not start or not end:
        return []
    rows = conn.execute(
        """
        SELECT wp.player_tag AS tag,
               MAX(m.current_name) AS name,
               wp.member_id,
               SUM(COALESCE(wp.fame, 0)) AS total_fame,
               COUNT(*) AS races_participated
        FROM war_participation wp
        JOIN war_races wr ON wr.war_race_id = wp.war_race_id
        JOIN members m ON m.member_id = wp.member_id
        JOIN clan_memberships cm
          ON cm.member_id = wp.member_id
         AND cm.left_at IS NULL
         AND cm.joined_at >= ?
         AND cm.joined_at < ?
        WHERE wr.season_id = ? AND m.status = 'active'
        GROUP BY wp.player_tag, wp.member_id
        HAVING total_fame > 0
        ORDER BY total_fame DESC, races_participated DESC
        LIMIT ?
        """,
        (_cr_time_to_iso(start), _cr_time_to_iso(end), season_id, limit),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "total_fame": r["total_fame"],
            "races_participated": r["races_participated"],
            "rank": i + 1,
        }
        for i, r in enumerate(rows)
    ]


@managed_connection
def get_war_participant_candidates(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """All active members with fame > 0 in any race of the season."""
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        """
        SELECT wp.player_tag AS tag,
               MAX(m.current_name) AS name,
               wp.member_id,
               SUM(COALESCE(wp.fame, 0)) AS total_fame
        FROM war_participation wp
        JOIN war_races wr ON wr.war_race_id = wp.war_race_id
        JOIN members m ON m.member_id = wp.member_id
        WHERE wr.season_id = ? AND m.status = 'active'
        GROUP BY wp.player_tag, wp.member_id
        HAVING total_fame > 0
        """,
        (season_id,),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "total_fame": r["total_fame"],
        }
        for r in rows
    ]


# -- season-close detection -------------------------------------------------

@managed_connection
def season_final_section_index(
    season_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(section_index) AS final FROM war_races WHERE season_id = ?",
        (int(season_id),),
    ).fetchone()
    return row["final"] if row and row["final"] is not None else None


@managed_connection
def season_is_complete(
    season_id: int,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """A season is complete once a newer season has appeared in war_races."""
    row = conn.execute(
        "SELECT 1 FROM war_races WHERE season_id > ? LIMIT 1",
        (int(season_id),),
    ).fetchone()
    return row is not None


# -- helpers ----------------------------------------------------------------

def _season_metric_date_bounds(
    conn: sqlite3.Connection,
    season_id: int,
) -> tuple[Optional[str], Optional[str]]:
    """Return (YYYY-MM-DD, YYYY-MM-DD) bounds for member_daily_metrics queries."""
    start, end = _season_bounds(conn, season_id)
    return _cr_time_to_date(start), _cr_time_to_date(end)


def _cr_time_to_iso(value: Optional[str]) -> Optional[str]:
    """Convert CR time (20260301T100000.000Z) to ISO (2026-03-01T10:00:00)."""
    dt = _parse_cr_time(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None
