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
from storage._formatting import callable_name, injection_safe
from storage.war_status import get_current_season_id


def _fold_award_names(rows: list[dict]) -> list[dict]:
    """QA L26: player_name is COALESCE(display_name, current_name); the
    display_name branch is already injection-safe, but the current_name fallback
    is raw. Fold every player_name through callable_name + injection_safe so a
    holder missing a materialized display_name can't leak an unsafe raw name
    (folding an already-safe name is a no-op)."""
    for row in rows:
        name = row.get("player_name")
        if name:
            row["player_name"] = injection_safe(callable_name(name)) or callable_name(name)
    return rows


__all__ = [
    "insert_award",
    "get_member_trophy_case",
    "get_awards_by_season",
    "get_iron_king_candidates",
    "get_season_donation_leaderboard",
    "get_rookie_mvp_candidates",
    "get_season_awards_standings",
    "get_award_races",
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
    return _fold_award_names(_rowdicts(rows))


@managed_connection
def award_leaderboard(
    award_type: Optional[str] = None,
    rank: Optional[int] = None,
    limit: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """All-time award counts per member: member_id (tag), player_tag,
    player_name, count, latest_season_id. Optionally restrict to a placement
    ``rank`` (e.g. only 1st-place awards) and cap the leaderboard to ``limit``
    rows (QA H19: the tool passed rank/limit but this fn didn't accept them)."""
    clauses, params = [], []
    if award_type:
        clauses.append("a.award_type = ?")
        params.append(award_type)
    if rank is not None:
        clauses.append("a.rank = ?")
        params.append(int(rank))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT a.player_tag AS member_id, a.player_tag, COALESCE(p.display_name, p.current_name) AS player_name, "
        "COUNT(*) AS count, MAX(a.season_id) AS latest_season_id "
        "FROM awards a LEFT JOIN players p ON p.player_tag = a.player_tag "
        f"{where} "
        "GROUP BY a.player_tag "
        "ORDER BY count DESC, player_name COLLATE NOCASE"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, tuple(params)).fetchall()
    return _fold_award_names(_rowdicts(rows))


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
    return _fold_award_names(_rowdicts(rows))


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
    limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Top-N points among members playing their FIRST clan-wars season — i.e.
    with NO war_participation in any earlier season (Jamie 2026-07-13: a "rookie"
    is a first-time war participant, not merely a mid-season joiner). Current
    members only; war_participation is live-upserted so the in-progress week is
    included. Ranks are tie-aware (competition ranking: equal points share a
    rank), so callers can say "three tied at 1,600"."""
    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        f"""
        SELECT wp.player_tag AS tag,
               MAX(m.current_name) AS name,
               SUM(COALESCE(wp.fame, 0)) AS total_points,
               COUNT(*) AS races_participated
        FROM war_participation wp
        JOIN players m ON m.player_tag = wp.player_tag
        JOIN clan_memberships cm
          ON cm.player_tag = wp.player_tag AND cm.left_at IS NULL
        WHERE wp.season_id = ? AND {_ACTIVE}
          AND wp.player_tag NOT IN (
              SELECT DISTINCT player_tag FROM war_participation WHERE season_id < ?
          )
        GROUP BY wp.player_tag
        HAVING total_points > 0
        ORDER BY total_points DESC, races_participated DESC
        LIMIT ?
        """,
        (int(season_id), int(season_id), max(1, int(limit))),
    ).fetchall()
    out = [
        {
            "tag": _canon_tag(r["tag"]),
            "name": r["name"],
            "member_id": _canon_tag(r["tag"]),
            "total_points": r["total_points"] or 0,
            "races_participated": r["races_participated"],
        }
        for r in rows
    ]
    _apply_tie_aware_ranks(out, "total_points")
    return out


def _apply_tie_aware_ranks(entries: list[dict], value_key: str) -> None:
    """Assign competition ranks in place (1, 2, 2, 4 …): equal ``value_key``
    share a rank, and each entry gets ``tied`` (bool) + ``tie_count`` so callers
    can phrase ties correctly ("tied for 2nd, three-way at 2,400"). ``entries``
    must already be sorted by ``value_key`` descending."""
    counts: dict = {}
    for e in entries:
        counts[e.get(value_key)] = counts.get(e.get(value_key), 0) + 1
    prev_value = object()
    rank = 0
    for i, e in enumerate(entries):
        v = e.get(value_key)
        if v != prev_value:
            rank = i + 1
            prev_value = v
        e["rank"] = rank
        e["tied"] = counts.get(v, 0) > 1
        e["tie_count"] = counts.get(v, 0)


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
               SUM(COALESCE(wp.fame, 0)) AS total_points
        FROM war_participation wp
        JOIN players m ON m.player_tag = wp.player_tag
        WHERE wp.season_id = ? AND {_ACTIVE}
        GROUP BY wp.player_tag
        HAVING total_points > 0
        ORDER BY total_points DESC
        """,
        (int(season_id),),
    ).fetchall()
    return [
        {"tag": _canon_tag(r["tag"]), "name": r["name"],
         "member_id": _canon_tag(r["tag"]), "total_points": r["total_points"] or 0}
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
            "metric_value": entry.get("total_points"),
            "metric_unit": "points",
            "metadata": {
                "races_participated": entry.get("races_participated"),
                "avg_points": entry.get("avg_points"),
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
            "metric_value": entry.get("total_points"),
            "metric_unit": "points",
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


@managed_connection
def get_award_races(
    season_id: Optional[int] = None,
    war_champ_limit: int = 10,
    rookie_limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """The LIVE, in-progress award competitions, shaped for the awareness read so
    Elixir can hype them mid-season (not just at season close).

    Deliberately richer than get_season_awards_standings:
      - ``war_champ`` / ``rookie_mvp``: the top ~10 (not just the podium) WITH
        points and **tie-aware** ranks, so Elixir can spot up-and-comers who are
        close and speak correctly about ties ("three tied for 2nd at 2,400").
      - ``iron_king``: a PARTICIPATION list (not a podium) — everyone currently
        on the 4/4-every-battle-day track. Any number can earn it; there is no
        winner. Never rank or pick "the" Iron King.
    ``war_champ_leader`` names who currently tops the points race — the free-pass
    is built on this (see the free-pass rotation rule)."""
    from storage.war_analytics import get_war_champ_standings

    season_id = season_id if season_id is not None else get_current_season_id(conn=conn)
    if season_id is None:
        return {"season_id": None, "war_champ": [], "iron_king": [], "rookie_mvp": [], "note": None}

    champ = []
    for entry in get_war_champ_standings(season_id=season_id, conn=conn)[: max(1, war_champ_limit)]:
        champ.append({
            "tag": _canon_tag(entry["tag"]),
            "name": entry.get("name"),
            "points": entry.get("total_points") or 0,
            "races_participated": entry.get("races_participated"),
        })
    _apply_tie_aware_ranks(champ, "points")

    iron_king = [
        {
            "tag": _canon_tag(c["tag"]),
            "name": c.get("name"),
            "perfect_days": c.get("perfect_days"),
            "total_battle_days": c.get("total_battle_days"),
            "on_track": True,
        }
        for c in get_iron_king_candidates(season_id=season_id, conn=conn)
    ]

    rookie = [
        {"tag": r["tag"], "name": r.get("name"), "points": r.get("total_points") or 0,
         "races_participated": r.get("races_participated"), "rank": r.get("rank"),
         "tied": r.get("tied"), "tie_count": r.get("tie_count")}
        for r in get_rookie_mvp_candidates(season_id=season_id, limit=rookie_limit, conn=conn)
    ]

    leader = champ[0] if champ else None
    return {
        "season_id": season_id,
        "war_champ": champ,
        "war_champ_leader": leader,
        "iron_king": iron_king,
        "rookie_mvp": rookie,
        "note": (
            "War Champ = season POINTS race (ranked; the free pass is built on it). "
            "Iron King = PARTICIPATION (4/4 decks every battle day; unranked — anyone "
            "who qualifies earns it, could be many). Rookie MVP = points race among "
            "members in their FIRST war season. Ranks are tie-aware — say 'tied' when "
            "tie_count>1, never invent an order between equal points."
        ),
    }
