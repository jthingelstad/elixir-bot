"""Awards read/write layer — v5.1 (docs/v5.1/schema.md §7.5, Q5).

Tag-keyed throughout (§7: awards.member_id dropped). Standings compute over
war_participation (live-upserted by the engine, so the in-progress week is
included); the awards ledger records at season close (the engine's Q5 pass
consumes season_closed and calls insert_award). war_champ is a PODIUM (ranks
1–3, carried behavior); free_pass is the Q2/C5 rotation ledger row.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import (
    _canon_tag,
    _json_or_none,
    _parse_cr_time,
    _rowdicts,
    _utcnow,
    managed_connection,
)
from storage.war_status import get_current_season_id

__all__ = [
    "insert_award",
    "get_member_trophy_case",
    "get_awards_by_season",
    "get_iron_king_candidates",
    "get_season_donation_leaderboard",
    "get_rookie_mvp_candidates",
    "get_season_awards_standings",
    "get_war_participant_candidates",
    "list_awards",
    "award_leaderboard",
    "season_final_section_index",
    "season_is_complete",
]

_ACTIVE = (
    "EXISTS (SELECT 1 FROM clan_memberships cm "
    "WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
)


# -- grant writer -----------------------------------------------------------

@managed_connection
def insert_award(
    award_type: str,
    season_id: int,
    player_tag: str,
    rank: int = 1,
    section_index: int = -1,
    metric_value: Optional[float] = None,
    metric_unit: Optional[str] = None,
    metadata: Optional[dict] = None,
    member_id=None,  # accepted-and-ignored (v5.1: the tag is the key)
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    del member_id
    cur = conn.execute(
        "INSERT INTO awards (award_type, season_id, section_index, player_tag, rank, "
        "metric_value, metric_unit, metadata_json, awarded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(award_type, season_id, section_index, player_tag) DO NOTHING",
        (
            award_type,
            int(season_id),
            int(section_index),
            _canon_tag(player_tag),
            int(rank),
            metric_value,
            metric_unit,
            _json_or_none(metadata or {}),
            _utcnow(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def _row_to_award(row) -> dict:
    item = dict(row)
    item.setdefault("member_id", item.get("player_tag"))
    return item


@managed_connection
def get_member_trophy_case(tag_or_id, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    tag = _canon_tag(str(tag_or_id))
    rows = conn.execute(
        "SELECT award_id, award_type, season_id, section_index, player_tag, rank, "
        "metric_value, metric_unit, metadata_json, awarded_at "
        "FROM awards WHERE player_tag = ? "
        "ORDER BY season_id DESC, award_type ASC, rank ASC",
        (tag,),
    ).fetchall()
    return [_row_to_award(r) for r in rows]


@managed_connection
def list_awards(award_type: Optional[str] = None, season_id: Optional[int] = None,
                rank: Optional[int] = None, member_tag: Optional[str] = None,
                limit: int = 50, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    where = ["1=1"]
    params: list = []
    if rank is not None:
        where.append("a.rank = ?")
        params.append(int(rank))
    if member_tag:
        where.append("a.player_tag = ?")
        params.append(_canon_tag(member_tag))
    if award_type:
        where.append("a.award_type = ?")
        params.append(award_type)
    if season_id is not None:
        where.append("a.season_id = ?")
        params.append(int(season_id))
    rows = conn.execute(
        "SELECT a.award_id, a.award_type, a.season_id, a.section_index, "
        "a.player_tag AS member_id, a.player_tag, a.rank, a.metric_value, a.metric_unit, "
        "a.metadata_json, a.awarded_at, COALESCE(p.display_name, p.current_name) AS player_name "
        "FROM awards a LEFT JOIN players p ON p.player_tag = a.player_tag "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY a.season_id DESC, a.award_type ASC, a.rank ASC LIMIT ?",
        (*params, max(1, int(limit))),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def award_leaderboard(award_type: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """All-time counts per member: member_id (tag), player_tag, player_name,
    count, latest_season_id."""
    where = "WHERE a.award_type = ?" if award_type else ""
    params = (award_type,) if award_type else ()
    rows = conn.execute(
        "SELECT a.player_tag AS member_id, a.player_tag, COALESCE(p.display_name, p.current_name) AS player_name, "
        "COUNT(*) AS count, MAX(a.season_id) AS latest_season_id "
        "FROM awards a LEFT JOIN players p ON p.player_tag = a.player_tag "
        f"{where} "
        "GROUP BY a.player_tag "
        "ORDER BY count DESC, player_name COLLATE NOCASE",
        params,
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_awards_by_season(season_id: int, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT a.award_id, a.award_type, a.season_id, a.section_index, "
        "a.player_tag AS member_id, a.player_tag, a.rank, a.metric_value, a.metric_unit, "
        "a.metadata_json, a.awarded_at, COALESCE(p.display_name, p.current_name) AS player_name "
        "FROM awards a LEFT JOIN players p ON p.player_tag = a.player_tag "
        "WHERE a.season_id = ? "
        "ORDER BY a.award_type ASC, a.rank ASC",
        (int(season_id),),
    ).fetchall()
    return _rowdicts(rows)


# -- season helpers ----------------------------------------------------------

def _cr_time_to_date(value: Optional[str]) -> Optional[str]:
    dt = _parse_cr_time(value)
    return dt.strftime("%Y-%m-%d") if dt else None


def _cr_time_to_iso(value: Optional[str]) -> Optional[str]:
    dt = _parse_cr_time(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def _season_bounds(conn, season_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(created_date) AS start_date, MAX(COALESCE(finish_time, created_date)) AS end_date "
        "FROM war_weeks WHERE season_id = ?",
        (int(season_id),),
    ).fetchone()
    if not row or not row["start_date"]:
        return None, None
    return row["start_date"], row["end_date"]


def _season_metric_date_bounds(conn, season_id: int) -> tuple[Optional[str], Optional[str]]:
    start, end = _season_bounds(conn, season_id)
    return _cr_time_to_date(start), _cr_time_to_date(end)


@managed_connection
def season_final_section_index(season_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(section_index) AS final FROM war_weeks WHERE season_id = ?",
        (int(season_id),),
    ).fetchone()
    return row["final"] if row and row["final"] is not None else None


@managed_connection
def season_is_complete(season_id: int, conn: Optional[sqlite3.Connection] = None) -> bool:
    """A season is complete when its war_seasons row has ended (the engine
    writes ended_at at the season_closed event, §16.1)."""
    row = conn.execute(
        "SELECT 1 FROM war_seasons WHERE season_id = ? AND ended_at IS NOT NULL",
        (int(season_id),),
    ).fetchone()
    return row is not None


# -- candidates / standings ---------------------------------------------------

@managed_connection
def get_iron_king_candidates(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members perfect on every finalized battle day of the season
    (war_attendance_days: decks_used >= decks_available on every tracked day),
    falling back to the participation-ceiling rule when no attendance rows
    exist (warm-up seasons — migration.md T9)."""
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    total_days = conn.execute(
        "SELECT COUNT(DISTINCT section_index || ':' || war_day_index) AS days "
        "FROM war_attendance_days WHERE season_id = ?",
        (int(season_id),),
    ).fetchone()["days"]
    if total_days:
        rows = conn.execute(
            "SELECT wad.player_tag AS tag, MAX(m.current_name) AS name, "
            "COUNT(*) AS days_tracked, "
            "SUM(CASE WHEN wad.decks_used >= wad.decks_available THEN 1 ELSE 0 END) AS perfect_days "
            "FROM war_attendance_days wad "
            "JOIN players m ON m.player_tag = wad.player_tag "
            f"WHERE wad.season_id = ? AND {_ACTIVE} "
            "GROUP BY wad.player_tag "
            "HAVING days_tracked = ? AND perfect_days = ?",
            (int(season_id), total_days, total_days),
        ).fetchall()
        return [
            {"tag": r["tag"], "name": r["name"], "member_id": r["tag"],
             "total_battle_days": total_days, "perfect_days": r["perfect_days"]}
            for r in rows
        ]
    # Fallback: per-section ceiling rule over war_participation.
    sections = [r["section_index"] for r in conn.execute(
        "SELECT DISTINCT section_index FROM war_weeks WHERE season_id = ? ORDER BY section_index",
        (int(season_id),),
    ).fetchall()]
    if not sections:
        return []
    qualifying: Optional[set[str]] = None
    details: dict[str, str] = {}
    total_battle_days = 0
    for section in sections:
        ceiling_row = conn.execute(
            "SELECT MAX(decks_used) AS ceiling FROM war_participation "
            "WHERE season_id = ? AND section_index = ?",
            (int(season_id), section),
        ).fetchone()
        if not ceiling_row or not ceiling_row["ceiling"]:
            return []
        ceiling = ceiling_row["ceiling"]
        total_battle_days += ceiling // 4 if ceiling % 4 == 0 else 0
        rows = conn.execute(
            "SELECT wp.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name "
            "FROM war_participation wp JOIN players m ON m.player_tag = wp.player_tag "
            f"WHERE wp.season_id = ? AND wp.section_index = ? AND {_ACTIVE} "
            "AND wp.decks_used = ?",
            (int(season_id), section, ceiling),
        ).fetchall()
        tags = set()
        for r in rows:
            details[r["tag"]] = r["name"]
            tags.add(r["tag"])
        qualifying = tags if qualifying is None else qualifying & tags
        if not qualifying:
            return []
    return [
        {"tag": t, "name": details[t], "member_id": t,
         "total_battle_days": total_battle_days or None}
        for t in sorted(qualifying)
    ]


