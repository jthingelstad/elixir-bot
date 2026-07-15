"""One authoritative War Champ and free-pass calculation.

Live award-race reads, season close, and durable award grants all consume this
module so eligibility, donation tiebreaking, and rotation cannot drift between
the preview members see and the outcome the engine records.
"""

from __future__ import annotations

from engine.normalize import parse_cr_time


def _season_date_bounds(conn, season_id: int) -> tuple[str | None, str | None]:
    first = conn.execute(
        "SELECT COALESCE(created_date, finish_time) AS boundary FROM war_weeks "
        "WHERE season_id = ? AND COALESCE(created_date, finish_time) IS NOT NULL "
        "ORDER BY section_index ASC LIMIT 1",
        (int(season_id),),
    ).fetchone()
    last = conn.execute(
        "SELECT COALESCE(finish_time, created_date) AS boundary FROM war_weeks "
        "WHERE season_id = ? AND COALESCE(finish_time, created_date) IS NOT NULL "
        "ORDER BY section_index DESC LIMIT 1",
        (int(season_id),),
    ).fetchone()
    start = first["boundary"] if first else None
    end = last["boundary"] if last else None
    if not start or not end:
        row = conn.execute(
            "SELECT started_at, ended_at FROM war_seasons WHERE season_id = ?",
            (int(season_id),),
        ).fetchone()
        if row:
            start = start or row["started_at"]
            end = end or row["ended_at"]
    start_dt, end_dt = parse_cr_time(start), parse_cr_time(end)
    return (
        start_dt.strftime("%Y-%m-%d") if start_dt else None,
        end_dt.strftime("%Y-%m-%d") if end_dt else None,
    )


def season_donation_totals(conn, season_id: int) -> dict[str, int]:
    """Sum each member's weekly donation peak across the season window."""
    start, end = _season_date_bounds(conn, season_id)
    if not start or not end:
        return {}
    rows = conn.execute(
        """WITH weekly_peaks AS (
               SELECT player_tag, strftime('%Y-%W', metric_date) AS week_key,
                      MAX(COALESCE(donations_week, 0)) AS peak
                 FROM player_daily_metrics
                WHERE metric_date BETWEEN ? AND ?
                GROUP BY player_tag, week_key
           )
           SELECT player_tag, SUM(peak) AS total
             FROM weekly_peaks
            GROUP BY player_tag""",
        (start, end),
    ).fetchall()
    return {row["player_tag"]: int(row["total"] or 0) for row in rows}


def _assign_ranks(standings: list[dict], value_key: str) -> None:
    counts: dict[int, int] = {}
    for entry in standings:
        value = entry[value_key]
        counts[value] = counts.get(value, 0) + 1
    seen = 0
    previous = None
    rank = 0
    for official_rank, entry in enumerate(standings, start=1):
        value = entry[value_key]
        if value != previous:
            rank = seen + 1
        entry["official_rank"] = official_rank
        entry["rank"] = rank
        entry["tie_count"] = counts[value]
        entry["tied"] = counts[value] > 1
        seen += 1
        previous = value


