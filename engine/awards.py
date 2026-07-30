"""Season awards — the Q5 consumer (docs/reference/v5.1/open-questions.md Q5).

Grants the durable award rows when a season closes. Q5's rule: standings
projections COMPUTE; the awards ledger RECORDS — this module runs once at the
war stream's season death (wired into engine/emitters/war.py close_season)
and writes rows idempotently (schema.md §7.5 UNIQUE constraint).

Semantics ported from the retired heartbeat/_awards.py + storage/awards.py
(git 1b6ef38~1), re-grounded on v5.1 tables:

- war_champ    — podium, ranks 1–3 by cumulative season points (Q2 erratum)
- free_pass    — exactly ONE row, from war_seasons.free_pass_tag (Q2 rotation)
- iron_king    — perfect attendance every finalized battle day of every
                 section; requires full-season attendance coverage (s133 only
                 has data from week 4 day 2 — the guard skips with a reason)
- donation_champ — top-3 by summed weekly donation peaks (Sunday-robust MAX
                 per ISO week, summed across the season window)
- rookie_mvp   — top-3 season points among members in their first war season
- war_participant — every member with season points > 0; SILENT (rows only —
                 the old engine deliberately never posted these)

The awareness brain reads the durable rows plus the season_closed event and
owns all member-facing narration. Ledger keys remain durable award-grant
claims (award:{type}:{season}:{tag}); no legacy summary intent is raised.
"""

from __future__ import annotations

import json

SEASON_WIDE_SECTION = -1  # carried convention (old storage/awards.py)
PODIUM = 3


def _active(conn, tag: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
            (tag,),
        ).fetchone()
        is not None
    )


def _name(conn, tag: str) -> str | None:
    row = conn.execute("SELECT current_name FROM players WHERE player_tag = ?", (tag,)).fetchone()
    return row[0] if row else None


def _grant(
    conn,
    *,
    award_type: str,
    season_id: int,
    player_tag: str,
    rank: int = 1,
    metric_value=None,
    metric_unit=None,
    metadata: dict | None = None,
    awarded_at: str,
) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO awards
               (award_type, season_id, section_index, player_tag, rank,
                metric_value, metric_unit, metadata_json, awarded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            award_type,
            season_id,
            SEASON_WIDE_SECTION,
            player_tag,
            rank,
            metric_value,
            metric_unit,
            json.dumps(metadata, default=str) if metadata else None,
            awarded_at,
        ),
    )
    # Idempotency is the UNIQUE(award_type, season_id, section_index, player_tag)
    # constraint above, paired with INSERT OR IGNORE — not the recognition
    # ledger. The ledger claim that used to fire here was intentless by design
    # ("awareness owns narration"): it existed only so the deterministic
    # recognizer would not also announce an award awareness already owns. That
    # recognizer is retired (#207), so the claim guarded nothing and nothing read
    # it back.
    return bool(cur.rowcount)


def _war_champ_podium(conn, season_id: int, awarded_at: str, outcome: dict) -> list[dict]:
    rows = outcome["standings"][:PODIUM]
    out = []
    for r in rows:
        rank = r["official_rank"]
        granted = _grant(
            conn,
            award_type="war_champ",
            season_id=season_id,
            player_tag=r["tag"],
            rank=rank,
            metric_value=r["points"],
            metric_unit="points",
            metadata={
                "races_participated": r["races_participated"],
                "donations_tiebreak": r["donations"],
                "points_rank": r["rank"],
                "tied_on_points": r["tied"],
                "avg_points": round(r["points"] / r["races_participated"], 1)
                if r["races_participated"]
                else None,
            },
            awarded_at=awarded_at,
        )
        if granted:
            out.append(
                {
                    "rank": rank,
                    "tag": r["tag"],
                    "name": r["name"],
                    "metric_value": r["points"],
                    "metric_unit": "points",
                }
            )
    return out


