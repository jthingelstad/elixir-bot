"""Season awards — the Q5 consumer (docs/reference/v5.1/open-questions.md Q5).

Grants the durable award rows when a season closes. Q5's rule: standings
projections COMPUTE; the awards ledger RECORDS — this module runs once at the
war stream's season death (wired into engine/emitters/war.py close_season)
and writes rows idempotently (schema.md §7.5 UNIQUE constraint).

Semantics ported from the retired heartbeat/_awards.py + storage/awards.py
(git 1b6ef38~1), re-grounded on v5.1 tables:

- war_champ    — podium, ranks 1–3 by cumulative season fame (Q2 erratum)
- free_pass    — exactly ONE row, from war_seasons.free_pass_tag (Q2 rotation)
- iron_king    — perfect attendance every finalized battle day of every
                 section; requires full-season attendance coverage (s133 only
                 has data from week 4 day 2 — the guard skips with a reason)
- donation_champ — top-3 by summed weekly donation peaks (Sunday-robust MAX
                 per ISO week, summed across the season window)
- rookie_mvp   — top-3 season fame among members whose open membership began
                 during the season
- war_participant — every member with season fame > 0; SILENT (rows only —
                 the old engine deliberately never posted these)

The single "season awards" summary intent (volume principle: one post, not
one per award) is raised by grant_season_awards' caller via the payload this
returns; ledger keys follow recognition.md §5 (award:{type}:{season}:{tag}).
"""

from __future__ import annotations

import json

SEASON_WIDE_SECTION = -1  # carried convention (old storage/awards.py)
PODIUM = 3


def _active(conn, tag: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
        (tag,),
    ).fetchone() is not None


def _name(conn, tag: str) -> str | None:
    row = conn.execute(
        "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
    ).fetchone()
    return row[0] if row else None


