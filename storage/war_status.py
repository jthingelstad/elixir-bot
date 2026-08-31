"""War read layer — v5.1 sources (docs/reference/v5.1/schema.md §9).

Live race state reads the engine's riverrace baseline
(state_baselines('riverrace'), the race-aspect projection); logged weeks read
war_weeks/war_week_clans; per-member day detail reads war_attendance_days +
war_participation. The war_current_state / war_participant_snapshots /
war_races tables are gone (schema.md §8).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from db import (
    _canon_tag,
    _parse_cr_time,
    _rowdicts,
    managed_connection,
)
from engine.game_rules import (
    NORMAL_RIVER_RACE_FINISH_LINE,
    river_race_completed_from_score,
    river_race_finish_line,
)
from engine.war_seasons import is_final_section, season_shape, war_date
from storage._enrichment import _member_reference_fields
from storage._war_shared import (
    get_latest_logged_race,
    infer_current_season_id_from_live_state,
)
from storage.war_calendar import (
    FINAL_BATTLE_PERIOD_OFFSET,
    FINAL_PRACTICE_PERIOD_OFFSET,
    FIRST_BATTLE_PERIOD_OFFSET,
    coerce_utc_datetime,
    format_utc_iso,
    period_offset,
    phase_day_number,
    resolve_phase,
    war_day_key,
    war_reset_window_utc,
)

log = logging.getLogger("elixir_db")

HOME_CLAN = "#J2RGCRVG"

# Compatibility export; the canonical rule lives in engine.game_rules.
FAME_FINISH_LINE = NORMAL_RIVER_RACE_FINISH_LINE

# Fame ("movement points") a clan banks at day close for its daily period-point
# rank (Clash wiki). Intact boat defenses add a further diminishing survival
# award on top, which the live API does not expose — so this is the placement
# floor, not the whole day's fame. In-game the boat screen shows this as the
# projected reward for your current rank if you hold it to reset.
DAILY_RANK_FAME = {1: 3_000, 2: 1_800, 3: 1_000, 4: 600, 5: 400}


def _coerce_int(value) -> int:
    try:
        return int(value or 0)
    except TypeError, ValueError:
        return 0


def _live_race(conn) -> Optional[tuple[dict, str]]:
    """The engine's riverrace baseline: (race-aspect projection, observed_at)."""
    row = conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind = 'riverrace' AND aspect = 'race' "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"]), row["observed_at"]
    except TypeError, ValueError:
        return None


def _rank_standings(projection: dict, *, by: str) -> list[dict]:
    """Clans ranked by ONE River Race field — ``by`` is 'fame' (the weekly
    cumulative boat race that decides the winner) or 'period_points' (today's
    race, which resets each day). Every entry still carries BOTH raw scores so a
    caller never has to re-derive them, but only the chosen field drives the
    order/rank. Keeping the two scoreboards strictly separate is the whole point:
    a clan's period points and another clan's fame are different races and must
    never be compared to each other (see get_current_war_status)."""
    clans = projection.get("clans") or {}
    our_tag = _canon_tag(projection.get("our_tag") or HOME_CLAN)
    ranked = sorted(
        clans.items(),
        key=lambda kv: (
            _coerce_int((kv[1] or {}).get(by)),
            _coerce_int((kv[1] or {}).get("war_league_score")),
        ),
        reverse=True,
    )
    standings = []
    for rank, (tag, info) in enumerate(ranked, start=1):
        info = info or {}
        standings.append(
            {
                "rank": rank,
                "clan_tag": tag,
                "clan_name": info.get("name"),
                "fame": _coerce_int(info.get("fame")),
                "repair_points": 0,
                "period_points": _coerce_int(info.get("period_points")),
                "war_league_score": _coerce_int(info.get("war_league_score")),
                "is_us": tag == our_tag,
            }
        )
    return standings


def _war_date_of(observed_at: Optional[str]) -> Optional[date]:
    """War-calendar date for a stored observation stamp (None → caller uses now)."""
    if not observed_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except TypeError, ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return war_date(stamp.astimezone(timezone.utc))


def _season_shape_fields(observed_at: Optional[str], section_index: Optional[int]) -> dict:
    """Season position: ``total_weeks`` / ``is_final_week`` / ``weeks_remaining``.

    Until now nothing computed a season's LENGTH — `war_seasons.weeks` is only
    written at season close — so Elixir could say "week 3" but never "week 3 of 4".
    The calendar makes the total knowable on day one of the season.
    """
    if section_index is None:
        return {}
    try:
        return {
            k: v
            for k, v in season_shape(
                _war_date_of(observed_at) or war_date(datetime.now(timezone.utc)),
                int(section_index),
            ).items()
            if k in {"total_weeks", "weeks_remaining", "is_final_week", "season_start"}
        }
    except TypeError, ValueError:
        return {}


def resolve_colosseum_week(
    period_type: Optional[str],
    *,
    section_index: Optional[int] = None,
    on: Optional[date] = None,
    trophy_change: Optional[int] = None,
    trophy_stakes_known: bool = False,
) -> tuple[bool, Optional[str]]:
    """Resolve "is this the colosseum week", returning ``(is_colosseum, source)``.

    Three tiers, strongest first:

    1. ``observed`` — the API says ``periodType == "colosseum"``. Always wins.
    2. ``trophy_stakes`` — the logged week carries ±100 trophies (colosseum stakes).
    3. ``derived`` — the calendar says this is the season's FINAL section, and
       colosseum is always the final section (:mod:`engine.war_seasons`).

    Tier 3 exists because the API cannot reveal colosseum during that week's
    PRACTICE days: ``periodType`` only flips to ``colosseum`` once battle days
    begin, and a colosseum-week practice day is indistinguishable from a normal
    one. Without it, Elixir framed 2026-07-27 (colosseum practice day 1) as a
    normal fame week and advised adding boat defenses to a parked boat.
    """
    if period_type == "colosseum":
        return True, "observed"
    if trophy_stakes_known and abs(trophy_change or 0) == 100:
        return True, "trophy_stakes"
    if section_index is not None:
        try:
            if is_final_section(on or war_date(datetime.now(timezone.utc)), int(section_index)):
                return True, "derived"
        except TypeError, ValueError:
            pass
    return False, None