def _season_donation_rows(
    conn: sqlite3.Connection,
    season_id: int,
    *,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    start, end = _season_metric_date_bounds(conn, season_id)
    if not start or not end:
        return []
    params: list = [start, end]
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT ?"
        params.append(int(limit))
    return conn.execute(
        f"""
        WITH weekly_peaks AS (
            SELECT d.player_tag,
                   strftime('%Y-%W', d.metric_date) AS iso_week,
                   MAX(COALESCE(d.donations_week, 0)) AS week_peak
            FROM player_daily_metrics d
            WHERE d.metric_date BETWEEN ? AND ?
            GROUP BY d.player_tag, iso_week
        )
        SELECT m.player_tag AS tag,
               COALESCE(m.display_name, m.current_name) AS name,
               m.player_tag AS member_id,
               SUM(wp.week_peak) AS total_donations
        FROM weekly_peaks wp
        JOIN players m ON m.player_tag = wp.player_tag
        WHERE {_ACTIVE}
        GROUP BY wp.player_tag
        HAVING total_donations > 0
        ORDER BY total_donations DESC, m.current_name COLLATE NOCASE
        {limit_clause}
        """,
        tuple(params),
    ).fetchall()


@managed_connection
def get_season_donation_leaderboard(
    season_id: Optional[int] = None,
    limit: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = _season_donation_rows(conn, season_id, limit=limit)
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
    """Top-N fame among members whose current membership began during the
    season (war_participation is live-upserted, so the in-progress week is
    already included)."""
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    start, end = _season_bounds(conn, season_id)
    if not start or not end:
        return []
    rows = conn.execute(
        f"""
        SELECT wp.player_tag AS tag,
               MAX(m.current_name) AS name,
               SUM(COALESCE(wp.fame, 0)) AS total_fame,
               COUNT(*) AS races_participated
        FROM war_participation wp
        JOIN players m ON m.player_tag = wp.player_tag
        JOIN clan_memberships cm
          ON cm.player_tag = wp.player_tag
         AND cm.left_at IS NULL
         AND cm.joined_at >= ?
         AND cm.joined_at < ?
        WHERE wp.season_id = ? AND {_ACTIVE}
        GROUP BY wp.player_tag
        HAVING total_fame > 0
        ORDER BY total_fame DESC, races_participated DESC
        LIMIT ?
        """,
        (_cr_time_to_iso(start) or start, _cr_time_to_iso(end) or end, int(season_id), max(1, int(limit))),
    ).fetchall()
    return [
        {
            "tag": _canon_tag(r["tag"]),
            "name": r["name"],
            "member_id": _canon_tag(r["tag"]),
            "total_fame": r["total_fame"] or 0,
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
    """Every member with any season fame — the silent war_participant accrual
    (Q5: no post, rows only)."""
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        f"""
        SELECT wp.player_tag AS tag, MAX(m.current_name) AS name,
               SUM(COALESCE(wp.fame, 0)) AS total_fame
        FROM war_participation wp
        JOIN players m ON m.player_tag = wp.player_tag
        WHERE wp.season_id = ? AND {_ACTIVE}
        GROUP BY wp.player_tag
        HAVING total_fame > 0
        ORDER BY total_fame DESC
        """,
        (int(season_id),),
    ).fetchall()
    return [
        {"tag": _canon_tag(r["tag"]), "name": r["name"],
         "member_id": _canon_tag(r["tag"]), "total_fame": r["total_fame"] or 0}
        for r in rows
    ]


@managed_connection
def get_season_awards_standings(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Current standings for the season awards in signal-payload shape."""
    from storage.war_analytics import get_war_champ_standings

    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    empty = {
        "season_id": season_id,
        "war_champ": [],
        "iron_kings": [],
        "donation_champs": [],
        "rookie_mvps": [],
    }
    if season_id is None:
        return empty

    war_champ = []
    for i, entry in enumerate(get_war_champ_standings(season_id=season_id, conn=conn)[:3]):
        war_champ.append({
            "rank": i + 1,
            "tag": entry["tag"],
            "name": entry.get("name"),
            "metric_value": entry.get("total_fame"),
            "metric_unit": "fame",
            "metadata": {
                "races_participated": entry.get("races_participated"),
                "avg_fame": entry.get("avg_fame"),
            },
        })

    iron_kings = []
    for c in get_iron_king_candidates(season_id=season_id, conn=conn):
        iron_kings.append({
            "rank": 1,
            "tag": c["tag"],
            "name": c.get("name"),
            "metric_value": c.get("total_battle_days"),
            "metric_unit": "battle_days",
            "metadata": {
                "perfect_days": c.get("perfect_days"),
                "total_battle_days": c.get("total_battle_days"),
            },
        })

    donation_champs = []
    for entry in get_season_donation_leaderboard(season_id=season_id, conn=conn):
        donation_champs.append({
            "rank": entry["rank"],
            "tag": entry["tag"],
            "name": entry.get("name"),
            "metric_value": entry.get("total_donations"),
            "metric_unit": "donations",
            "metadata": {},
        })

    rookie_mvps = []
    for entry in get_rookie_mvp_candidates(season_id=season_id, conn=conn):
        rookie_mvps.append({
            "rank": entry["rank"],
            "tag": entry["tag"],
            "name": entry.get("name"),
            "metric_value": entry.get("total_fame"),
            "metric_unit": "fame",
            "metadata": {
                "races_participated": entry.get("races_participated"),
            },
        })

    return {
        "season_id": season_id,
        "war_champ": war_champ,
        "iron_kings": iron_kings,
        "donation_champs": donation_champs,
        "rookie_mvps": rookie_mvps,
    }