def _season_date_bounds(conn, season_id: int) -> tuple[str | None, str | None]:
    """[start, end) dates for the season, from war_weeks (fallback war_seasons)."""
    row = conn.execute(
        """SELECT MIN(COALESCE(created_date, finish_time)) AS start,
                  MAX(COALESCE(finish_time, created_date)) AS end
           FROM war_weeks WHERE season_id = ?""",
        (season_id,),
    ).fetchone()
    start, end = (row["start"], row["end"]) if row else (None, None)
    if not start or not end:
        srow = conn.execute(
            "SELECT started_at, ended_at FROM war_seasons WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        if srow:
            start = start or srow["started_at"]
            end = end or srow["ended_at"]
    return (str(start)[:10] if start else None,
            str(end)[:10] if end else None)


def _grant(conn, *, award_type: str, season_id: int, player_tag: str,
           rank: int = 1, metric_value=None, metric_unit=None,
           metadata: dict | None = None, awarded_at: str) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO awards
               (award_type, season_id, section_index, player_tag, rank,
                metric_value, metric_unit, metadata_json, awarded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (award_type, season_id, SEASON_WIDE_SECTION, player_tag, rank,
         metric_value, metric_unit,
         json.dumps(metadata, default=str) if metadata else None, awarded_at),
    )
    if cur.rowcount:
        # recognition.md §5: award grants claim ledger keys (dedup discipline;
        # intentless claims are legitimate — the summary intent covers voice).
        from engine.recognition import ledger

        ledger.claim(
            conn, f"award:{award_type}:{season_id}:{player_tag}", "war",
            [f"season_closed:{season_id}"], 0,
        )
    return bool(cur.rowcount)


def _war_champ_podium(conn, season_id: int, awarded_at: str) -> list[dict]:
    rows = conn.execute(
        """SELECT player_tag, SUM(COALESCE(fame, 0)) AS total_fame,
                  COUNT(*) AS races
           FROM war_participation WHERE season_id = ?
           GROUP BY player_tag HAVING total_fame > 0
           ORDER BY total_fame DESC, player_tag""",
        (season_id,),
    ).fetchall()
    out = []
    rank = 0
    for r in rows:
        if not _active(conn, r["player_tag"]):
            continue
        rank += 1
        if rank > PODIUM:
            break
        granted = _grant(
            conn, award_type="war_champ", season_id=season_id,
            player_tag=r["player_tag"], rank=rank,
            metric_value=r["total_fame"], metric_unit="fame",
            metadata={"races_participated": r["races"],
                      "avg_fame": round(r["total_fame"] / r["races"], 1) if r["races"] else None},
            awarded_at=awarded_at,
        )
        if granted:
            out.append({"rank": rank, "tag": r["player_tag"],
                        "name": _name(conn, r["player_tag"]),
                        "metric_value": r["total_fame"], "metric_unit": "fame"})
    return out


def _free_pass(conn, season_id: int, awarded_at: str) -> list[dict]:
    row = conn.execute(
        "SELECT war_champ_tag, free_pass_tag FROM war_seasons WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    if not row or not row["free_pass_tag"]:
        return []
    tag = row["free_pass_tag"]
    fame = conn.execute(
        "SELECT SUM(COALESCE(fame,0)) FROM war_participation WHERE season_id = ? AND player_tag = ?",
        (season_id, tag),
    ).fetchone()[0]
    rotated = bool(row["war_champ_tag"] and row["war_champ_tag"] != tag)
    granted = _grant(
        conn, award_type="free_pass", season_id=season_id, player_tag=tag,
        rank=1, metric_value=fame, metric_unit="fame",
        metadata={"rotation_applied": rotated,
                  "war_champ_tag": row["war_champ_tag"]},
        awarded_at=awarded_at,
    )
    if granted:
        return [{"rank": 1, "tag": tag, "name": _name(conn, tag),
                 "rotation_applied": rotated}]
    return []


def _iron_king(conn, season_id: int, awarded_at: str) -> tuple[list[dict], str | None]:
    """Perfect attendance every finalized battle day of every section.
    Returns (granted, skip_reason)."""
    sections = [r[0] for r in conn.execute(
        "SELECT DISTINCT section_index FROM war_weeks WHERE season_id = ? ORDER BY 1",
        (season_id,),
    ).fetchall()]
    att_sections = [r[0] for r in conn.execute(
        "SELECT DISTINCT section_index FROM war_attendance_days WHERE season_id = ? ORDER BY 1",
        (season_id,),
    ).fetchall()]
    if not sections or set(att_sections) != set(sections):
        # v5.1 attendance capture began mid-s133 (week 4 day 2) — a partial
        # season cannot judge "perfect every day". Grants normally from s134.
        return [], "insufficient attendance data"
    rows = conn.execute(
        """SELECT player_tag,
                  COUNT(*) AS days,
                  SUM(CASE WHEN decks_used >= decks_available THEN 1 ELSE 0 END) AS perfect,
                  COUNT(DISTINCT section_index) AS sections
           FROM war_attendance_days WHERE season_id = ?
           GROUP BY player_tag""",
        (season_id,),
    ).fetchall()
    total_days = conn.execute(
        """SELECT COUNT(DISTINCT section_index || ':' || war_day_index)
           FROM war_attendance_days WHERE season_id = ?""",
        (season_id,),
    ).fetchone()[0]
    out = []
    for r in rows:
        if (r["days"] == total_days and r["perfect"] == r["days"]
                and r["sections"] == len(sections) and _active(conn, r["player_tag"])):
            if _grant(conn, award_type="iron_king", season_id=season_id,
                      player_tag=r["player_tag"], rank=1,
                      metric_value=total_days, metric_unit="battle_days",
                      metadata={"perfect_days": r["perfect"]},
                      awarded_at=awarded_at):
                out.append({"rank": 1, "tag": r["player_tag"],
                            "name": _name(conn, r["player_tag"]),
                            "metric_value": total_days,
                            "metric_unit": "battle_days"})
    return out, None


def _donation_champs(conn, season_id: int, awarded_at: str) -> list[dict]:
    start, end = _season_date_bounds(conn, season_id)
    if not start or not end:
        return []
    rows = conn.execute(
        """WITH weekly_peaks AS (
               SELECT player_tag, strftime('%Y-%W', metric_date) AS wk,
                      MAX(COALESCE(donations_week, 0)) AS peak
               FROM player_daily_metrics
               WHERE metric_date BETWEEN ? AND ?
               GROUP BY player_tag, wk)
           SELECT player_tag, SUM(peak) AS total
           FROM weekly_peaks GROUP BY player_tag
           HAVING total > 0 ORDER BY total DESC, player_tag""",
        (start, end),
    ).fetchall()
    out = []
    rank = 0
    for r in rows:
        if not _active(conn, r["player_tag"]):
            continue
        rank += 1
        if rank > PODIUM:
            break
        if _grant(conn, award_type="donation_champ", season_id=season_id,
                  player_tag=r["player_tag"], rank=rank,
                  metric_value=r["total"], metric_unit="donations",
                  awarded_at=awarded_at):
            out.append({"rank": rank, "tag": r["player_tag"],
                        "name": _name(conn, r["player_tag"]),
                        "metric_value": r["total"], "metric_unit": "donations"})
    return out


def _rookie_mvps(conn, season_id: int, awarded_at: str) -> list[dict]:
    start, end = _season_date_bounds(conn, season_id)
    if not start or not end:
        return []
    rows = conn.execute(
        """SELECT wp.player_tag, SUM(COALESCE(wp.fame, 0)) AS total_fame,
                  COUNT(*) AS races
           FROM war_participation wp
           JOIN clan_memberships cm ON cm.player_tag = wp.player_tag
                AND cm.left_at IS NULL AND cm.joined_at >= ? AND cm.joined_at < ?
           WHERE wp.season_id = ?
           GROUP BY wp.player_tag HAVING total_fame > 0
           ORDER BY total_fame DESC, races DESC, wp.player_tag""",
        (start, end, season_id),
    ).fetchall()
    out = []
    for i, r in enumerate(rows[:PODIUM]):
        if _grant(conn, award_type="rookie_mvp", season_id=season_id,
                  player_tag=r["player_tag"], rank=i + 1,
                  metric_value=r["total_fame"], metric_unit="fame",
                  metadata={"races_participated": r["races"]},
                  awarded_at=awarded_at):
            out.append({"rank": i + 1, "tag": r["player_tag"],
                        "name": _name(conn, r["player_tag"]),
                        "metric_value": r["total_fame"], "metric_unit": "fame"})
    return out


def _war_participants(conn, season_id: int, awarded_at: str) -> int:
    """Silent accrual — rows only, never posted (carried behavior)."""
    rows = conn.execute(
        """SELECT player_tag, SUM(COALESCE(fame, 0)) AS total_fame
           FROM war_participation WHERE season_id = ?
           GROUP BY player_tag HAVING total_fame > 0""",
        (season_id,),
    ).fetchall()
    n = 0
    for r in rows:
        if _active(conn, r["player_tag"]) and _grant(
            conn, award_type="war_participant", season_id=season_id,
            player_tag=r["player_tag"], rank=1,
            metric_value=r["total_fame"], metric_unit="fame",
            awarded_at=awarded_at,
        ):
            n += 1
    return n


def grant_season_awards(conn, season_id: int, awarded_at: str) -> dict:
    """Grant every award type for a closed season. Idempotent (UNIQUE +
    INSERT OR IGNORE): re-running grants nothing new. Returns counters plus
    the podium payload for the single season-awards summary intent."""
    champ = _war_champ_podium(conn, season_id, awarded_at)
    fp = _free_pass(conn, season_id, awarded_at)
    iron, iron_skip = _iron_king(conn, season_id, awarded_at)
    donations = _donation_champs(conn, season_id, awarded_at)
    rookies = _rookie_mvps(conn, season_id, awarded_at)
    participants = _war_participants(conn, season_id, awarded_at)
    return {
        "season_id": season_id,
        "war_champ": champ,
        "free_pass": fp,
        "iron_kings": iron,
        "iron_king_skipped": iron_skip,
        "donation_champs": donations,
        "rookie_mvps": rookies,
        "war_participants": participants,
        "granted": len(champ) + len(fp) + len(iron) + len(donations)
        + len(rookies) + participants,
    }
