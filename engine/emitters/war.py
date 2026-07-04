"""War-stream emitter — events.md §5, architecture.md §16 (the bounded stream).

Diffs the `state_baselines('riverrace')` aspect. Events land in `war_events`;
the emitter also maintains the war tables (war_seasons / war_weeks /
war_week_clans / war_participation / war_attendance_days) so reads are joins,
not inference (§14.5).

Lifecycle (§16.1): a season is born on a new (inferred) seasonId; the end is
DISCOVERED — every season ends with a Colosseum week; the birth of the next
instance is the same observation as the prior season's death, so
season_closed for season N fires when the rollover to N+1 is observed.

Q2 at season close: War Champ = top cumulative season fame (the honor,
unconditional); the Free Pass rotates — never the same player in sequential
seasons; falls to rank 2. The Q5 award pass consumes the season_closed event;
this module never writes the awards ledger.
"""

from __future__ import annotations

from engine.db import canon_tag, utcnow
from engine.emitters import insert_stream_event
from engine.normalize import war_day as normalize_war_day


def project_race_aspect(payload: dict, season_id: int | None) -> dict:
    """The riverrace baseline: one deterministic projection of the live race."""
    clan = payload.get("clan") or {}
    clans = {}
    for c in payload.get("clans") or []:
        tag = canon_tag(c.get("tag"))
        if not tag:
            continue
        clans[tag] = {
            "name": c.get("name"),
            "fame": c.get("fame"),
            "period_points": c.get("periodPoints"),
            "clan_score": c.get("clanScore"),
        }
    participants = {}
    for p in clan.get("participants") or []:
        tag = canon_tag(p.get("tag"))
        if not tag:
            continue
        participants[tag] = {
            "name": p.get("name"),
            "fame": p.get("fame"),
            "repair_points": p.get("repairPoints"),
            "boat_attacks": p.get("boatAttacks"),
            "decks_used": p.get("decksUsed"),
            "decks_used_today": p.get("decksUsedToday"),
        }
    return {
        "season_id": season_id,
        "section_index": payload.get("sectionIndex"),
        "period_index": payload.get("periodIndex"),
        "period_type": payload.get("periodType"),
        "our_tag": canon_tag(clan.get("tag")),
        "our_fame": clan.get("fame"),
        "clans": clans,
        "participants": participants,
    }


def _emit(conn, season_id, section_index, observed_at, window_start, event_type, dedup_key, payload) -> int:
    return insert_stream_event(
        conn,
        "war_events",
        dedup_key=dedup_key,
        event_type=event_type,
        subject_cols={"season_id": season_id, "section_index": section_index},
        observed_at=observed_at,
        window_start=window_start,
        payload=payload,
    )


