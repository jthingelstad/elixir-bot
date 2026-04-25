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
    "get_season_donation_leaderboard",
    "get_rookie_mvp_candidates",
    "get_season_awards_standings",
    "get_war_participant_candidates",
    "list_awards",
    "award_leaderboard",
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
def list_awards(
    *,
    award_type: Optional[str] = None,
    season_id: Optional[int] = None,
    rank: Optional[int] = None,
    member_tag: Optional[str] = None,
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Filtered list of award grants with resolved member names.

    Any combination of filters is supported. Ordered newest-season first,
    then by award_type, rank, section_index. Use this for cross-member
    queries ('who won S130 War Champ?', 'list all iron kings this year').
    Prefer get_member_trophy_case when you have a single member_id.
    """
    where = []
    params: list = []
    if award_type:
        where.append("a.award_type = ?")
        params.append(award_type)
    if season_id is not None:
        where.append("a.season_id = ?")
        params.append(int(season_id))
    if rank is not None:
        where.append("a.rank = ?")
        params.append(int(rank))
    if member_tag:
        where.append("a.player_tag = ?")
        params.append(_canon_tag(member_tag))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    rows = conn.execute(
        "SELECT a.award_id, a.award_type, a.season_id, a.section_index, "
        "a.member_id, a.player_tag, a.rank, a.metric_value, a.metric_unit, "
        "a.metadata_json, a.awarded_at, m.current_name AS player_name "
        "FROM awards a JOIN members m ON m.member_id = a.member_id"
        f"{clause} "
        "ORDER BY a.season_id DESC, a.award_type, a.rank, a.section_index "
        "LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_award(r) for r in rows]


@managed_connection
def award_leaderboard(
    *,
    award_type: str,
    rank: int = 1,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Count of award wins per member for a given award_type + rank.

    Answers 'who has won War Champ the most?' style questions. Returns rows
    ordered by count DESC, then most-recent season_id DESC. Each row has
    member_id, player_tag, player_name, count, latest_season_id.
    """
    rows = conn.execute(
        "SELECT a.member_id, a.player_tag, m.current_name AS player_name, "
        "COUNT(*) AS count, MAX(a.season_id) AS latest_season_id "
        "FROM awards a JOIN members m ON m.member_id = a.member_id "
        "WHERE a.award_type = ? AND a.rank = ? "
        "GROUP BY a.member_id "
        "ORDER BY count DESC, latest_season_id DESC "
        "LIMIT ?",
        (award_type, int(rank), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


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


def _has_snapshots_for_section(
    conn: sqlite3.Connection,
    season_id: int,
    section_index: int,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM war_participant_snapshots "
        "WHERE season_id = ? AND section_index = ? AND phase = 'battle' LIMIT 1",
        (int(season_id), int(section_index)),
    ).fetchone()
    return row is not None


def _section_iron_king_member_ids(
    conn: sqlite3.Connection,
    season_id: int,
    section_index: int,
) -> tuple[set[int], int, dict[int, dict]]:
    """Return (member_ids, battle_days, member_details) for one section.

    ``member_ids`` qualify as "perfect in this section" — 4/4 on every
    required battle day, or (for seasons without snapshots) hit the
    war_participation deck ceiling that section. ``member_details`` maps
    member_id → {tag, name}.
    """
    details: dict[int, dict] = {}

    if _has_snapshots_for_section(conn, season_id, section_index):
        required_days = _required_battle_days(conn, season_id, section_index)
        total_days = len(required_days)
        if total_days == 0:
            return set(), 0, details
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
            """,
            (season_id, section_index, *required_days, total_days, total_days),
        ).fetchall()
        member_ids: set[int] = set()
        for r in rows:
            details[r["member_id"]] = {"tag": r["tag"], "name": r["name"]}
            member_ids.add(r["member_id"])
        return member_ids, total_days, details

    # Legacy fallback: no snapshots — reconstruct from war_participation.
    ceiling_row = conn.execute(
        """
        SELECT MAX(wp.decks_used) AS ceiling
        FROM war_races wr
        JOIN war_participation wp ON wp.war_race_id = wr.war_race_id
        WHERE wr.season_id = ? AND wr.section_index = ?
        """,
        (int(season_id), int(section_index)),
    ).fetchone()
    if not ceiling_row or not ceiling_row["ceiling"]:
        return set(), 0, details
    ceiling = ceiling_row["ceiling"]
    rows = conn.execute(
        """
        SELECT wp.player_tag AS tag,
               m.current_name AS name,
               wp.member_id
        FROM war_participation wp
        JOIN war_races wr ON wr.war_race_id = wp.war_race_id
        JOIN members m ON m.member_id = wp.member_id
        WHERE wr.season_id = ? AND wr.section_index = ?
          AND m.status = 'active'
          AND wp.decks_used = ?
        """,
        (int(season_id), int(section_index), ceiling),
    ).fetchall()
    member_ids = set()
    for r in rows:
        details[r["member_id"]] = {"tag": r["tag"], "name": r["name"]}
        member_ids.add(r["member_id"])
    # A ceiling of 16 means 4 decks × 4 battle days; colosseum weeks may be
    # shorter. If ceiling isn't a multiple of 4, surface as None.
    battle_days = ceiling // 4 if ceiling % 4 == 0 else 0
    return member_ids, battle_days, details


@managed_connection
def get_iron_king_candidates(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Members who played 4/4 on every required battle day of every logged week.

    A member qualifies only if they were present and perfect in every
    section of the season (no mid-season joiners). Post-victory battle
    days — those that began after the clan reached 10000 fame — don't
    count toward the requirement.

    Seasons without ``war_participant_snapshots`` fall back to
    ``war_participation.decks_used``: a member qualifies for a section if
    they hit the max decks any member reached that week.
    """
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    section_rows = conn.execute(
        "SELECT DISTINCT section_index FROM war_races "
        "WHERE season_id = ? ORDER BY section_index",
        (int(season_id),),
    ).fetchall()
    sections = [r["section_index"] for r in section_rows]
    if not sections:
        return []

    qualifying: Optional[set[int]] = None
    member_details: dict[int, dict] = {}
    total_battle_days = 0
    for section in sections:
        section_ids, section_days, section_details = _section_iron_king_member_ids(
            conn, season_id, section,
        )
        total_battle_days += section_days
        for mid, det in section_details.items():
            member_details.setdefault(mid, det)
        qualifying = section_ids if qualifying is None else qualifying & section_ids
        if not qualifying:
            return []

    return [
        {
            "tag": member_details[mid]["tag"],
            "name": member_details[mid]["name"],
            "member_id": mid,
            "total_battle_days": total_battle_days or None,
        }
        for mid in sorted(qualifying)
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


# -- unified standings ------------------------------------------------------

@managed_connection
def get_season_awards_standings(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Current standings for all four season awards in signal-payload shape.

    Mid-season callers see who would win if the season ended now; final
    callers (after season-end grant) see the same data the announcement post
    will read. Each entry mirrors the ``season_awards_granted`` payload —
    ``rank``, ``tag``, ``name``, ``metric_value``, ``metric_unit``,
    ``metadata`` — so prompt-side narration stays consistent across the
    Situation block, weekly digest, interactive tools, and the season-end
    post.
    """
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