def is_colosseum_week_confirmed(
    period_type: Optional[str],
    trophy_change: Optional[int] = None,
    *,
    trophy_stakes_known: bool = False,
    section_index: Optional[int] = None,
    on: Optional[date] = None,
) -> bool:
    """Back-compat boolean wrapper over :func:`resolve_colosseum_week`."""
    confirmed, _source = resolve_colosseum_week(
        period_type,
        section_index=section_index,
        on=on,
        trophy_change=trophy_change,
        trophy_stakes_known=trophy_stakes_known,
    )
    return confirmed


@managed_connection
def get_current_war_status(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    live = _live_race(conn)
    if not live:
        return None
    projection, observed_at = live
    latest_logged = get_latest_logged_race(conn)
    season_id = projection.get("season_id")
    if season_id is None:
        season_id = infer_current_season_id_from_live_state(projection, latest_logged)
    section_index = projection.get("section_index")
    period_index = projection.get("period_index")
    period_type = projection.get("period_type")
    phase = resolve_phase(period_type, period_index)
    offset = period_offset(period_index)

    trophy_change = None
    if (
        latest_logged
        and season_id == latest_logged["season_id"]
        and section_index == latest_logged["section_index"]
    ):
        trophy_change = latest_logged["trophy_change"]

    # ONE colosseum answer for every downstream surface. Previously the read used
    # a bare `period_type == "colosseum"` while build_war_now_context used the
    # confirmed variant, so they could disagree — and neither could see colosseum
    # during that week's practice days (2026-07-27, loop 372).
    colosseum, colosseum_source = resolve_colosseum_week(
        period_type,
        section_index=section_index,
        on=_war_date_of(observed_at),
        trophy_change=trophy_change,
        trophy_stakes_known=trophy_change is not None,
    )
    # Mechanics key off the RESOLVED week type, so a derived colosseum drops the
    # 10,000-fame finish line exactly like an observed one.
    effective_period_type = "colosseum" if colosseum else period_type
    our_fame = _coerce_int(projection.get("our_fame"))
    finish_line = river_race_finish_line(effective_period_type)

    # Two separate races, never coalesced (see _rank_standings):
    #  - WEEKLY fame = the boat, awarded at each day's close by that day's rank;
    #    it decides who wins the week. 0 during Day 1 until the first day closes.
    #  - DAILY period points = today's race, what players drive; resets each day.
    # Colosseum weeks have period points only (no weekly fame), so the metric
    # that decides the race there is period_points.
    race_standings = _rank_standings(projection, by="fame")  # weekly boat
    day_standings = _rank_standings(projection, by="period_points")  # today
    _us_race = next((s for s in race_standings if s["is_us"]), None)
    _us_day = next((s for s in day_standings if s["is_us"]), None)
    race_rank = _us_race["rank"] if _us_race else None
    day_rank = _us_day["rank"] if _us_day else None
    if _us_race:
        our_fame = _us_race["fame"]
    our_period_points = _us_day["period_points"] if _us_day else None
    boat_scored = any(s["fame"] > 0 for s in race_standings)
    # No battles logged yet this day → the daily standings are a 0-0-0 tie and
    # our day_rank is meaningless (don't let the brain read it as "losing today").
    day_scored = any(s["period_points"] > 0 for s in day_standings)
    primary_metric = "period_points" if colosseum else "fame"
    # What we'd bank at day close if our current daily rank holds — PLACEMENT
    # fame. Mirrors the in-game boat projection; only meaningful once today has
    # scored, and there is no fame in Colosseum.
    projected_day_fame = DAILY_RANK_FAME.get(day_rank) if (day_scored and not colosseum) else None
    # Boat defenses ALSO add fame at day close (survival award) — read directly
    # from the API's periodLogs (our_defense.defense_fame_recent), NOT
    # back-calculated. Project the recent per-day rate when defenses still stand.
    # This is what lets a full-defense clan cross the finish line a day early, so
    # fold it into projected_fame_at_close + a clinches_finish_today flag.
    our_defense = projection.get("our_defense") or {}
    projected_defense_fame = None
    if not colosseum and (our_defense.get("defenses_remaining") or 0) > 0:
        projected_defense_fame = our_defense.get("defense_fame_recent")
    projected_fame_at_close = None
    clinches_finish_today = False
    if not colosseum and projected_day_fame is not None:
        projected_fame_at_close = our_fame + projected_day_fame + (projected_defense_fame or 0)
        # We'd cross the finish line at TODAY's close (winning the week early) if
        # holding this rank + defenses gets us there and we're not already over.
        clinches_finish_today = (
            finish_line is not None and our_fame < finish_line <= projected_fame_at_close
        )

    observed_dt = coerce_utc_datetime(observed_at)
    _, period_ends_at = war_reset_window_utc(observed_dt or observed_at)

    result = {
        "observed_at": observed_at,
        "war_state": "training" if phase == "practice" else "inWar",
        "clan_tag": _canon_tag(projection.get("our_tag") or HOME_CLAN),
        "clan_name": next((s["clan_name"] for s in race_standings if s["is_us"]), None),
        "fame": our_fame,
        "repair_points": 0,
        "period_points": our_period_points,
        "war_league_score": next(
            (s["war_league_score"] for s in race_standings if s["is_us"]), None
        ),
        "primary_metric": primary_metric,
        "boat_scored": boat_scored,
        "day_scored": day_scored,
        "projected_day_fame": projected_day_fame,
        "projected_defense_fame": projected_defense_fame,
        "projected_fame_at_close": projected_fame_at_close,
        # QA L7: projected_day_fame is the PLACEMENT floor only (fame for holding
        # today's daily rank at close); boat-defense survival fame is counted
        # separately in projected_defense_fame. Read projected_fame_at_close for
        # the combined day-close total, not projected_day_fame alone.
        "projection_note": (
            "projected_day_fame = placement fame for holding today's daily rank at close; "
            "boat-defense fame is separate (projected_defense_fame, from the API's periodLogs, "
            "null when unavailable). projected_fame_at_close combines both."
        ),
        # There is no boat in a colosseum week, so there is nothing to defend —
        # this mirrors the `not colosseum` guard on projected_defense_fame above,
        # which this field was missing (it leaked a stale count into the war
        # capability and the get_river_race tool result).
        "defenses_remaining": (None if colosseum else our_defense.get("defenses_remaining")),
        "clinches_finish_today": clinches_finish_today,
        "finish_line": finish_line,
        "season_id": season_id,
        "section_index": section_index,
        "week": (section_index + 1) if section_index is not None else None,
        "period_index": period_index,
        "period_offset": offset,
        "period_type": period_type,
        "phase": phase,
        "colosseum_week": colosseum,
        # How we know: "observed" (API periodType), "trophy_stakes" (±100), or
        # "derived" (the calendar says this is the season's final section).
        "colosseum_source": colosseum_source,
        **_season_shape_fields(observed_at, section_index),
        "battle_phase_active": phase == "battle",
        "practice_phase_active": phase == "practice",
        "final_practice_day_active": phase == "practice" and offset == FINAL_PRACTICE_PERIOD_OFFSET,
        "final_battle_day_active": phase == "battle" and offset == FINAL_BATTLE_PERIOD_OFFSET,
        "battle_day_number": phase_day_number(phase, period_index) if phase == "battle" else None,
        "battle_day_total": 4 if phase == "battle" else None,
        "practice_day_number": phase_day_number(phase, period_index)
        if phase == "practice"
        else None,
        "practice_day_total": FIRST_BATTLE_PERIOD_OFFSET if phase == "practice" else None,
        "race_rank": race_rank,
        "race_standings": race_standings,
        "day_rank": day_rank,
        "day_standings": day_standings,
        "war_day_key": war_day_key(season_id, section_index, period_index, observed_at),
        "finish_time": None,
        "race_completed": river_race_completed_from_score(
            period_type,
            our_fame,
            active_battle_phase=phase != "practice",
        ),
        "race_completed_at": None,
        "race_completed_early": False,
        "trophy_change": int(trophy_change) if isinstance(trophy_change, (int, float)) else None,
        "trophy_stakes_known": isinstance(trophy_change, (int, float)),
        "trophy_stakes_text": (
            f"{abs(int(trophy_change))} trophies on the line"
            if isinstance(trophy_change, (int, float)) and trophy_change
            else None
        ),
        "period_ends_at": format_utc_iso(period_ends_at) if period_ends_at else None,
    }
    result["phase_display"] = (
        f"Battle Day {result['battle_day_number']}"
        if result["battle_day_number"] is not None
        else f"Practice Day {result['practice_day_number']}"
        if result["practice_day_number"] is not None
        else (phase.title() if phase else None)
    )
    result["season_week_label"] = (
        f"Season {season_id} Week {result['week']}"
        if season_id is not None and result.get("week") is not None
        else None
    )
    return result


def _format_duration_short(total_seconds: Optional[int]) -> Optional[str]:
    if total_seconds is None:
        return None
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@managed_connection
def get_war_day_state(
    war_day_key_arg: Optional[str] = None,
    observed_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Current war-day engagement, from the live participants projection plus
    war_attendance_days (fame_delta per member for the day)."""
    del observed_at  # historical point-in-time reads retired with the snapshots
    status = get_current_war_status(conn=conn)
    if not status:
        return None
    if war_day_key_arg and status.get("war_day_key") != war_day_key_arg:
        return None  # only the current day is reconstructable live
    live = _live_race(conn)
    if not live:
        return None
    projection, observed_at_live = live
    participants_map = projection.get("participants") or {}

    season_id = status.get("season_id")
    section_index = status.get("section_index")

    # Riverrace participants persist for the whole week even after a player
    # leaves the clan — without a membership filter, departed players landed
    # in used_none and chat said "49 haven't started" against a 47-member
    # roster (rehearsal finding 2026-07-04). Deck buckets are current-members
    # only; departed fame still counts in the fame lists. Empty set =
    # fixture/cold-start → treat everyone as a member.
    open_tags = {
        r["player_tag"]
        for r in conn.execute(
            "SELECT player_tag FROM clan_memberships WHERE left_at IS NULL"
        ).fetchall()
    }
    participants, used_all, used_some, used_none, departed = [], [], [], [], []
    for tag, info in participants_map.items():
        info = info or {}
        item = {
            "tag": tag,
            "name": info.get("name"),
            "points": _coerce_int(info.get("fame")),
            "repair_points": _coerce_int(info.get("repair_points")),
            "boat_attacks": _coerce_int(info.get("boat_attacks")),
            "decks_used_total": _coerce_int(info.get("decks_used")),
            "decks_used_today": _coerce_int(info.get("decks_used_today")),
            # QA M6: per-member daily war points are not tracked (fame_delta is
            # never populated), so there is no honest "points today" — None, not a
            # misleading 0. Today's engagement signal is decks_used_today.
            "points_today": None,
        }
        item = _member_reference_fields(conn, tag, item)
        item["is_current_member"] = (not open_tags) or tag in open_tags
        participants.append(item)
        if not item["is_current_member"]:
            departed.append(item)
            continue
        decks_today = item["decks_used_today"]
        if decks_today >= 4:
            used_all.append(item)
        elif decks_today > 0:
            used_some.append(item)
        else:
            used_none.append(item)

    # top_points_today would just re-rank by cumulative points (daily points
    # aren't tracked — QA M6), so it's omitted rather than mislabelled.
    # top_points_total is the honest cumulative-points leaderboard.
    top_points_total = sorted(
        participants,
        key=lambda i: (
            -(i.get("points") or 0),
            -(i.get("decks_used_total") or 0),
            (i.get("name") or "").lower(),
        ),
    )

    observed_dt = coerce_utc_datetime(observed_at_live)
    started_at, ends_at = war_reset_window_utc(observed_dt or observed_at_live)
    now = datetime.now(timezone.utc)
    time_left_seconds = max(0, int((ends_at - now).total_seconds())) if ends_at else None

    phase = status.get("phase")
    day_number = (
        status.get("battle_day_number") if phase == "battle" else status.get("practice_day_number")
    )
    return {
        "war_day_key": status.get("war_day_key"),
        "season_id": season_id,
        "section_index": section_index,
        "week": status.get("week"),
        "period_index": status.get("period_index"),
        "period_type": status.get("period_type"),
        "phase": phase,
        "phase_display": status.get("phase_display"),
        "day_number": day_number,
        "day_total": status.get("battle_day_total")
        if phase == "battle"
        else status.get("practice_day_total"),
        "race_rank": status.get("race_rank"),
        "day_rank": status.get("day_rank"),
        "clan_fame": status.get("fame"),
        "war_league_score": status.get("war_league_score"),
        "period_points": status.get("period_points"),
        "finish_time": status.get("finish_time"),
        "race_completed": status.get("race_completed"),
        "race_completed_at": status.get("race_completed_at"),
        "race_completed_early": status.get("race_completed_early"),
        "trophy_change": status.get("trophy_change"),
        "trophy_stakes_known": status.get("trophy_stakes_known"),
        "trophy_stakes_text": status.get("trophy_stakes_text"),
        "observed_at": observed_at_live,
        "first_observed_at": observed_at_live,
        "last_observed_at": observed_at_live,
        "period_started_at": format_utc_iso(started_at) if started_at else None,
        "period_ends_at": format_utc_iso(ends_at) if ends_at else None,
        "time_left_seconds": time_left_seconds,
        "time_left_text": _format_duration_short(time_left_seconds),
        # Member-scoped counts (bucket sums stay internally consistent for
        # downstream decks-left math); the raw week-long participant total
        # keeps its own key.
        "total_participants": len(used_all) + len(used_some) + len(used_none),
        "all_participant_count": len(participants),
        "departed_participant_count": len(departed),
        "engaged_count": len(used_all) + len(used_some),
        "finished_count": len(used_all),
        "untouched_count": len(used_none),
        "used_all_4": used_all,
        "used_some": used_some,
        "used_none": used_none,
        "top_points_total": top_points_total[:5],
        "participants": participants,
    }


def get_current_war_day_state(
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    return get_war_day_state(None, conn=conn)


@managed_connection
def list_recent_war_day_summaries(
    limit: int = 7,
    phase: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Recent war-day engagement from war_attendance_days (finalized days)."""
    rows = conn.execute(
        "SELECT season_id, section_index, war_day_index, "
        "COUNT(*) AS participants, "
        "SUM(CASE WHEN decks_used >= 4 THEN 1 ELSE 0 END) AS finished_count, "
        "SUM(CASE WHEN decks_used > 0 THEN 1 ELSE 0 END) AS engaged_count, "
        "MAX(observed_at) AS observed_at "
        "FROM war_attendance_days "
        "GROUP BY season_id, section_index, war_day_index "
        "ORDER BY observed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    del phase  # attendance days are battle days by definition
    out = []
    for row in rows:
        out.append(
            {
                "war_day_key": f"{row['season_id']}:{row['section_index']}:{row['war_day_index']}",
                "season_id": row["season_id"],
                "section_index": row["section_index"],
                "phase": "battle",
                "phase_display": f"Battle Day {row['war_day_index'] + 1}",
                "engaged_count": row["engaged_count"],
                "finished_count": row["finished_count"],
                "total_participants": row["participants"],
                "observed_at": row["observed_at"],
                "top_points_today": [],
            }
        )
    return out


@managed_connection
def get_latest_war_participant_snapshot_observed_at(
    war_day_key_arg: str = "", conn: Optional[sqlite3.Connection] = None
) -> Optional[str]:
    """Freshness anchor: the riverrace baseline's observed_at (the snapshot
    tables are gone; the baseline is the live participants source)."""
    del war_day_key_arg
    live = _live_race(conn)
    return live[1] if live else None


@managed_connection
def get_latest_clan_boat_defense_status(
    clan_tag: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    """Boat-defense detail came from war_period_clan_status (dropped). The new
    race projection has no defense fields; return None so callers fall back
    gracefully (the war clock gates boat-defense talk anyway, §16.2)."""
    del clan_tag, conn
    return None


def get_war_deck_status_today(conn: Optional[sqlite3.Connection] = None) -> dict:
    state = get_current_war_day_state(conn=conn)
    if not state:
        return {
            "battle_date": None,
            "used_all_4": [],
            "used_some": [],
            "used_none": [],
            "total_participants": 0,
        }
    return {
        "battle_date": state.get("war_day_key"),
        "season_id": state.get("season_id"),
        "week": state.get("week"),
        "phase": state.get("phase"),
        "phase_display": state.get("phase_display"),
        "day_number": state.get("day_number"),
        "period_started_at": state.get("period_started_at"),
        "period_ends_at": state.get("period_ends_at"),
        "time_left_seconds": state.get("time_left_seconds"),
        "time_left_text": state.get("time_left_text"),
        "race_rank": state.get("race_rank"),
        "day_rank": state.get("day_rank"),
        "clan_fame": state.get("clan_fame"),
        "war_league_score": state.get("war_league_score"),
        "period_points": state.get("period_points"),
        "used_all_4": state.get("used_all_4") or [],
        "used_some": state.get("used_some") or [],
        "used_none": state.get("used_none") or [],
        "top_points_total": state.get("top_points_total") or [],
        "engaged_count": state.get("engaged_count") or 0,
        "finished_count": state.get("finished_count") or 0,
        "untouched_count": state.get("untouched_count") or 0,
        "total_participants": state.get("total_participants") or 0,
    }


def build_war_now_context(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Single source of truth for 'what moment is it in the war' — returns the
    STRUCTURED data only (no rendered prose; the agent layer renders it via
    agent.war_render.render_war_now). ``None`` when there is no race baseline yet."""
    status = get_current_war_status(conn=conn) or {}
    if not status:
        return None
    day_state = get_current_war_day_state(conn=conn) or {}

    period_type = status.get("period_type")
    phase = status.get("phase")
    if phase == "battle":
        day_number = status.get("battle_day_number")
        day_total = status.get("battle_day_total")
    else:
        day_number = status.get("practice_day_number")
        day_total = status.get("practice_day_total")

    time_left_seconds = day_state.get("time_left_seconds")
    # Consume the single resolved answer from get_current_war_status rather than
    # re-deriving it here — this context used to run its own detection without the
    # section index, so it could disagree with the awareness read about the very
    # same week.
    colosseum_week = status.get("colosseum_week")

    data = {
        "season_id": status.get("season_id"),
        "week": status.get("week"),
        "total_weeks": status.get("total_weeks"),
        "weeks_remaining": status.get("weeks_remaining"),
        "is_final_week": bool(status.get("is_final_week", False)),
        "colosseum_source": status.get("colosseum_source"),
        "phase": phase,
        "phase_display": status.get("phase_display"),
        "day_number": day_number,
        "day_total": day_total,
        "period_type": period_type,
        "time_left_seconds": time_left_seconds,
        "time_left_text": _format_duration_short(time_left_seconds),
        "period_started_at": day_state.get("period_started_at"),
        "period_ends_at": day_state.get("period_ends_at"),
        "is_colosseum_week": bool(colosseum_week),
        "is_final_battle_day": bool(status.get("final_battle_day_active", False)),
        "is_final_practice_day": bool(status.get("final_practice_day_active", False)),
        "race_standings": status.get("race_standings") or [],
        "day_standings": status.get("day_standings") or [],
        "primary_metric": status.get("primary_metric"),
        "boat_scored": bool(status.get("boat_scored")),
        "day_scored": bool(status.get("day_scored")),
    }
    return data


@managed_connection
def get_war_week_summary(
    season_id: Optional[int] = None,
    section_index: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    current = get_current_war_status(conn=conn)
    if season_id is None:
        season_id = (current or {}).get("season_id")
    if section_index is None:
        section_index = (current or {}).get("section_index")
    if season_id is None or section_index is None:
        return None

    race = conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, finish_time "
        "FROM war_weeks WHERE season_id = ? AND section_index = ?",
        (season_id, section_index),
    ).fetchone()
    participant_rows = conn.execute(
        "SELECT wp.player_tag, COALESCE(p.display_name, p.current_name) AS player_name, wp.fame AS points, wp.repair_points, wp.boat_attacks, wp.decks_used "
        "FROM war_participation wp LEFT JOIN players p ON p.player_tag = wp.player_tag "
        "WHERE wp.season_id = ? AND wp.section_index = ? "
        "ORDER BY COALESCE(wp.fame, 0) DESC, COALESCE(wp.decks_used, 0) DESC, player_name COLLATE NOCASE",
        (season_id, section_index),
    ).fetchall()
    top_participants = []
    for row in participant_rows[:5]:
        item = {
            "tag": row["player_tag"],
            "name": row["player_name"],
            "points": row["points"] or 0,
            "repair_points": row["repair_points"] or 0,
            "boat_attacks": row["boat_attacks"] or 0,
            "decks_used": row["decks_used"] or 0,
        }
        top_participants.append(_member_reference_fields(conn, row["player_tag"], item))

    day_summaries = []
    for row in conn.execute(
        "SELECT war_day_index, COUNT(*) AS participants, "
        "SUM(CASE WHEN decks_used >= 4 THEN 1 ELSE 0 END) AS finished_count, "
        "SUM(CASE WHEN decks_used > 0 THEN 1 ELSE 0 END) AS engaged_count "
        "FROM war_attendance_days WHERE season_id = ? AND section_index = ? "
        "GROUP BY war_day_index ORDER BY war_day_index ASC",
        (season_id, section_index),
    ).fetchall():
        day_summaries.append(
            {
                "war_day_key": f"{season_id}:{section_index}:{row['war_day_index']}",
                "phase": "battle",
                "phase_display": f"Battle Day {row['war_day_index'] + 1}",
                "engaged_count": row["engaged_count"],
                "finished_count": row["finished_count"],
                "top_points_today": [],
            }
        )

    return {
        "season_id": season_id,
        "section_index": section_index,
        "week": section_index + 1,
        "race": dict(race) if race else None,
        "participant_count": len(participant_rows),
        "top_participants": top_participants,
        "day_summaries": day_summaries,
    }


@managed_connection
def get_war_season_summary(
    season_id: Optional[int] = None,
    top_n: int = 5,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    from storage.war_analytics import (
        get_members_without_war_participation,
        get_war_champ_standings,
    )

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    total_races = conn.execute(
        "SELECT COUNT(*) AS cnt, SUM(COALESCE(our_fame, 0)) AS total_clan_fame "
        "FROM war_weeks WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    total_fame = total_races["total_clan_fame"] or 0
    races = total_races["cnt"]
    # QA H7/H8: war_weeks.our_fame is NULL until a week finalizes at close, so the
    # in-progress week contributed 0 and the whole season read "0 clan fame" while
    # members visibly had thousands. Add the live current-week boat fame from the
    # war clock when the current week isn't yet finalized in war_weeks.
    in_progress = False
    live = get_current_war_status(conn=conn) or {}
    if live.get("season_id") == season_id and live.get("fame"):
        cur = conn.execute(
            "SELECT our_fame FROM war_weeks WHERE season_id = ? AND section_index = ?",
            (season_id, live.get("section_index")),
        ).fetchone()
        if not (cur and cur["our_fame"] is not None):
            total_fame += int(live["fame"])
            in_progress = True
            if cur is None:  # current week not yet a war_weeks row → count it
                races += 1
    top = get_war_champ_standings(season_id=season_id, conn=conn)[:top_n]
    nonparticipants = get_members_without_war_participation(season_id=season_id, conn=conn)[
        "members"
    ]
    active_members = conn.execute(
        "SELECT COUNT(*) AS cnt FROM clan_memberships WHERE left_at IS NULL"
    ).fetchone()["cnt"]
    # Per-member attribution is POINTS (war_participation), never fame — fame is
    # the boat's number alone, capped by the finish line, so dividing it across
    # members says nothing about contribution. Participation rows are live all
    # week, so unlike the boat fame above no finalized-week fold-in is needed.
    total_points = (
        conn.execute(
            "SELECT SUM(COALESCE(fame, 0)) AS pts FROM war_participation WHERE season_id = ?",
            (season_id,),
        ).fetchone()["pts"]
        or 0
    )
    return {
        "season_id": season_id,
        "races": races,
        "total_clan_fame": total_fame,
        "total_member_points": total_points,
        "points_per_active_member": (
            round(total_points / active_members, 2) if active_members else 0
        ),
        "current_week_in_progress": in_progress,
        "top_contributors": top,
        "nonparticipants": nonparticipants,
    }


@managed_connection
def get_trophy_drops(
    days: int = 7, min_drop: int = 100, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    """Members whose Trophy Road trophies actually DECLINED over the window.

    QA H11: this previously computed MAX-MIN (an unsigned spread) and labelled
    it 'drop', so a member who *climbed* 8700->9004 was reported as dropping
    304. We now take the directional first->last net (like get_trophy_changes)
    and return only genuine declines of at least min_drop. Note: this reads
    Trophy Road (player_daily_metrics.trophies); Path-of-Legends/ranked losses
    live elsewhere and are not covered here."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name,
                   d.trophies, d.metric_date, d.player_tag,
                   ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date ASC) AS rn_asc,
                   ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date DESC) AS rn_desc
            FROM player_daily_metrics d
            JOIN players m ON m.player_tag = d.player_tag
            WHERE d.metric_date >= ?
              AND EXISTS (SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)
        )
        SELECT a.tag, a.name,
               a.trophies AS from_trophies, b.trophies AS to_trophies,
               a.metric_date AS from_date, b.metric_date AS to_date,
               (b.trophies - a.trophies) AS change
        FROM ranked a
        JOIN ranked b ON a.player_tag = b.player_tag
        WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND (b.trophies - a.trophies) <= ?
        ORDER BY change ASC
        """,
        (cutoff, -abs(min_drop)),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["drop"] = -item["change"]  # positive magnitude of the decline
        result.append(item)
    return result


@managed_connection
def get_trophy_changes(
    since_hours: int = 24, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)
    ).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, d.trophies, d.metric_date,
                ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date ASC) AS rn_asc,
                ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date DESC) AS rn_desc,
                d.player_tag
            FROM player_daily_metrics d
            JOIN players m ON m.player_tag = d.player_tag
            WHERE d.metric_date >= ?
        )
        SELECT a.tag, a.name,
               a.trophies AS old_trophies,
               b.trophies AS new_trophies,
               (b.trophies - a.trophies) AS change
        FROM ranked a
        JOIN ranked b ON a.player_tag = b.player_tag
        WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
        ORDER BY ABS(change) DESC
        """,
        (cutoff,),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_war_history(n: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT (season_id * 100 + section_index) AS id, season_id, section_index, our_rank, our_fame, defense_fame, "
        "finish_time, created_date, NULL AS standings_json FROM war_weeks "
        "ORDER BY season_id DESC, section_index DESC LIMIT ?",
        (n,),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_week_win_streak(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Consecutive War-week #1 finishes, most-recent-first, from ``war_weeks``.

    Only FINALIZED weeks count (our_rank set — the in-progress training/battle
    period is still NULL). Returns ``{"streak", "weeks_tracked", "all_first",
    "since"}``. POAP KINGS has won every war week it has ever played (clan wars
    need 10+ members, so the clan's first ~10 memberless days had no war), so the
    streak is effectively the clan's entire war history — but we report the
    provable number, not the founding claim."""
    rows = conn.execute(
        "SELECT season_id, section_index, our_rank FROM war_weeks "
        "WHERE our_rank IS NOT NULL ORDER BY season_id DESC, section_index DESC"
    ).fetchall()
    streak = 0
    for row in rows:
        if row["our_rank"] == 1:
            streak += 1
        else:
            break
    weeks_tracked = len(rows)
    oldest = rows[-1] if rows else None
    return {
        "streak": streak,
        "weeks_tracked": weeks_tracked,
        "all_first": weeks_tracked > 0 and streak == weeks_tracked,
        "since": (f"season {oldest['season_id']}" if oldest else None),
    }


def get_current_season_id(conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
    current = get_current_war_status(conn=conn)
    return current.get("season_id") if current else None


@managed_connection
def count_war_races_for_season(season_id: int, conn: Optional[sqlite3.Connection] = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM war_weeks WHERE season_id = ?", (season_id,)
    ).fetchone()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0


@managed_connection
def is_war_section_finalized(
    season_id: int, section_index: int, conn: Optional[sqlite3.Connection] = None
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM war_weeks WHERE season_id = ? AND section_index = ? "
        "AND (finish_time IS NOT NULL OR our_rank IS NOT NULL) LIMIT 1",
        (season_id, section_index),
    ).fetchone()
    return row is not None


@managed_connection
def get_latest_war_race_finish_time(
    season_id: int, conn: Optional[sqlite3.Connection] = None
) -> Optional[str]:
    """Most recent finalized week's finish_time (or created_date fallback)."""
    row = conn.execute(
        "SELECT finish_time, created_date FROM war_weeks "
        "WHERE season_id = ? ORDER BY section_index DESC LIMIT 1",
        (season_id,),
    ).fetchone()
    if not row:
        return None
    return row["finish_time"] or row["created_date"]


def _season_bounds(conn: sqlite3.Connection, season_id: int) -> tuple[Optional[str], Optional[str]]:
    first = conn.execute(
        "SELECT created_date FROM war_weeks WHERE season_id = ? "
        "AND created_date IS NOT NULL ORDER BY section_index ASC LIMIT 1",
        (season_id,),
    ).fetchone()
    last = conn.execute(
        "SELECT COALESCE(finish_time, created_date) AS end_date FROM war_weeks "
        "WHERE season_id = ? AND COALESCE(finish_time, created_date) IS NOT NULL "
        "ORDER BY section_index DESC LIMIT 1",
        (season_id,),
    ).fetchone()
    if not first or not last:
        return None, None
    start_dt = _parse_cr_time(first["created_date"])
    end_dt = _parse_cr_time(last["end_date"])
    if not start_dt or not end_dt:
        return None, None
    end_dt = end_dt + timedelta(days=7)
    return start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@managed_connection
def get_season_window(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Concrete River Race season frame: date bounds + week-by-week trajectory."""
    sid = season_id if season_id is not None else get_current_season_id(conn=conn)
    if sid is None:
        row = conn.execute("SELECT MAX(season_id) AS sid FROM war_seasons").fetchone()
        sid = row["sid"] if row else None
    if sid is None:
        return None
    start, end = _season_bounds(conn, int(sid))
    weeks = conn.execute(
        "SELECT ww.section_index, ww.our_rank, ww.our_fame, ww.trophy_change, ww.defense_fame, "
        "(SELECT COUNT(*) FROM war_week_clans wwc "
        " WHERE wwc.season_id = ww.season_id AND wwc.section_index = ww.section_index) AS clans "
        "FROM war_weeks ww WHERE ww.season_id = ? ORDER BY ww.section_index ASC",
        (int(sid),),
    ).fetchall()
    # QA M14: a week's our_rank/our_fame stay NULL until it finalizes at close,
    # so the in-progress week read as null rank/fame (and weeks_recorded overstated
    # completeness). Fill the live current week from the war clock and flag it.
    live = get_current_war_status(conn=conn) or {}
    live_sid = live.get("season_id")
    live_section = live.get("section_index")
    trajectory = []
    for row in weeks:
        item = {
            "section_index": row["section_index"],
            "rank": row["our_rank"],
            "fame": row["our_fame"],
            "defense_fame": row["defense_fame"],
            "trophy_change": row["trophy_change"],
            "clans": row["clans"] or None,
            "in_progress": False,
        }
        if (
            live_sid == int(sid)
            and row["section_index"] == live_section
            and row["our_fame"] is None
        ):
            item["rank"] = live.get("race_rank")
            item["fame"] = live.get("fame")
            item["in_progress"] = True
        trajectory.append(item)
    finalized = sum(1 for t in trajectory if not t["in_progress"])
    return {
        "season_id": int(sid),
        "start": start,
        "end": end,
        "weeks_recorded": len(trajectory),
        "weeks_finalized": finalized,
        "week_trajectory": trajectory,
    }


def _strip_ranks(standings: list | None, scored: bool) -> list:
    """Copy a scoreboard, nulling each entry's ``rank`` when the race hasn't
    scored — the API's order on an all-zero board is an arbitrary tiebreaker,
    not a standing, and a raw ordinal there reads to the brain as a real rank."""
    out = []
    for s in standings or []:
        row = dict(s)
        if not scored:
            row["rank"] = None
        out.append(row)
    return out


def get_war_season_snapshot(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    """Season-state snapshot for memory-synthesis, leadership reports, and
    get_elixir_state. Lean v5.1 rebuild of the retired storage.projects
    version: computed from the war clock + war tables on demand; the Gen C
    project machinery (active_risks, recent_communications,
    prior_cycle_comparison) is gone, so those keys return empty — callers
    render this dict generically. Returns None when no season is active."""
    current = get_current_war_status(conn=conn) or {}
    season_id = current.get("season_id")
    if season_id is None:
        return None

    @managed_connection
    def _build(*, conn: Optional[sqlite3.Connection] = None) -> dict:
        season = conn.execute(
            "SELECT started_at, weeks, final_rank FROM war_seasons WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        weeks = conn.execute(
            """SELECT section_index, our_rank, our_fame, defense_fame, trophy_change
               FROM war_weeks WHERE season_id = ? ORDER BY section_index""",
            (season_id,),
        ).fetchall()
        participation = conn.execute(
            """SELECT COUNT(DISTINCT player_tag) AS players,
                      COALESCE(SUM(fame), 0) AS points,
                      COALESCE(SUM(decks_used), 0) AS decks_used
               FROM war_participation WHERE season_id = ?""",
            (season_id,),
        ).fetchone()
        phase = current.get("phase")
        # The weekly fame race is only RANKED once a clan has scored. On a
        # practice day every clan sits at 0 fame, so the API's rank is an
        # arbitrary tiebreaker order (not a real standing) — don't surface it.
        race_scored = any((s.get("fame") or 0) > 0 for s in (current.get("race_standings") or []))
        win_streak = get_week_win_streak(conn=conn)
        summary_bits = [f"Season {season_id}"]
        if current.get("section_index") is not None:
            summary_bits.append(f"week {int(current['section_index']) + 1}")
        if phase:
            summary_bits.append(str(phase))
        if current.get("race_rank") and race_scored:
            summary_bits.append(f"race rank {current['race_rank']}")
        elif not race_scored:
            summary_bits.append("race not yet ranked (no clan has scored)")
        if win_streak.get("streak"):
            summary_bits.append(f"{win_streak['streak']} straight weeks at #1")
        return {
            "season_id": season_id,
            "summary": ", ".join(summary_bits),
            "started_at": season["started_at"] if season else None,
            "last_observed_at": current.get("observed_at"),
            "week_win_streak": win_streak,
            "race_ranked": race_scored,
            "state": {
                "season_id": season_id,
                "week": current.get("week"),
                "phase": phase,
                "phase_display": current.get("phase_display") or phase,
                # get_current_war_status keys are battle_day_number / race_* /
                # fame — NOT day_number/standings/our_fame (the old keys here read
                # None/[] and emptied the whole live race block; QA H13/H14).
                "day_number": current.get("battle_day_number")
                if current.get("battle_day_number") is not None
                else current.get("practice_day_number"),
                "race": {
                    # Two separate races, never mixed (see get_current_war_status).
                    # Each race's rank is a phantom until that race has scored — on
                    # a practice day every clan sits at 0, so the API rank is an
                    # arbitrary tiebreaker order, NOT a standing. Null it (and the
                    # per-clan ranks) so the brain never cites "rank N" on an
                    # unranked race — matches _standing_block in the awareness read.
                    "primary_metric": current.get("primary_metric"),
                    "race_ranked": race_scored,
                    "race_rank": current.get("race_rank") if race_scored else None,
                    "race_standings": _strip_ranks(current.get("race_standings"), race_scored),
                    "fame": current.get("fame"),
                    "boat_scored": current.get("boat_scored"),
                    "day_rank": current.get("day_rank") if current.get("day_scored") else None,
                    "day_standings": _strip_ranks(
                        current.get("day_standings"), bool(current.get("day_scored"))
                    ),
                    "period_points": current.get("period_points"),
                    "day_scored": current.get("day_scored"),
                    "projected_day_fame": current.get("projected_day_fame"),
                    "finish_line": current.get("finish_line"),
                    "colosseum_week": current.get("colosseum_week"),
                },
                "participation_health": dict(participation) if participation else {},
                "season_summary": {
                    "weeks_recorded": len(weeks),
                    "week_trajectory": [dict(w) for w in weeks],
                },
                "active_risks": {},
                "recent_communications": [],
                "prior_cycle_comparison": {},
            },
        }

    return _build(conn=conn)


__all__ = [
    "DAILY_RANK_FAME",
    "FAME_FINISH_LINE",
    "FINAL_BATTLE_PERIOD_OFFSET",
    "FINAL_PRACTICE_PERIOD_OFFSET",
    "FIRST_BATTLE_PERIOD_OFFSET",
    "HOME_CLAN",
    "NORMAL_RIVER_RACE_FINISH_LINE",
    "build_war_now_context",
    "count_war_races_for_season",
    "get_current_season_id",
    "get_current_war_day_state",
    "get_current_war_status",
    "get_latest_clan_boat_defense_status",
    "get_latest_war_participant_snapshot_observed_at",
    "get_latest_war_race_finish_time",
    "get_season_window",
    "get_trophy_changes",
    "get_trophy_drops",
    "get_war_day_state",
    "get_war_deck_status_today",
    "get_war_history",
    "get_war_season_snapshot",
    "get_war_season_summary",
    "get_war_week_summary",
    "get_week_win_streak",
    "is_colosseum_week_confirmed",
    "is_war_section_finalized",
    "list_recent_war_day_summaries",
]