def _standings(state: dict) -> list[dict]:
    rows = [
        {"clan_tag": tag, "fame": (info or {}).get("fame") or 0}
        for tag, info in (state.get("clans") or {}).items()
    ]
    rows.sort(key=lambda r: -r["fame"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _our_rank(state: dict) -> int | None:
    for row in _standings(state):
        if row["clan_tag"] == state.get("our_tag"):
            return row["rank"]
    return None


def _ensure_season(conn, season_id: int, observed_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO war_seasons (season_id, started_at) VALUES (?, ?)",
        (season_id, observed_at),
    )


def _upsert_week(conn, season_id, section_index, period_type, observed_at) -> None:
    conn.execute(
        """INSERT INTO war_weeks (season_id, section_index, period_type, created_date)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(season_id, section_index)
           DO UPDATE SET period_type = excluded.period_type""",
        (season_id, section_index, period_type, observed_at[:10]),
    )


def _finalize_week(conn, state: dict, observed_at: str) -> None:
    """Write the finished week's summary + standings (week_finished side)."""
    season_id, section = state.get("season_id"), state.get("section_index")
    if season_id is None or section is None:
        return
    conn.execute(
        """INSERT INTO war_weeks (season_id, section_index, period_type,
                                  finish_time, our_rank, our_fame)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(season_id, section_index) DO UPDATE SET
               finish_time = excluded.finish_time,
               our_rank = excluded.our_rank,
               our_fame = excluded.our_fame""",
        (season_id, section, state.get("period_type"), observed_at,
         _our_rank(state), state.get("our_fame")),
    )
    for row in _standings(state):
        conn.execute(
            """INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(clan_tag) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (row["clan_tag"],
             (state.get("clans") or {}).get(row["clan_tag"], {}).get("name"),
             observed_at, observed_at),
        )
        conn.execute(
            """INSERT INTO war_week_clans (season_id, section_index, clan_tag,
                                           fame, rank, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(season_id, section_index, clan_tag) DO UPDATE SET
                   fame = excluded.fame, rank = excluded.rank,
                   observed_at = excluded.observed_at""",
            (season_id, section, row["clan_tag"], row["fame"], row["rank"], observed_at),
        )


def _upsert_participation(conn, state: dict, observed_at: str) -> None:
    season_id, section = state.get("season_id"), state.get("section_index")
    if season_id is None or section is None:
        return
    wd = normalize_war_day(state.get("period_index"))
    war_day = wd.war_day_index if wd is not None else None
    for tag, p in (state.get("participants") or {}).items():
        p = p or {}
        conn.execute(
            """INSERT INTO war_participation (season_id, section_index, player_tag,
                   fame, repair_points, boat_attacks, decks_used, decks_used_today, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(season_id, section_index, player_tag) DO UPDATE SET
                   fame = excluded.fame, repair_points = excluded.repair_points,
                   boat_attacks = excluded.boat_attacks, decks_used = excluded.decks_used,
                   decks_used_today = excluded.decks_used_today,
                   observed_at = excluded.observed_at""",
            (season_id, section, tag, p.get("fame"), p.get("repair_points"),
             p.get("boat_attacks"), p.get("decks_used"), p.get("decks_used_today"),
             observed_at),
        )
        if war_day is not None:
            conn.execute(
                """INSERT INTO war_attendance_days (season_id, section_index,
                       war_day_index, player_tag, decks_used, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(season_id, section_index, war_day_index, player_tag)
                   DO UPDATE SET decks_used = excluded.decks_used,
                                 observed_at = excluded.observed_at""",
                (season_id, section, war_day, tag, p.get("decks_used_today") or 0,
                 observed_at),
            )


def _season_standings(conn, season_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT player_tag, SUM(COALESCE(fame, 0)) AS total_fame,
                  SUM(COALESCE(decks_used, 0)) AS decks_used
           FROM war_participation WHERE season_id = ?
           GROUP BY player_tag ORDER BY total_fame DESC, player_tag""",
        (season_id,),
    ).fetchall()
    return [
        {"player_tag": r["player_tag"], "fame": r["total_fame"],
         "decks_used": r["decks_used"], "rank": i + 1}
        for i, r in enumerate(rows)
    ]


def _last_free_pass_tag(conn, before_season: int) -> str | None:
    row = conn.execute(
        """SELECT player_tag FROM awards WHERE award_type = 'free_pass'
           AND season_id < ? ORDER BY season_id DESC LIMIT 1""",
        (before_season,),
    ).fetchone()
    return row["player_tag"] if row else None


def close_season(conn, season_id: int, final_state: dict, observed_at: str) -> int:
    """Season death (§16.1): compute Q2's honor + rotation outcome into
    war_seasons and emit season_closed. Idempotent via the event dedup key."""
    standings = _season_standings(conn, season_id)
    champ = standings[0]["player_tag"] if standings else None
    free_pass = champ
    if champ and _last_free_pass_tag(conn, season_id) == champ and len(standings) > 1:
        free_pass = standings[1]["player_tag"]  # rotation: falls to rank 2 (Q2)
    final_rank = _our_rank(final_state)
    weeks = conn.execute(
        "SELECT COUNT(*) FROM war_weeks WHERE season_id = ?", (season_id,)
    ).fetchone()[0]
    conn.execute(
        """UPDATE war_seasons SET ended_at = ?, final_rank = ?, weeks = ?,
               war_champ_tag = ?, free_pass_tag = ?
           WHERE season_id = ?""",
        (observed_at, final_rank, weeks, champ, free_pass, season_id),
    )
    return _emit(conn, season_id, None, observed_at, None, "season_closed",
                 f"season_closed:{season_id}",
                 {"final_rank": final_rank, "weeks": weeks,
                  "war_champ_tag": champ, "free_pass_tag": free_pass,
                  "standings_top": standings[:3]})


def emit_race(conn, entity_tag, old, new, observed_at, window_start) -> int:
    """Diff two race baselines. `entity_tag` is our clan tag (schema.md §4)."""
    n = 0
    old_season, new_season = old.get("season_id"), new.get("season_id")
    old_section, new_section = old.get("section_index"), new.get("section_index")
    old_period, new_period = old.get("period_index"), new.get("period_index")
    old_type, new_type = old.get("period_type"), new.get("period_type")

    if new_season is not None:
        _ensure_season(conn, new_season, observed_at)
        _upsert_week(conn, new_season, new_section, new_type, observed_at)

    # season rollover: prior season dies, new one is born (same observation)
    if new_season is not None and old_season is not None and new_season != old_season:
        _finalize_week(conn, old, observed_at)  # colosseum week's final standings
        n += _emit(conn, old_season, old_section, observed_at, window_start,
                   "week_finished", f"week_finished:{old_season}:{old_section}",
                   {"our_rank": _our_rank(old), "our_fame": old.get("our_fame"),
                    "standings": _standings(old)})
        n += close_season(conn, old_season, old, observed_at)
        n += _emit(conn, new_season, new_section, observed_at, window_start,
                   "season_started", f"season_started:{new_season}",
                   {"season_id": new_season})
    elif old_season is None and new_season is not None:
        n += _emit(conn, new_season, new_section, observed_at, window_start,
                   "season_started", f"season_started:{new_season}",
                   {"season_id": new_season})

    # week rollover within a season
    if (
        new_season is not None
        and new_season == old_season
        and isinstance(new_section, int)
        and isinstance(old_section, int)
        and new_section > old_section
    ):
        _finalize_week(conn, old, observed_at)
        n += _emit(conn, old_season, old_section, observed_at, window_start,
                   "week_finished", f"week_finished:{old_season}:{old_section}",
                   {"our_rank": _our_rank(old), "our_fame": old.get("our_fame"),
                    "standings": _standings(old)})

    # colosseum detected — the end is discovered, not known (§16.1)
    if new_type == "colosseum" and old_type != "colosseum" and new_season is not None:
        conn.execute(
            "UPDATE war_seasons SET colosseum_detected_at = ? "
            "WHERE season_id = ? AND colosseum_detected_at IS NULL",
            (observed_at, new_season),
        )
        n += _emit(conn, new_season, new_section, observed_at, window_start,
                   "colosseum_detected", f"colosseum_detected:{new_season}",
                   {"section_index": new_section})

    # war day opened — day transitions into a battle day
    if (
        isinstance(new_period, int)
        and new_period != old_period
        and new_type in ("warDay", "colosseum")
        and (wd_open := normalize_war_day(new_period)) is not None
        and wd_open.war_day_index is not None
        and new_season is not None
    ):
        n += _emit(conn, new_season, new_section, observed_at, window_start,
                   "war_day_opened",
                   f"war_day_opened:{new_season}:{new_section}:{wd_open.war_day_index}",
                   {"period_type": new_type, "day_index": wd_open.war_day_index,
                    "war_day_human": wd_open.human})

    # race finished — we crossed the finish line mid-week (§16.4 tone shift)
    finish_line = 5_000 if new_type == "colosseum" else 10_000
    old_fame, new_fame = old.get("our_fame") or 0, new.get("our_fame") or 0
    if (
        new_type in ("warDay", "colosseum")
        and old_fame < finish_line <= new_fame
        and new_season is not None
    ):
        n += _emit(conn, new_season, new_section, observed_at, window_start,
                   "race_finished", f"race_finished:{new_season}:{new_section}",
                   {"finished_at": observed_at})

    _upsert_participation(conn, new, observed_at)
    return n


def finalize_attendance_day(conn, season_id: int, section_index: int, war_day_index: int) -> int:
    """End-of-battle-day finalization (runtime.md §3 war-attendance-snapshot):
    stamp final decks_used and fame_delta; evaluators read finalized days only."""
    cur = conn.execute(
        """UPDATE war_attendance_days
           SET fame_delta = COALESCE(fame_delta, 0), observed_at = ?
           WHERE season_id = ? AND section_index = ? AND war_day_index = ?""",
        (utcnow(), season_id, section_index, war_day_index),
    )
    return cur.rowcount
