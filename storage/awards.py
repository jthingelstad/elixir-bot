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
from storage.war_status import LIVE_FINISH_TIME_SENTINEL, _season_bounds, get_current_season_id


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
    "get_victory_lap_candidates",
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

def _section_finish_iso(
    conn: sqlite3.Connection,
    season_id: int,
    section_index: int,
) -> Optional[str]:
    """Return the clan's finish_time for (season, section) as ISO, or None.

    Returns None when the clan never finished the race or the race isn't
    logged yet. None means "no days were played after victory" — every
    battle day counts as required.
    """
    row = conn.execute(
        "SELECT finish_time FROM war_races WHERE season_id = ? AND section_index = ?",
        (int(season_id), int(section_index)),
    ).fetchone()
    if not row:
        return None
    ft = (row["finish_time"] or "").strip()
    if not ft or ft == LIVE_FINISH_TIME_SENTINEL:
        return None
    return _cr_time_to_iso(ft)


def _required_battle_days(
    conn: sqlite3.Connection,
    season_id: int,
    section_index: int,
) -> list[str]:
    """Battle days that started at or before the clan's finish_time.

    A day started before finish_time contributes to clan victory; a day that
    started entirely after finish is a post-victory day and is not required
    for Perfect Week / Iron King.
    """
    finish_iso = _section_finish_iso(conn, season_id, section_index)
    rows = conn.execute(
        """
        SELECT war_day_key, MIN(observed_at) AS day_start_at
        FROM war_participant_snapshots
        WHERE season_id = ? AND section_index = ? AND phase = 'battle'
        GROUP BY war_day_key
        ORDER BY war_day_key
        """,
        (int(season_id), int(section_index)),
    ).fetchall()
    if finish_iso is None:
        return [r["war_day_key"] for r in rows]
    return [r["war_day_key"] for r in rows if r["day_start_at"] <= finish_iso]


def _has_snapshots_for_season(conn: sqlite3.Connection, season_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM war_participant_snapshots "
        "WHERE season_id = ? AND phase = 'battle' LIMIT 1",
        (int(season_id),),
    ).fetchone()
    return row is not None


def _legacy_iron_king_candidates(
    conn: sqlite3.Connection,
    season_id: int,
) -> list[dict]:
    """Reconstruct Iron King from war_participation.decks_used.

    Used for seasons that predate war_participant_snapshots. The per-week
    ceiling is the max decks_used any active member reached that week
    (typically 16 = 4 decks × 4 battle days). A player qualifies if they
    hit the ceiling in every logged week of the season.
    """
    rows = conn.execute(
        """
        WITH week_ceilings AS (
            SELECT wr.war_race_id, wr.section_index,
                   MAX(wp.decks_used) AS ceiling
            FROM war_races wr
            JOIN war_participation wp ON wp.war_race_id = wr.war_race_id
            WHERE wr.season_id = ?
            GROUP BY wr.war_race_id
        ),
        player_weeks AS (
            SELECT wp.player_tag, wp.member_id, wc.ceiling, wp.decks_used
            FROM war_participation wp
            JOIN week_ceilings wc ON wc.war_race_id = wp.war_race_id
        )
        SELECT pw.player_tag AS tag,
               m.current_name AS name,
               pw.member_id,
               COUNT(*) AS weeks_played,
               SUM(CASE WHEN pw.decks_used = pw.ceiling THEN 1 ELSE 0 END) AS perfect_weeks,
               SUM(pw.decks_used) AS total_decks
        FROM player_weeks pw
        JOIN members m ON m.member_id = pw.member_id
        WHERE m.status = 'active'
        GROUP BY pw.player_tag, pw.member_id
        HAVING weeks_played = (SELECT COUNT(*) FROM week_ceilings)
           AND perfect_weeks = weeks_played
        ORDER BY pw.player_tag
        """,
        (int(season_id),),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "days_played": None,
            "perfect_days": None,
            "total_battle_days": r["total_decks"] // 4 if r["total_decks"] else None,
        }
        for r in rows
    ]


def _legacy_perfect_week_candidates(
    conn: sqlite3.Connection,
    season_id: int,
    section_index: int,
) -> list[dict]:
    """Reconstruct Perfect Week from war_participation.decks_used for one week."""
    row = conn.execute(
        """
        SELECT wr.war_race_id, MAX(wp.decks_used) AS ceiling
        FROM war_races wr
        JOIN war_participation wp ON wp.war_race_id = wr.war_race_id
        WHERE wr.season_id = ? AND wr.section_index = ?
        GROUP BY wr.war_race_id
        """,
        (int(season_id), int(section_index)),
    ).fetchone()
    if not row or not row["ceiling"]:
        return []
    ceiling = row["ceiling"]
    rows = conn.execute(
        """
        SELECT wp.player_tag AS tag,
               m.current_name AS name,
               wp.member_id,
               wp.decks_used
        FROM war_participation wp
        JOIN war_races wr ON wr.war_race_id = wp.war_race_id
        JOIN members m ON m.member_id = wp.member_id
        WHERE wr.season_id = ? AND wr.section_index = ?
          AND m.status = 'active'
          AND wp.decks_used = ?
        ORDER BY wp.player_tag
        """,
        (int(season_id), int(section_index), ceiling),
    ).fetchall()
    # A ceiling of 16 means 4 decks × 4 battle days; smaller ceilings imply
    # the clan had a shorter week (colosseum) or the whole clan rested early.
    battle_days = ceiling // 4 if ceiling % 4 == 0 else None
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "section_index": section_index,
            "total_battle_days": battle_days,
        }
        for r in rows
    ]


