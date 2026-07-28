from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from db import (
    _canon_tag,
    _rowdicts,
    chicago_today,
    managed_connection,
)
from storage._enrichment import _member_reference_fields


def _cutoff_date(days: int) -> str:
    return (
        (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0)))
        .date()
        .isoformat()
    )


def _member_id_for_tag(conn, tag: str):
    return conn.execute(
        "SELECT player_tag AS member_id FROM players WHERE player_tag = ?",
        (_canon_tag(tag),),
    ).fetchone()


@managed_connection
def get_member_trophy_history(
    tag: str, days: int = 30, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    cutoff = _cutoff_date(days)
    rows = conn.execute(
        "SELECT dm.metric_date, dm.trophies, dm.best_trophies, dm.clan_rank "
        "FROM player_daily_metrics dm "
        "JOIN players m ON m.player_tag = dm.player_tag "
        "WHERE m.player_tag = ? AND dm.metric_date >= ? "
        "ORDER BY dm.metric_date ASC",
        (_canon_tag(tag), cutoff),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_member_daily_battle_summary(
    tag: str,
    days: int = 30,
    mode_group: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    cutoff = _cutoff_date(days)
    where = ["m.player_tag = ?", "r.battle_date >= ?"]
    params = [_canon_tag(tag), cutoff]
    if mode_group:
        where.append("r.mode_group = ?")
        params.append(mode_group)
    rows = conn.execute(
        "SELECT r.battle_date, r.mode_group, r.game_mode_id, r.game_mode_name, r.battles, r.wins, r.losses, r.draws, "
        "r.trophy_change_total, r.captured_battles, r.expected_battle_delta, r.completeness_ratio, r.is_complete "
        "FROM player_daily_battle_rollups r "
        "JOIN players m ON m.player_tag = r.player_tag "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY r.battle_date ASC, r.mode_group ASC, COALESCE(r.game_mode_id, 0) ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_clan_member_count_history(
    days: int = 30,
    clan_tag: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    cutoff = _cutoff_date(days)
    where = ["metric_date >= ?"]
    params = [cutoff]
    if clan_tag:
        where.append("clan_tag = ?")
        params.append(_canon_tag(clan_tag))
    rows = conn.execute(
        "SELECT metric_date, clan_tag, clan_name, member_count, open_slots, joins_today, leaves_today, net_member_change "
        f"FROM clan_daily_metrics WHERE {' AND '.join(where)} "
        "ORDER BY metric_date ASC, clan_tag ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_clan_score_history(
    days: int = 30,
    clan_tag: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    cutoff = _cutoff_date(days)
    where = ["metric_date >= ?"]
    params = [cutoff]
    if clan_tag:
        where.append("clan_tag = ?")
        params.append(_canon_tag(clan_tag))
    rows = conn.execute(
        "SELECT metric_date, clan_tag, clan_name, clan_score, clan_war_trophies, required_trophies "
        f"FROM clan_daily_metrics WHERE {' AND '.join(where)} "
        "ORDER BY metric_date ASC, clan_tag ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_clan_total_member_trophies_history(
    days: int = 30,
    clan_tag: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    cutoff = _cutoff_date(days)
    where = ["metric_date >= ?"]
    params = [cutoff]
    if clan_tag:
        where.append("clan_tag = ?")
        params.append(_canon_tag(clan_tag))
    rows = conn.execute(
        "SELECT metric_date, clan_tag, clan_name, total_member_trophies, avg_member_trophies, top_member_trophies "
        f"FROM clan_daily_metrics WHERE {' AND '.join(where)} "
        "ORDER BY metric_date ASC, clan_tag ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


def _compare_series_window(rows, value_key):
    if not rows:
        return {"days": 0, "start": None, "end": None, "delta": 0}
    start = rows[0].get(value_key)
    end = rows[-1].get(value_key)
    if start is None or end is None:
        delta = None
    else:
        delta = end - start
    return {
        "days": len(rows),
        "start": start,
        "end": end,
        "delta": delta,
    }


def _events_battle_window(
    conn, canon_tag: str, start_ymd: str, end_ymd: str, window_days: int
) -> dict:
    """Battle W/L/volume for a member over a calendar window, read from the
    authoritative battle_events store (QA H2/M2: the daily rollups it replaced
    are lossy for new/backfilled members — e.g. a real 27-battle week showed up
    as 2 tracked days / 10 battles). battle_time is UTC-compact
    'YYYYMMDDTHHMMSS...'; compare on the 8-char date prefix. Because
    battle_events is complete, the window coverage is authoritative — we surface
    days_with_battles so a low-activity week reads as low activity, not missing
    data."""
    row = conn.execute(
        "SELECT COUNT(*) AS battles, "
        "SUM(outcome = 'W') AS wins, SUM(outcome = 'L') AS losses, SUM(outcome = 'D') AS draws, "
        "SUM(COALESCE(trophy_change, 0)) AS trophy_delta, "
        "COUNT(DISTINCT substr(battle_time, 1, 8)) AS days_with_battles "
        "FROM battle_events WHERE player_tag = ? "
        "AND substr(battle_time, 1, 8) >= ? AND substr(battle_time, 1, 8) <= ?",
        (canon_tag, start_ymd, end_ymd),
    ).fetchone()
    battles = int(row["battles"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    return {
        "days": int(row["days_with_battles"] or 0),
        "window_days": window_days,
        "battles": battles,
        "wins": wins,
        "losses": losses,
        "draws": int(row["draws"] or 0),
        "trophy_change_total": int(row["trophy_delta"] or 0),
        "win_rate": round(wins / battles, 4) if battles else None,
    }


@managed_connection
def compare_member_trend_windows(
    tag: str, window_days: int = 7, conn: Optional[sqlite3.Connection] = None
) -> dict:
    total_days = max(window_days * 2, 2)
    trophy_history = get_member_trophy_history(tag, days=total_days, conn=conn)
    current_trophies = trophy_history[-window_days:] if window_days else trophy_history
    previous_trophies = trophy_history[-(window_days * 2) : -window_days] if window_days else []

    canon = _canon_tag(tag)
    today = datetime.fromisoformat(chicago_today()).date()
    win = max(window_days, 1)
    cur_start = (today - timedelta(days=win - 1)).strftime("%Y%m%d")
    cur_end = today.strftime("%Y%m%d")
    prev_start = (today - timedelta(days=2 * win - 1)).strftime("%Y%m%d")
    prev_end = (today - timedelta(days=win)).strftime("%Y%m%d")
    current_battle_window = _events_battle_window(conn, canon, cur_start, cur_end, win)
    previous_battle_window = _events_battle_window(conn, canon, prev_start, prev_end, win)

    member_row = conn.execute(
        "SELECT player_tag AS member_id, player_tag AS tag, display_name AS name FROM players WHERE player_tag = ?",
        (_canon_tag(tag),),
    ).fetchone()
    member = dict(member_row) if member_row else {"tag": _canon_tag(tag), "name": _canon_tag(tag)}
    if member_row:
        member = _member_reference_fields(conn, member_row["member_id"], member)

    return {
        "member": member,
        "window_days": window_days,
        "current": {
            "trophies": _compare_series_window(current_trophies, "trophies"),
            "battle_activity": current_battle_window,
        },
        "previous": {
            "trophies": _compare_series_window(previous_trophies, "trophies"),
            "battle_activity": previous_battle_window,
        },
    }


def _events_clan_battle_window(conn, start_ymd: str, end_ymd: str, window_days: int) -> dict:
    """Clan-wide battle activity over a calendar window from authoritative
    battle_events (current members only). QA H1: the clan_daily_battle_rollups
    this replaced were stale (data ended a week behind), so the previous week
    read as 0 battles / 0-0-0 — a false 'dead clan' signal — while battle_events
    held thousands of real battles."""
    row = conn.execute(
        "SELECT COUNT(*) AS battles, "
        "SUM(b.outcome = 'W') AS wins, SUM(b.outcome = 'L') AS losses, SUM(b.outcome = 'D') AS draws, "
        "SUM(COALESCE(b.trophy_change, 0)) AS trophy_delta, "
        "COUNT(DISTINCT b.player_tag) AS active_members "
        "FROM battle_events b "
        "WHERE substr(b.battle_time, 1, 8) >= ? AND substr(b.battle_time, 1, 8) <= ? "
        "AND EXISTS (SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = b.player_tag AND cm.left_at IS NULL)",
        (start_ymd, end_ymd),
    ).fetchone()
    battles = int(row["battles"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "window_days": window_days,
        "battles": battles,
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "draws": int(row["draws"] or 0),
        "trophy_change_total": int(row["trophy_delta"] or 0),
        "active_members": int(row["active_members"] or 0),
        "win_rate": round(wins / battles, 4) if battles else None,
    }


@managed_connection
def compare_clan_trend_windows(
    window_days: int = 7,
    clan_tag: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    total_days = max(window_days * 2, 2)
    counts = get_clan_member_count_history(days=total_days, clan_tag=clan_tag, conn=conn)
    scores = get_clan_score_history(days=total_days, clan_tag=clan_tag, conn=conn)
    trophy_totals = get_clan_total_member_trophies_history(
        days=total_days, clan_tag=clan_tag, conn=conn
    )

    def _split(rows):
        return rows[-window_days:] if window_days else rows, rows[
            -(window_days * 2) : -window_days
        ] if window_days else []

    current_counts, previous_counts = _split(counts)
    current_scores, previous_scores = _split(scores)
    current_trophies, previous_trophies = _split(trophy_totals)

    win = max(window_days, 1)
    today = datetime.fromisoformat(chicago_today()).date()
    cur_start = (today - timedelta(days=win - 1)).strftime("%Y%m%d")
    cur_end = today.strftime("%Y%m%d")
    prev_start = (today - timedelta(days=2 * win - 1)).strftime("%Y%m%d")
    prev_end = (today - timedelta(days=win)).strftime("%Y%m%d")
    current_battle_window = _events_clan_battle_window(conn, cur_start, cur_end, win)
    previous_battle_window = _events_clan_battle_window(conn, prev_start, prev_end, win)

    clan_row = conn.execute(
        "SELECT clan_tag, clan_name FROM clan_daily_metrics "
        + ("WHERE clan_tag = ? " if clan_tag else "")
        + "ORDER BY metric_date DESC, observed_at DESC, metric_id DESC LIMIT 1",
        ((_canon_tag(clan_tag),) if clan_tag else ()),
    ).fetchone()
    clan = (
        dict(clan_row)
        if clan_row
        else {
            "clan_tag": _canon_tag(clan_tag or "#J2RGCRVG"),
            "clan_name": "POAP KINGS",
        }
    )

    return {
        "clan": clan,
        "window_days": window_days,
        "current": {
            "member_count": _compare_series_window(current_counts, "member_count"),
            "clan_score": _compare_series_window(current_scores, "clan_score"),
            "total_member_trophies": _compare_series_window(
                current_trophies, "total_member_trophies"
            ),
            "battle_activity": current_battle_window,
        },
        "previous": {
            "member_count": _compare_series_window(previous_counts, "member_count"),
            "clan_score": _compare_series_window(previous_scores, "clan_score"),
            "total_member_trophies": _compare_series_window(
                previous_trophies, "total_member_trophies"
            ),
            "battle_activity": previous_battle_window,
        },
    }


@managed_connection
def build_member_trend_summary_context(
    tag: str,
    days: int = 30,
    window_days: int = 7,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    history = get_member_trophy_history(tag, days=days, conn=conn)
    battle_summary = get_member_daily_battle_summary(tag, days=days, conn=conn)
    comparison = compare_member_trend_windows(tag, window_days=window_days, conn=conn)
    latest = history[-1] if history else {}
    member = comparison["member"]
    current_battles = comparison["current"]["battle_activity"]
    previous_battles = comparison["previous"]["battle_activity"]
    lines = [
        "=== MEMBER TREND SUMMARY ===",
        f"member: {member.get('member_ref') or member.get('name') or member.get('tag')}",
        f"player_tag: {member.get('tag') or _canon_tag(tag)}",
        f"window_days: {days}",
        f"latest_snapshot: {latest.get('metric_date') or 'n/a'} | trophies {latest.get('trophies')} | best_trophies {latest.get('best_trophies')}",
        (
            f"current_{window_days}d_vs_previous_{window_days}d: "
            f"trophies {comparison['current']['trophies'].get('delta')} vs {comparison['previous']['trophies'].get('delta')} | "
            f"battles {current_battles.get('battles')} vs {previous_battles.get('battles')} | "
            f"record {current_battles.get('wins')}-{current_battles.get('losses')}-{current_battles.get('draws')} "
            f"vs {previous_battles.get('wins')}-{previous_battles.get('losses')}-{previous_battles.get('draws')} | "
            f"battle_trophy_delta {current_battles.get('trophy_change_total')} vs {previous_battles.get('trophy_change_total')}"
        ),
        f"daily_battle_rows: {len(battle_summary)}",
    ]
    return "\n".join(lines)


@managed_connection
def build_clan_trend_summary_context(
    days: int = 30,
    window_days: int = 7,
    clan_tag: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    counts = get_clan_member_count_history(days=days, clan_tag=clan_tag, conn=conn)
    scores = get_clan_score_history(days=days, clan_tag=clan_tag, conn=conn)
    trophies = get_clan_total_member_trophies_history(days=days, clan_tag=clan_tag, conn=conn)
    comparison = compare_clan_trend_windows(window_days=window_days, clan_tag=clan_tag, conn=conn)
    latest_counts = counts[-1] if counts else {}
    latest_scores = scores[-1] if scores else {}
    latest_trophies = trophies[-1] if trophies else {}
    current_battles = comparison["current"]["battle_activity"]
    previous_battles = comparison["previous"]["battle_activity"]
    lines = [
        "=== CLAN TREND SUMMARY ===",
        f"clan: {comparison['clan'].get('clan_name')} ({comparison['clan'].get('clan_tag')})",
        # QA L9: `days` is the HISTORY span the snapshots below cover, NOT the
        # comparison window — that's `window_days` (7d), shown on the
        # current_Nd_vs_previous_Nd line. Naming it window_days here read as if
        # the deltas compared 30-day windows.
        f"history_days: {days} (comparison window below is {window_days}d)",
        (
            f"latest_snapshot: {latest_counts.get('metric_date') or latest_scores.get('metric_date') or latest_trophies.get('metric_date') or 'n/a'} | "
            f"members {latest_counts.get('member_count')} | clan_score {latest_scores.get('clan_score')} | "
            f"total_member_trophies {latest_trophies.get('total_member_trophies')}"
        ),
        (
            f"current_{window_days}d_vs_previous_{window_days}d: "
            f"member_count {comparison['current']['member_count'].get('delta')} vs {comparison['previous']['member_count'].get('delta')} | "
            f"clan_score {comparison['current']['clan_score'].get('delta')} vs {comparison['previous']['clan_score'].get('delta')} | "
            # Labeled precisely: the roster-total delta swallows each joiner's
            # entire trophy count (a 43→47 week read as "pushed 39,865
            # trophies" — live incident 2026-07-04). battle_trophy_delta is
            # the real pushed/lost number.
            f"roster_total_trophies_change {comparison['current']['total_member_trophies'].get('delta')} vs {comparison['previous']['total_member_trophies'].get('delta')} "
            f"(roster sum — includes joins/leaves; NOT trophies pushed) | "
            f"battle_trophy_delta {current_battles.get('trophy_change_total')} vs {previous_battles.get('trophy_change_total')} "
            f"(trophies actually won/lost in battles) | "
            f"battles {current_battles.get('battles')} vs {previous_battles.get('battles')} | "
            f"record {current_battles.get('wins')}-{current_battles.get('losses')}-{current_battles.get('draws')} "
            f"vs {previous_battles.get('wins')}-{previous_battles.get('losses')}-{previous_battles.get('draws')}"
        ),
    ]
    return "\n".join(lines)


__all__ = [name for name in globals() if not name.startswith("__")]
