from __future__ import annotations

from datetime import datetime, timedelta

from db import (
    _canon_tag,
    _member_reference_fields,
    _rowdicts,
    chicago_today,
    get_connection,
)


def _cutoff_date(days: int) -> str:
    return (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0))).date().isoformat()


def _member_id_for_tag(conn, tag: str):
    return conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?",
        (_canon_tag(tag),),
    ).fetchone()


def get_member_trophy_history(tag, days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = _cutoff_date(days)
        rows = conn.execute(
            "SELECT dm.metric_date, dm.trophies, dm.best_trophies, dm.clan_rank, dm.exp_level "
            "FROM member_daily_metrics dm "
            "JOIN members m ON m.member_id = dm.member_id "
            "WHERE m.player_tag = ? AND dm.metric_date >= ? "
            "ORDER BY dm.metric_date ASC",
            (_canon_tag(tag), cutoff),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def get_member_daily_battle_summary(tag, days=30, mode_group=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = _cutoff_date(days)
        where = ["m.player_tag = ?", "r.battle_date >= ?"]
        params = [_canon_tag(tag), cutoff]
        if mode_group:
            where.append("r.mode_group = ?")
            params.append(mode_group)
        rows = conn.execute(
            "SELECT r.battle_date, r.mode_group, r.game_mode_id, r.game_mode_name, r.battles, r.wins, r.losses, r.draws, "
            "r.trophy_change_total, r.captured_battles, r.expected_battle_delta, r.completeness_ratio, r.is_complete "
            "FROM member_daily_battle_rollups r "
            "JOIN members m ON m.member_id = r.member_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY r.battle_date ASC, r.mode_group ASC, COALESCE(r.game_mode_id, 0) ASC",
            tuple(params),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def get_clan_member_count_history(days=30, clan_tag=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def get_clan_score_history(days=30, clan_tag=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def get_clan_total_member_trophies_history(days=30, clan_tag=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def get_clan_daily_battle_summary(days=30, clan_tag=None, mode_group=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = _cutoff_date(days)
        where = ["battle_date >= ?"]
        params = [cutoff]
        if clan_tag:
            where.append("clan_tag = ?")
            params.append(_canon_tag(clan_tag))
        if mode_group:
            where.append("mode_group = ?")
            params.append(mode_group)
        rows = conn.execute(
            "SELECT battle_date, clan_tag, clan_name, mode_group, game_mode_id, game_mode_name, members_active, battles, wins, losses, draws, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete "
            f"FROM clan_daily_battle_rollups WHERE {' AND '.join(where)} "
            "ORDER BY battle_date ASC, mode_group ASC, COALESCE(game_mode_id, 0) ASC",
            tuple(params),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


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


def compare_member_trend_windows(tag, window_days=7, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        total_days = max(window_days * 2, 2)
        trophy_history = get_member_trophy_history(tag, days=total_days, conn=conn)
        current_trophies = trophy_history[-window_days:] if window_days else trophy_history
        previous_trophies = trophy_history[-(window_days * 2):-window_days] if window_days else []

        battle_rows = get_member_daily_battle_summary(tag, days=total_days, conn=conn)
        battle_by_day = {}
        for row in battle_rows:
            daily = battle_by_day.setdefault(
                row["battle_date"],
                {"battle_date": row["battle_date"], "battles": 0, "wins": 0, "losses": 0, "draws": 0, "trophy_change_total": 0},
            )
            daily["battles"] += int(row.get("battles") or 0)
            daily["wins"] += int(row.get("wins") or 0)
            daily["losses"] += int(row.get("losses") or 0)
            daily["draws"] += int(row.get("draws") or 0)
            daily["trophy_change_total"] += int(row.get("trophy_change_total") or 0)
        ordered_battles = [battle_by_day[key] for key in sorted(battle_by_day)]
        current_battles = ordered_battles[-window_days:] if window_days else ordered_battles
        previous_battles = ordered_battles[-(window_days * 2):-window_days] if window_days else []

        def _battle_window(rows):
            battles = sum(row["battles"] for row in rows)
            wins = sum(row["wins"] for row in rows)
            losses = sum(row["losses"] for row in rows)
            draws = sum(row["draws"] for row in rows)
            trophy_delta = sum(row["trophy_change_total"] for row in rows)
            win_rate = round(wins / battles, 4) if battles else None
            return {
                "days": len(rows),
                "battles": battles,
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "trophy_change_total": trophy_delta,
                "win_rate": win_rate,
            }

        member_row = conn.execute(
            "SELECT member_id, player_tag AS tag, current_name AS name FROM members WHERE player_tag = ?",
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
                "battle_activity": _battle_window(current_battles),
            },
            "previous": {
                "trophies": _compare_series_window(previous_trophies, "trophies"),
                "battle_activity": _battle_window(previous_battles),
            },
        }
    finally:
        if close:
            conn.close()


def compare_clan_trend_windows(window_days=7, clan_tag=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        total_days = max(window_days * 2, 2)
        counts = get_clan_member_count_history(days=total_days, clan_tag=clan_tag, conn=conn)
        scores = get_clan_score_history(days=total_days, clan_tag=clan_tag, conn=conn)
        trophy_totals = get_clan_total_member_trophies_history(days=total_days, clan_tag=clan_tag, conn=conn)
        battles = get_clan_daily_battle_summary(days=total_days, clan_tag=clan_tag, conn=conn)

        def _split(rows):
            return rows[-window_days:] if window_days else rows, rows[-(window_days * 2):-window_days] if window_days else []

        current_counts, previous_counts = _split(counts)
        current_scores, previous_scores = _split(scores)
        current_trophies, previous_trophies = _split(trophy_totals)

        battle_by_day = {}
        for row in battles:
            daily = battle_by_day.setdefault(
                row["battle_date"],
                {"battle_date": row["battle_date"], "battles": 0, "wins": 0, "losses": 0, "draws": 0, "trophy_change_total": 0},
            )
            daily["battles"] += int(row.get("battles") or 0)
            daily["wins"] += int(row.get("wins") or 0)
            daily["losses"] += int(row.get("losses") or 0)
            daily["draws"] += int(row.get("draws") or 0)
            daily["trophy_change_total"] += int(row.get("trophy_change_total") or 0)
        ordered_battles = [battle_by_day[key] for key in sorted(battle_by_day)]
        current_battles, previous_battles = _split(ordered_battles)

        def _battle_window(rows):
            battles = sum(row["battles"] for row in rows)
            wins = sum(row["wins"] for row in rows)
            losses = sum(row["losses"] for row in rows)
            draws = sum(row["draws"] for row in rows)
            trophy_delta = sum(row["trophy_change_total"] for row in rows)
            win_rate = round(wins / battles, 4) if battles else None
            return {
                "days": len(rows),
                "battles": battles,
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "trophy_change_total": trophy_delta,
                "win_rate": win_rate,
            }

        clan_row = conn.execute(
            "SELECT clan_tag, clan_name FROM clan_daily_metrics "
            + ("WHERE clan_tag = ? " if clan_tag else "")
            + "ORDER BY metric_date DESC, observed_at DESC, metric_id DESC LIMIT 1",
            ((_canon_tag(clan_tag),) if clan_tag else ()),
        ).fetchone()
        clan = dict(clan_row) if clan_row else {"clan_tag": _canon_tag(clan_tag or "#J2RGCRVG"), "clan_name": "POAP KINGS"}

        return {
            "clan": clan,
            "window_days": window_days,
            "current": {
                "member_count": _compare_series_window(current_counts, "member_count"),
                "clan_score": _compare_series_window(current_scores, "clan_score"),
                "total_member_trophies": _compare_series_window(current_trophies, "total_member_trophies"),
                "battle_activity": _battle_window(current_battles),
            },
            "previous": {
                "member_count": _compare_series_window(previous_counts, "member_count"),
                "clan_score": _compare_series_window(previous_scores, "clan_score"),
                "total_member_trophies": _compare_series_window(previous_trophies, "total_member_trophies"),
                "battle_activity": _battle_window(previous_battles),
            },
        }
    finally:
        if close:
            conn.close()


def build_member_trend_summary_context(tag, days=30, window_days=7, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def build_clan_trend_summary_context(days=30, window_days=7, clan_tag=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        counts = get_clan_member_count_history(days=days, clan_tag=clan_tag, conn=conn)
        scores = get_clan_score_history(days=days, clan_tag=clan_tag, conn=conn)
        trophies = get_clan_total_member_trophies_history(days=days, clan_tag=clan_tag, conn=conn)
        battle_rows = get_clan_daily_battle_summary(days=days, clan_tag=clan_tag, conn=conn)
        comparison = compare_clan_trend_windows(window_days=window_days, clan_tag=clan_tag, conn=conn)
        latest_counts = counts[-1] if counts else {}
        latest_scores = scores[-1] if scores else {}
        latest_trophies = trophies[-1] if trophies else {}
        current_battles = comparison["current"]["battle_activity"]
        previous_battles = comparison["previous"]["battle_activity"]
        lines = [
            "=== CLAN TREND SUMMARY ===",
            f"clan: {comparison['clan'].get('clan_name')} ({comparison['clan'].get('clan_tag')})",
            f"window_days: {days}",
            (
                f"latest_snapshot: {latest_counts.get('metric_date') or latest_scores.get('metric_date') or latest_trophies.get('metric_date') or 'n/a'} | "
                f"members {latest_counts.get('member_count')} | clan_score {latest_scores.get('clan_score')} | "
                f"total_member_trophies {latest_trophies.get('total_member_trophies')}"
            ),
            (
                f"current_{window_days}d_vs_previous_{window_days}d: "
                f"member_count {comparison['current']['member_count'].get('delta')} vs {comparison['previous']['member_count'].get('delta')} | "
                f"clan_score {comparison['current']['clan_score'].get('delta')} vs {comparison['previous']['clan_score'].get('delta')} | "
                f"total_member_trophies {comparison['current']['total_member_trophies'].get('delta')} vs {comparison['previous']['total_member_trophies'].get('delta')} | "
                f"battles {current_battles.get('battles')} vs {previous_battles.get('battles')} | "
                f"record {current_battles.get('wins')}-{current_battles.get('losses')}-{current_battles.get('draws')} "
                f"vs {previous_battles.get('wins')}-{previous_battles.get('losses')}-{previous_battles.get('draws')}"
            ),
            f"daily_battle_rows: {len(battle_rows)}",
        ]
        return "\n".join(lines)
    finally:
        if close:
            conn.close()


__all__ = [name for name in globals() if not name.startswith("__")]