def compute_season_award_outcome(conn, season_id: int) -> dict:
    """Compute the active-member War Champ race and free-pass outcome.

    Ordering is season points, then cards donated, then immutable player tag.
    Equal points remain a display tie; ``official_rank`` captures the donation
    tiebreak order used for the podium, War Champ, and free pass.
    """
    donations = season_donation_totals(conn, season_id)
    rows = conn.execute(
        """SELECT wp.player_tag AS tag,
                  COALESCE(p.display_name, p.current_name) AS name,
                  SUM(COALESCE(wp.fame, 0)) AS points,
                  COUNT(*) AS races_participated,
                  SUM(COALESCE(wp.decks_used, 0)) AS decks_used
             FROM war_participation wp
             JOIN players p ON p.player_tag = wp.player_tag
            WHERE wp.season_id = ?
              AND EXISTS (
                    SELECT 1 FROM clan_memberships cm
                     WHERE cm.player_tag = wp.player_tag AND cm.left_at IS NULL
              )
            GROUP BY wp.player_tag
           HAVING points > 0""",
        (int(season_id),),
    ).fetchall()
    standings = [
        {
            "tag": row["tag"],
            "name": row["name"],
            "points": int(row["points"] or 0),
            "donations": donations.get(row["tag"], 0),
            "races_participated": int(row["races_participated"] or 0),
            "decks_used": int(row["decks_used"] or 0),
        }
        for row in rows
    ]
    standings.sort(key=lambda entry: (-entry["points"], -entry["donations"], entry["tag"]))
    _assign_ranks(standings, "points")

    active_rows = conn.execute(
        """SELECT p.player_tag AS tag,
                  COALESCE(p.display_name, p.current_name) AS name
             FROM players p
            WHERE EXISTS (
                  SELECT 1 FROM clan_memberships cm
                   WHERE cm.player_tag = p.player_tag AND cm.left_at IS NULL
            )"""
    ).fetchall()
    active_names = {row["tag"]: row["name"] for row in active_rows}

    donation_champs = [
        {
            "tag": tag,
            "name": active_names[tag],
            "total_donations": total,
        }
        for tag, total in donations.items()
        if total > 0 and tag in active_names
    ]
    donation_champs.sort(
        key=lambda entry: (-entry["total_donations"], entry["tag"])
    )
    _assign_ranks(donation_champs, "total_donations")

    rookie_rows = conn.execute(
        """SELECT wp.player_tag AS tag,
                  COALESCE(p.display_name, p.current_name) AS name,
                  SUM(COALESCE(wp.fame, 0)) AS total_points,
                  COUNT(*) AS races_participated
             FROM war_participation wp
             JOIN players p ON p.player_tag = wp.player_tag
            WHERE wp.season_id = ?
              AND EXISTS (
                    SELECT 1 FROM clan_memberships cm
                     WHERE cm.player_tag = wp.player_tag AND cm.left_at IS NULL
              )
              AND NOT EXISTS (
                    SELECT 1 FROM war_participation prior
                     WHERE prior.player_tag = wp.player_tag
                       AND prior.season_id < ?
              )
            GROUP BY wp.player_tag
           HAVING total_points > 0""",
        (int(season_id), int(season_id)),
    ).fetchall()
    rookie_mvps = [
        {
            "tag": row["tag"],
            "name": row["name"],
            "total_points": int(row["total_points"] or 0),
            "races_participated": int(row["races_participated"] or 0),
        }
        for row in rookie_rows
    ]
    rookie_mvps.sort(
        key=lambda entry: (
            -entry["total_points"],
            -entry["races_participated"],
            entry["tag"],
        )
    )
    _assign_ranks(rookie_mvps, "total_points")

    prior = conn.execute(
        """SELECT s.season_id, s.free_pass_tag AS tag,
                  COALESCE(p.display_name, p.current_name) AS name
             FROM war_seasons s
             LEFT JOIN players p ON p.player_tag = s.free_pass_tag
            WHERE s.season_id < ? AND s.free_pass_tag IS NOT NULL
            ORDER BY s.season_id DESC LIMIT 1""",
        (int(season_id),),
    ).fetchone()
    last_free_pass = (
        {"season_id": int(prior["season_id"]), "tag": prior["tag"], "name": prior["name"]}
        if prior else None
    )
    champion = standings[0] if standings else None
    prior_tag = last_free_pass["tag"] if last_free_pass else None
    free_pass = next((entry for entry in standings if entry["tag"] != prior_tag), champion)
    return {
        "season_id": int(season_id),
        "standings": standings,
        "war_champ": champion,
        "war_champ_tag": champion["tag"] if champion else None,
        "last_free_pass": last_free_pass,
        "free_pass": free_pass,
        "free_pass_tag": free_pass["tag"] if free_pass else None,
        "rotation_applied": bool(champion and free_pass and champion["tag"] != free_pass["tag"]),
        "donation_champs": donation_champs,
        "rookie_mvps": rookie_mvps,
        # Every active participant with points receives the silent participation
        # row; this is the same eligibility set as the War Champ standings.
        "war_participants": standings,
    }


__all__ = ["compute_season_award_outcome", "season_donation_totals"]