@managed_connection
def get_iron_king_candidates(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members who used 4/4 decks on every required battle day of the season.

    Players who joined mid-season and therefore missed early battle days are
    excluded naturally — they simply don't have snapshots for those days, so
    their ``days_played`` is less than the season's ``total_battle_days``.

    "Required" battle days exclude days that began after the clan had
    already reached 10000 fame, so stopping to rest after clan victory
    doesn't cost the award.
    """
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    if not _has_snapshots_for_season(conn, season_id):
        return _legacy_iron_king_candidates(conn, season_id)
    section_rows = conn.execute(
        "SELECT DISTINCT section_index FROM war_participant_snapshots "
        "WHERE season_id = ? AND phase = 'battle'",
        (season_id,),
    ).fetchall()
    required_days: list[str] = []
    for row in section_rows:
        required_days.extend(_required_battle_days(conn, season_id, row["section_index"]))
    total_battle_days = len(required_days)
    if total_battle_days == 0:
        return []
    placeholders = ",".join(["?"] * total_battle_days)
    rows = conn.execute(
        f"""
        WITH per_day_peaks AS (
            SELECT player_tag, member_id, war_day_key,
                   MAX(decks_used_today) AS peak_decks
            FROM war_participant_snapshots
            WHERE season_id = ? AND phase = 'battle'
              AND war_day_key IN ({placeholders})
            GROUP BY player_tag, member_id, war_day_key
        )
        SELECT p.player_tag AS tag,
               m.current_name AS name,
               p.member_id,
               COUNT(*) AS days_played,
               SUM(CASE WHEN p.peak_decks = 4 THEN 1 ELSE 0 END) AS perfect_days
        FROM per_day_peaks p
        JOIN members m ON m.member_id = p.member_id
        WHERE m.status = 'active'
        GROUP BY p.player_tag, p.member_id
        HAVING days_played = ? AND perfect_days = ?
        ORDER BY p.player_tag
        """,
        (season_id, *required_days, total_battle_days, total_battle_days),
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
    """Members who used 4/4 decks on every required battle day of a war week.

    "Required" battle days are those that began at or before the clan's
    finish_time; days that started after clan victory (when no further play
    was needed to win) don't count against the award.
    """
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    if not _has_snapshots_for_season(conn, season_id):
        return _legacy_perfect_week_candidates(conn, season_id, section_index)
    required_days = _required_battle_days(conn, season_id, section_index)
    total_days = len(required_days)
    if total_days == 0:
        return []
    placeholders = ",".join(["?"] * total_days)
    rows = conn.execute(
        f"""
        WITH per_day_peaks AS (
            SELECT player_tag, member_id, war_day_key,
                   MAX(decks_used_today) AS peak_decks
            FROM war_participant_snapshots
            WHERE season_id = ? AND section_index = ? AND phase = 'battle'
              AND war_day_key IN ({placeholders})
            GROUP BY player_tag, member_id, war_day_key
        )
        SELECT p.player_tag AS tag,
               m.current_name AS name,
               p.member_id,
               COUNT(*) AS days_played,
               SUM(CASE WHEN p.peak_decks = 4 THEN 1 ELSE 0 END) AS perfect_days
        FROM per_day_peaks p
        JOIN members m ON m.member_id = p.member_id
        WHERE m.status = 'active'
        GROUP BY p.player_tag, p.member_id
        HAVING days_played = ? AND perfect_days = ?
        ORDER BY p.player_tag
        """,
        (season_id, section_index, *required_days, total_days, total_days),
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
def get_victory_lap_candidates(
    season_id: Optional[int],
    section_index: int,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members who used war decks on a battle day after the clan had won.

    Victory Lap celebrates players who kept racking up war battles after the
    clan had already crossed 10000 fame — the bonus laps no one had to run.
    """
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    finish_iso = _section_finish_iso(conn, season_id, section_index)
    if finish_iso is None:
        return []
    day_rows = conn.execute(
        """
        SELECT war_day_key, MIN(observed_at) AS day_start_at
        FROM war_participant_snapshots
        WHERE season_id = ? AND section_index = ? AND phase = 'battle'
        GROUP BY war_day_key
        """,
        (int(season_id), int(section_index)),
    ).fetchall()
    post_victory_days = [r["war_day_key"] for r in day_rows if r["day_start_at"] > finish_iso]
    if not post_victory_days:
        return []
    placeholders = ",".join(["?"] * len(post_victory_days))
    rows = conn.execute(
        f"""
        SELECT wps.player_tag AS tag,
               m.current_name AS name,
               wps.member_id,
               MAX(wps.decks_used_today) AS peak_decks
        FROM war_participant_snapshots wps
        JOIN members m ON m.member_id = wps.member_id
        WHERE wps.season_id = ? AND wps.section_index = ? AND wps.phase = 'battle'
          AND wps.war_day_key IN ({placeholders})
          AND m.status = 'active'
        GROUP BY wps.player_tag, wps.member_id
        HAVING peak_decks > 0
        ORDER BY peak_decks DESC, wps.player_tag
        """,
        (int(season_id), int(section_index), *post_victory_days),
    ).fetchall()
    return [
        {
            "tag": r["tag"],
            "name": r["name"],
            "member_id": r["member_id"],
            "section_index": section_index,
            "post_victory_days": len(post_victory_days),
            "peak_decks": r["peak_decks"],
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