def _free_pass(conn, season_id: int, awarded_at: str, outcome: dict) -> list[dict]:
    selected = outcome["free_pass"]
    if not selected:
        return []
    tag = selected["tag"]
    points = selected["points"]
    rotated = outcome["rotation_applied"]
    granted = _grant(
        conn,
        award_type="free_pass",
        season_id=season_id,
        player_tag=tag,
        rank=1,
        metric_value=points,
        metric_unit="points",
        metadata={
            "rotation_applied": rotated,
            "war_champ_tag": outcome["war_champ_tag"],
        },
        awarded_at=awarded_at,
    )
    if granted:
        return [
            {
                "rank": 1,
                "tag": tag,
                "name": _name(conn, tag),
                "rotation_applied": rotated,
            }
        ]
    return []


def perfect_attendance(
    conn,
    season_id: int,
    *,
    exclude_day: tuple[int, int] | None = None,
    require_full_season: bool = True,
) -> tuple[list[dict], int, str | None]:
    """THE Iron King rule. Returns (rows, total_days, skip_reason).

    One definition, because there used to be three and they disagreed. The
    season-close grant required perfection across every war day of the season;
    an award-race query dropped the per-section check; and
    ``get_perfect_war_participants`` used ``perfect_days = battle_days`` — a
    PER-PLAYER denominator, so a member who played one day perfectly qualified.
    That last one fed the ``perfect_attendance`` agent tool, so the bot told
    members one thing while the grant did another.

    The denominator is the CLAN's finalized battle days, never the player's.

    Two knobs, both about WHICH DAYS COUNT — never about how the rule reads
    them, which is the whole point of having one function:

    ``exclude_day`` drops one ``(section_index, war_day_index)``, used by
    in-season views for the live battle day, because a member who is
    perfect-so-far simply has not finished today's decks yet (QA H10).

    ``require_full_season`` is a grant-time data-sufficiency precondition, not
    part of eligibility. At season close we refuse to judge "perfect every day"
    unless attendance covers every section the season actually had. Mid-season
    that check is meaningless — later weeks have not happened — so in-season
    callers pass False and the section expectation is derived from the days in
    scope instead.

    Rows carry ``player_tag``, ``days``, ``perfect``, ``sections``. Only active
    members are returned. ``skip_reason`` is set when the season cannot be
    judged at all, in which case rows are empty.
    """
    expected_sections: int | None = None
    if require_full_season:
        sections = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT section_index FROM war_weeks WHERE season_id = ? ORDER BY 1",
                (season_id,),
            ).fetchall()
        ]
        att_sections = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT section_index FROM war_attendance_days "
                "WHERE season_id = ? ORDER BY 1",
                (season_id,),
            ).fetchall()
        ]
        if not sections or set(att_sections) != set(sections):
            # v5.1 attendance capture began mid-s133 (week 4 day 2) — a partial
            # season cannot judge "perfect every day". Grants normally from s134.
            return [], 0, "insufficient attendance data"
        expected_sections = len(sections)

    where = ["season_id = ?"]
    params: list = [season_id]
    if exclude_day is not None:
        where.append("NOT (section_index = ? AND war_day_index = ?)")
        params.extend([int(exclude_day[0]), int(exclude_day[1])])
    clause = " AND ".join(where)

    total_days = conn.execute(
        f"""SELECT COUNT(DISTINCT section_index || ':' || war_day_index)
            FROM war_attendance_days WHERE {clause}""",
        tuple(params),
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT player_tag,
                   COUNT(*) AS days,
                   SUM(CASE WHEN decks_used >= decks_available THEN 1 ELSE 0 END) AS perfect,
                   COUNT(DISTINCT section_index) AS sections
            FROM war_attendance_days WHERE {clause}
            GROUP BY player_tag""",
        tuple(params),
    ).fetchall()

    if expected_sections is None:
        expected_sections = conn.execute(
            f"SELECT COUNT(DISTINCT section_index) FROM war_attendance_days WHERE {clause}",
            tuple(params),
        ).fetchone()[0]

    qualified = [
        dict(r)
        for r in rows
        if r["days"] == total_days
        and r["perfect"] == r["days"]
        and r["sections"] == expected_sections
        and _active(conn, r["player_tag"])
    ]
    return qualified, total_days, None


def _iron_king(conn, season_id: int, awarded_at: str) -> tuple[list[dict], str | None]:
    """Perfect attendance every finalized battle day of every section.
    Returns (granted, skip_reason)."""
    rows, total_days, skip_reason = perfect_attendance(conn, season_id)
    if skip_reason:
        return [], skip_reason
    out = []
    for r in rows:
        if _grant(
            conn,
            award_type="iron_king",
            season_id=season_id,
            player_tag=r["player_tag"],
            rank=1,
            metric_value=total_days,
            metric_unit="battle_days",
            metadata={"perfect_days": r["perfect"]},
            awarded_at=awarded_at,
        ):
            out.append(
                {
                    "rank": 1,
                    "tag": r["player_tag"],
                    "name": _name(conn, r["player_tag"]),
                    "metric_value": total_days,
                    "metric_unit": "battle_days",
                }
            )
    return out, None


def _donation_champs(conn, season_id: int, awarded_at: str, outcome: dict) -> list[dict]:
    out = []
    for entry in outcome["donation_champs"][:PODIUM]:
        tag = entry["tag"]
        total = entry["total_donations"]
        rank = entry["official_rank"]
        if _grant(
            conn,
            award_type="donation_champ",
            season_id=season_id,
            player_tag=tag,
            rank=rank,
            metric_value=total,
            metric_unit="donations",
            awarded_at=awarded_at,
        ):
            out.append(
                {
                    "rank": rank,
                    "tag": tag,
                    "name": entry["name"],
                    "metric_value": total,
                    "metric_unit": "donations",
                }
            )
    return out


def _rookie_mvps(conn, season_id: int, awarded_at: str, outcome: dict) -> list[dict]:
    out = []
    for entry in outcome["rookie_mvps"][:PODIUM]:
        rank = entry["official_rank"]
        if _grant(
            conn,
            award_type="rookie_mvp",
            season_id=season_id,
            player_tag=entry["tag"],
            rank=rank,
            metric_value=entry["total_points"],
            metric_unit="points",
            metadata={
                "races_participated": entry["races_participated"],
                "points_rank": entry["rank"],
                "tied_on_points": entry["tied"],
            },
            awarded_at=awarded_at,
        ):
            out.append(
                {
                    "rank": rank,
                    "tag": entry["tag"],
                    "name": entry["name"],
                    "metric_value": entry["total_points"],
                    "metric_unit": "points",
                }
            )
    return out


def _war_participants(conn, season_id: int, awarded_at: str, outcome: dict) -> int:
    """Silent accrual — rows only, never posted (carried behavior)."""
    n = 0
    for entry in outcome["war_participants"]:
        if _grant(
            conn,
            award_type="war_participant",
            season_id=season_id,
            player_tag=entry["tag"],
            rank=1,
            metric_value=entry["points"],
            metric_unit="points",
            awarded_at=awarded_at,
        ):
            n += 1
    return n


def grant_season_awards(
    conn,
    season_id: int,
    awarded_at: str,
    *,
    outcome: dict | None = None,
) -> dict:
    """Grant every award type for a closed season. Idempotent (UNIQUE +
    INSERT OR IGNORE): re-running grants nothing new. Returns counters plus
    the podium payload the awareness read can narrate."""
    if outcome is None:
        from engine.award_outcomes import compute_season_award_outcome

        outcome = compute_season_award_outcome(conn, season_id)
    champ = _war_champ_podium(conn, season_id, awarded_at, outcome)
    fp = _free_pass(conn, season_id, awarded_at, outcome)
    iron, iron_skip = _iron_king(conn, season_id, awarded_at)
    donations = _donation_champs(conn, season_id, awarded_at, outcome)
    rookies = _rookie_mvps(conn, season_id, awarded_at, outcome)
    participants = _war_participants(conn, season_id, awarded_at, outcome)
    return {
        "season_id": season_id,
        "war_champ": champ,
        "free_pass": fp,
        "iron_kings": iron,
        "iron_king_skipped": iron_skip,
        "donation_champs": donations,
        "rookie_mvps": rookies,
        "war_participants": participants,
        "granted": len(champ) + len(fp) + len(iron) + len(donations) + len(rookies) + participants,
    }
