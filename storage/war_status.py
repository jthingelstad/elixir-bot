from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import sqlite3

log = logging.getLogger("elixir_db")

from db import (
    _canon_tag,
    _current_joined_at,
    _member_reference_fields,
    _parse_cr_time,
    _rowdicts,
    managed_connection,
)
from storage.war_calendar import (
    FINAL_BATTLE_PERIOD_OFFSET,
    FINAL_PRACTICE_PERIOD_OFFSET,
    FIRST_BATTLE_PERIOD_OFFSET,
    coerce_utc_datetime,
    format_utc_iso,
    is_colosseum_week,
    normalize_period_type,
    phase_day_number,
    period_offset,
    resolve_phase,
    war_day_key,
    war_reset_window_utc,
)

LIVE_FINISH_TIME_SENTINEL = "19691231T235959.000Z"

from storage._formatting import format_member_reference as _format_member_reference


def _load_war_payload(raw_json) -> dict:
    if not raw_json:
        return {}
    if isinstance(raw_json, dict):
        return raw_json
    try:
        return json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        log.debug("_load_war_payload: failed to parse JSON (%d chars)", len(str(raw_json)))
        return {}


def _was_signal_sent_any_date(conn: sqlite3.Connection, signal_type: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM signal_log WHERE signal_type = ?",
        (signal_type,),
    ).fetchone() is not None


def _get_latest_logged_race(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
        "FROM war_races ORDER BY season_id DESC, section_index DESC, war_race_id DESC LIMIT 1"
    ).fetchone()


def _infer_current_season_id_from_live_state(payload: dict, latest_logged_race) -> Optional[int]:
    live_season_id = payload.get("seasonId")
    if live_season_id is not None:
        return live_season_id
    if not latest_logged_race:
        return None
    live_section_index = payload.get("sectionIndex")
    logged_section_index = latest_logged_race["section_index"]
    if (
        live_section_index is not None
        and logged_section_index is not None
        and live_section_index < logged_section_index
    ):
        return latest_logged_race["season_id"] + 1
    return latest_logged_race["season_id"]


def _normalize_period_type(period_type: Optional[str]) -> Optional[str]:
    return normalize_period_type(period_type)


def _period_offset(period_index: Optional[int]) -> Optional[int]:
    return period_offset(period_index)


def _resolve_phase(period_type: Optional[str], period_index: Optional[int]) -> Optional[str]:
    return resolve_phase(period_type, period_index)


def _phase_day_number(phase: Optional[str], period_index: Optional[int]) -> Optional[int]:
    return phase_day_number(phase, period_index)


def _resolve_live_race_rank(payload: dict, clan_tag: Optional[str]) -> Optional[int]:
    clans = payload.get("clans") or []
    canon_clan_tag = _canon_tag(clan_tag) if clan_tag else None
    if not clans or not canon_clan_tag:
        return None
    ranked = sorted(
        clans,
        key=lambda clan: (
            clan.get("fame") or 0,
            clan.get("repairPoints") or 0,
            clan.get("periodPoints") or 0,
            clan.get("clanScore") or 0,
        ),
        reverse=True,
    )
    for index, clan in enumerate(ranked, start=1):
        if _canon_tag(clan.get("tag")) == canon_clan_tag:
            return index
    return None


def _extract_race_standings(payload: dict, our_clan_tag: Optional[str]) -> list[dict]:
    """Extract ranked standings for all clans in the current race."""
    clans = payload.get("clans") or []
    if not clans:
        return []
    canon_our_tag = _canon_tag(our_clan_tag) if our_clan_tag else None
    ranked = sorted(
        clans,
        key=lambda clan: (
            clan.get("fame") or 0,
            clan.get("repairPoints") or 0,
            clan.get("periodPoints") or 0,
            clan.get("clanScore") or 0,
        ),
        reverse=True,
    )
    standings = []
    for rank, clan in enumerate(ranked, start=1):
        tag = _canon_tag(clan.get("tag"))
        standings.append({
            "rank": rank,
            "clan_tag": tag,
            "clan_name": clan.get("name"),
            "fame": clan.get("fame") or 0,
            "repair_points": clan.get("repairPoints") or 0,
            "period_points": clan.get("periodPoints") or 0,
            "clan_score": clan.get("clanScore") or 0,
            "is_us": tag == canon_our_tag,
        })
    return standings


def _usable_live_finish_time(value: Optional[str]) -> Optional[str]:
    finish_time = (value or "").strip()
    if not finish_time or finish_time == LIVE_FINISH_TIME_SENTINEL:
        return None
    return finish_time


def _finish_time_fields(finish_time: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    finish_time = _usable_live_finish_time(finish_time)
    if not finish_time:
        return None, None
    finish_dt = coerce_utc_datetime(finish_time)
    return finish_time, format_utc_iso(finish_dt) if finish_dt else None


def _same_week_trophy_change(latest_logged_race, season_id: Optional[int], section_index: Optional[int]):
    if (
        not latest_logged_race
        or season_id is None
        or section_index is None
        or latest_logged_race["season_id"] != season_id
        or latest_logged_race["section_index"] != section_index
    ):
        return None
    return latest_logged_race["trophy_change"]


def _trophy_stakes_fields(trophy_change) -> tuple[Optional[int], bool, Optional[str]]:
    if not isinstance(trophy_change, (int, float)):
        return None, False, None
    normalized = int(trophy_change)
    stakes = abs(normalized)
    if stakes <= 0:
        return normalized, True, None
    return normalized, True, f"{stakes} trophies on the line"


def _build_live_war_state(row, latest_logged_race) -> Optional[dict]:
    if not row:
        return None
    payload = _load_war_payload(row["raw_json"])
    result = dict(row)
    result.pop("raw_json", None)

    season_id = _infer_current_season_id_from_live_state(payload, latest_logged_race)
    section_index = payload.get("sectionIndex")
    period_index = payload.get("periodIndex")
    period_type = payload.get("periodType")
    phase = _resolve_phase(period_type, period_index)
    period_offset = _period_offset(period_index)
    clan_payload = payload.get("clan") or {}
    finish_time, race_completed_at = _finish_time_fields(clan_payload.get("finishTime"))

    if season_id is not None:
        result["season_id"] = season_id
    if section_index is not None:
        result["section_index"] = section_index
        result["week"] = section_index + 1
        result["trophy_change"] = _same_week_trophy_change(latest_logged_race, season_id, section_index)
    elif latest_logged_race and season_id == latest_logged_race["season_id"]:
        result["section_index"] = latest_logged_race["section_index"]
        result["week"] = (
            latest_logged_race["section_index"] + 1
            if latest_logged_race["section_index"] is not None
            else None
        )
        result["trophy_change"] = latest_logged_race["trophy_change"]

    colosseum = is_colosseum_week(period_type)
    result["period_index"] = period_index
    result["period_offset"] = period_offset
    result["period_type"] = period_type
    result["phase"] = phase
    result["colosseum_week"] = colosseum
    result["battle_phase_active"] = phase == "battle"
    result["practice_phase_active"] = phase == "practice"
    result["final_practice_day_active"] = (
        phase == "practice" and period_offset == FINAL_PRACTICE_PERIOD_OFFSET
    )
    result["final_battle_day_active"] = phase == "battle" and period_offset == FINAL_BATTLE_PERIOD_OFFSET
    result["battle_day_number"] = _phase_day_number(phase, period_index) if phase == "battle" else None
    result["battle_day_total"] = 4 if phase == "battle" else None
    result["practice_day_number"] = _phase_day_number(phase, period_index) if phase == "practice" else None
    result["practice_day_total"] = FIRST_BATTLE_PERIOD_OFFSET if phase == "practice" else None
    result["phase_display"] = (
        f"Battle Day {result['battle_day_number']}"
        if result["battle_day_number"] is not None
        else f"Practice Day {result['practice_day_number']}"
        if result["practice_day_number"] is not None
        else phase.title() if phase else None
    )
    result["season_week_label"] = (
        f"Season {season_id} Week {result['week']}"
        if season_id is not None and result.get("week") is not None
        else None
    )
    result["race_rank"] = _resolve_live_race_rank(payload, result.get("clan_tag")) or result.get("race_rank")
    result["race_standings"] = _extract_race_standings(payload, result.get("clan_tag"))
    result["period_logs_count"] = len(payload.get("periodLogs") or [])
    result["war_day_key"] = war_day_key(
        result.get("season_id"),
        result.get("section_index"),
        result.get("period_index"),
        row["observed_at"],
    )
    observed_at = coerce_utc_datetime(row["observed_at"])
    finish_dt = coerce_utc_datetime(finish_time)
    _, period_ends_at = war_reset_window_utc(observed_at or finish_dt or row["observed_at"])
    trophy_change, trophy_stakes_known, trophy_stakes_text = _trophy_stakes_fields(
        result.get("trophy_change")
    )
    result["finish_time"] = finish_time
    result["race_completed"] = bool(finish_time)
    result["race_completed_at"] = race_completed_at
    result["race_completed_early"] = bool(
        finish_dt and period_ends_at and finish_dt < period_ends_at
    )
    result["trophy_change"] = trophy_change
    result["trophy_stakes_known"] = trophy_stakes_known
    result["trophy_stakes_text"] = trophy_stakes_text
    return result


def _load_live_war_state_rows(rows, *, latest_logged_race) -> list[dict]:
    return [
        state
        for state in (
            _build_live_war_state(row, latest_logged_race)
            for row in rows
        )
        if state
    ]


@managed_connection
def get_recent_live_war_states(limit: int = 2, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    latest_logged_race = _get_latest_logged_race(conn)
    rows = conn.execute(
        "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
        "FROM war_current_state ORDER BY war_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return _load_live_war_state_rows(rows, latest_logged_race=latest_logged_race)


@managed_connection
def get_live_war_state_by_id(war_id: Optional[int], conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    if war_id is None:
        return None
    latest_logged_race = _get_latest_logged_race(conn)
    row = conn.execute(
        "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
        "FROM war_current_state WHERE war_id = ?",
        (int(war_id),),
    ).fetchone()
    return _build_live_war_state(row, latest_logged_race)


@managed_connection
def get_latest_live_war_state_id(conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
    row = conn.execute("SELECT MAX(war_id) AS war_id FROM war_current_state").fetchone()
    return row["war_id"] if row else None


@managed_connection
def get_previous_live_war_state_before(before_war_id: Optional[int], conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    if before_war_id is None:
        return None
    latest_logged_race = _get_latest_logged_race(conn)
    row = conn.execute(
        "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
        "FROM war_current_state WHERE war_id < ? ORDER BY war_id DESC LIMIT 1",
        (int(before_war_id),),
    ).fetchone()
    return _build_live_war_state(row, latest_logged_race)


@managed_connection
def list_live_war_states_after(after_war_id: Optional[int], conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    latest_logged_race = _get_latest_logged_race(conn)
    rows = conn.execute(
        "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
        "FROM war_current_state WHERE war_id > ? ORDER BY war_id ASC",
        (int(after_war_id or 0),),
    ).fetchall()
    return _load_live_war_state_rows(rows, latest_logged_race=latest_logged_race)


def get_current_war_status(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    states = get_recent_live_war_states(limit=1, conn=conn)
    return states[0] if states else None


def _format_duration_short(total_seconds: Optional[int]) -> Optional[str]:
    if total_seconds is None:
        return None
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fresh_time_left_seconds(war_day_state: dict, *, now=None) -> Optional[int]:
    """Seconds remaining in the current war day, re-anchored against wall clock.

    The CR API's stored ``time_left_seconds`` is only accurate at the moment we
    polled — it drifts within minutes. ``period_ends_at`` is anchored on the
    per-season ``finishTime`` so the true remaining time is
    ``period_ends_at - now`` regardless of how stale the last poll is.
    Falls back to the stored ``time_left_seconds`` when ``period_ends_at`` is
    unavailable.
    """
    ends_at = war_day_state.get("period_ends_at")
    if ends_at:
        try:
            ends_dt = datetime.fromisoformat(str(ends_at).replace("Z", "+00:00"))
            if ends_dt.tzinfo is None:
                ends_dt = ends_dt.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            remaining = int((ends_dt - current).total_seconds())
            return max(0, remaining)
        except (ValueError, TypeError):
            pass
    stored = war_day_state.get("time_left_seconds")
    if stored is None:
        return None
    return max(0, int(stored))


def is_colosseum_week_confirmed(
    period_type: Optional[str],
    trophy_change: Optional[int] = None,
    *,
    trophy_stakes_known: bool = False,
) -> bool:
    """True when we have positive evidence the current week is the colosseum
    (final) week of the season.

    Broader than ``storage.war_calendar.is_colosseum_week``, which only returns
    True on battle days (``periodType == "colosseum"``). This helper also
    catches colosseum-week practice days by cross-referencing the logged
    trophy stakes (±100 only occurs on the colosseum week).

    Kept permissive on inputs so both ``build_situation_time`` and
    ``build_war_now_context`` can share it.
    """
    if period_type == "colosseum":
        return True
    if trophy_stakes_known and abs(trophy_change or 0) == 100:
        return True
    return False


def _format_war_now_text(data: dict) -> str:
    parts = [f"Season {data['season_id']} · Week {data['week']}"]
    phase_with_total = data.get("phase_display")
    day_number = data.get("day_number")
    day_total = data.get("day_total")
    if phase_with_total and day_total:
        phase_with_total = f"{phase_with_total} of {day_total}"
        if day_number is not None:
            after_today = max(0, day_total - day_number)
            phase_word = "battle" if data.get("phase") == "battle" else "practice"
            if after_today > 0:
                more = "day" if after_today == 1 else "days"
                phase_with_total += f" (today + {after_today} more {phase_word} {more})"
    if phase_with_total:
        parts.append(phase_with_total)
    if data.get("is_colosseum_week"):
        parts.append("Colosseum (final week, 100 trophy stakes)")
    if data.get("is_final_battle_day"):
        parts.append("Final battle day")
    elif data.get("is_final_practice_day"):
        parts.append("Final practice day")

    lines = ["=== RIVER RACE — CURRENT MOMENT ===", " · ".join(parts)]
    if data.get("time_left_text"):
        lines.append(f"Period ends in {data['time_left_text']}")

    standings = data.get("race_standings") or []
    if standings:
        lines.append("Race standings:")
        for clan in standings:
            marker = " (us)" if clan.get("is_us") else ""
            lines.append(
                f"  {clan['rank']}. {clan.get('clan_name', '?')}{marker} | "
                f"{clan.get('fame', 0):,} fame"
            )
    return "\n".join(lines)


def build_war_now_context(conn: Optional[sqlite3.Connection] = None) -> tuple[Optional[dict], str]:
    """Single source of truth for 'what moment is it in the war' for LLM consumption.

    Returns (data, text). Returns (None, "") when there is no active war.

    The prompt builder and the get_river_race(engagement) tool both call this
    so the LLM sees identical, fresh time-left values on both surfaces. Field
    names align with ``build_situation_time`` (``is_colosseum_week``,
    ``is_final_battle_day``, ``is_final_practice_day``) so both LLM-facing
    blocks can be referenced by the same field list in subagent prompts.
    """
    status = get_current_war_status(conn=conn) or {}
    if not status or (status.get("war_state") or "").strip() == "notInWar":
        return None, ""
    # Day-state carries the period_ends_at anchor + fresh time fields; it
    # requires war_participant_snapshots, so it may be None early in a week.
    day_state = get_current_war_day_state(conn=conn) or {}

    period_type = status.get("period_type")
    phase = status.get("phase")
    if phase == "battle":
        day_number = status.get("battle_day_number")
        day_total = status.get("battle_day_total")
    else:
        day_number = status.get("practice_day_number")
        day_total = status.get("practice_day_total")

    fresh_seconds = _fresh_time_left_seconds(day_state) if day_state else None
    time_left_text = _format_duration_short(fresh_seconds)

    colosseum_week = is_colosseum_week_confirmed(
        period_type,
        status.get("trophy_change"),
        trophy_stakes_known=bool(status.get("trophy_stakes_known")),
    )

    # day_total is included while now_text is built ("Battle Day 2 of 4"
    # reads naturally), then dropped from the LLM-facing dict so the model
    # can't derive "days left" from day_total - day_number. See heartbeat
    # _war.build_situation_time for the same rationale.
    data = {
        "season_id": status.get("season_id"),
        "week": status.get("week"),
        "phase": phase,
        "phase_display": status.get("phase_display"),
        "day_number": day_number,
        "day_total": day_total,
        "period_type": period_type,
        "time_left_seconds": fresh_seconds,
        "time_left_text": time_left_text,
        "period_started_at": day_state.get("period_started_at"),
        "period_ends_at": day_state.get("period_ends_at"),
        "is_colosseum_week": bool(colosseum_week),
        "is_final_battle_day": bool(status.get("final_battle_day_active", False)),
        "is_final_practice_day": bool(status.get("final_practice_day_active", False)),
        "race_standings": status.get("race_standings") or [],
    }
    data["now_text"] = _format_war_now_text(data)
    data.pop("day_total", None)
    return data, data["now_text"]


def _decorate_participant(conn, row: dict, *, fame_today: Optional[int] = None) -> dict:
    item = {
        "tag": row.get("player_tag") or row.get("tag"),
        "name": row.get("player_name") or row.get("name"),
        "fame": row.get("fame") or 0,
        "repair_points": row.get("repair_points") or 0,
        "boat_attacks": row.get("boat_attacks") or 0,
        "decks_used_total": row.get("decks_used_total") or 0,
        "decks_used_today": row.get("decks_used_today") or 0,
    }
    if fame_today is not None:
        item["fame_today"] = fame_today
    member_id = row.get("member_id")
    if member_id:
        item = _member_reference_fields(conn, member_id, item)
    else:
        item["member_ref"] = item.get("name") or item.get("tag")
    return item


@managed_connection
def _get_live_state_for_war_day(
    war_day_key: Optional[str],
    *,
    newest: bool = True,
    observed_at: Optional[str] = None,
    conn=None,
) -> Optional[dict]:
    if not war_day_key:
        return None
    latest_logged_race = _get_latest_logged_race(conn)
    order = "DESC" if newest else "ASC"
    params = []
    where = []
    if observed_at:
        where.append("observed_at <= ?")
        params.append(observed_at)
    rows = conn.execute(
        "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
        f"FROM war_current_state {'WHERE ' + ' AND '.join(where) if where else ''} "
        f"ORDER BY war_id {order} LIMIT 500",
        tuple(params),
    ).fetchall()
    for row in rows:
        state = _build_live_war_state(row, latest_logged_race)
        if state and state.get("war_day_key") == war_day_key:
            return state
    return None


@managed_connection
def get_war_day_state(war_day_key: Optional[str] = None, observed_at: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    current_state = None
    if war_day_key is None:
        current_state = get_current_war_status(conn=conn)
        war_day_key = (current_state or {}).get("war_day_key")
    if not war_day_key:
        return None

    if current_state is None or current_state.get("war_day_key") != war_day_key:
        current_state = _get_live_state_for_war_day(war_day_key, observed_at=observed_at, conn=conn)
    first_state = _get_live_state_for_war_day(war_day_key, newest=False, observed_at=observed_at, conn=conn)

    bounds_where = ["war_day_key = ?"]
    bounds_params = [war_day_key]
    if observed_at:
        bounds_where.append("observed_at <= ?")
        bounds_params.append(observed_at)
    bounds = conn.execute(
        "SELECT MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at "
        f"FROM war_participant_snapshots WHERE {' AND '.join(bounds_where)}",
        tuple(bounds_params),
    ).fetchone()
    if not bounds or not bounds["last_observed_at"]:
        return None

    first_observed_at = bounds["first_observed_at"]
    last_observed_at = bounds["last_observed_at"]
    first_rows = conn.execute(
        "SELECT s.member_id, s.player_tag, s.player_name, s.fame, s.repair_points, s.boat_attacks, "
        "s.decks_used_total, s.decks_used_today "
        "FROM war_participant_snapshots s "
        "WHERE s.war_day_key = ? AND s.observed_at = ?",
        (war_day_key, first_observed_at),
    ).fetchall()
    latest_rows = conn.execute(
        "SELECT s.member_id, s.player_tag, s.player_name, s.fame, s.repair_points, s.boat_attacks, "
        "s.decks_used_total, s.decks_used_today, m.status "
        "FROM war_participant_snapshots s "
        "LEFT JOIN members m ON m.member_id = s.member_id "
        "WHERE s.war_day_key = ? AND s.observed_at = ?",
        (war_day_key, last_observed_at),
    ).fetchall()

    first_by_tag = {
        _canon_tag(row["player_tag"]): dict(row)
        for row in first_rows
        if row["player_tag"]
    }
    participants = []
    eligible_rows = []
    used_all = []
    used_some = []
    used_none = []
    top_fame_today = []
    top_fame_total = []

    for row in latest_rows:
        raw = dict(row)
        tag = _canon_tag(raw.get("player_tag"))
        start = first_by_tag.get(tag) or {}
        fame_today = max(0, (raw.get("fame") or 0) - (start.get("fame") or 0))
        item = _decorate_participant(conn, raw, fame_today=fame_today)
        participants.append(item)
        top_fame_total.append(item)
        top_fame_today.append(item)
        if raw.get("status") == "active" or raw.get("status") is None:
            eligible_rows.append(item)
            decks_today = item.get("decks_used_today") or 0
            if decks_today >= 4:
                used_all.append(item)
            elif decks_today > 0:
                used_some.append(item)
            else:
                used_none.append(item)

    top_fame_today.sort(
        key=lambda item: (-(item.get("fame_today") or 0), -(item.get("fame") or 0), (item.get("name") or "").lower())
    )
    top_fame_total.sort(
        key=lambda item: (-(item.get("fame") or 0), -(item.get("decks_used_total") or 0), (item.get("name") or "").lower())
    )

    observed_at = coerce_utc_datetime((current_state or {}).get("observed_at") or last_observed_at)
    # The first time we observed this (season, section, period) triple is the
    # most accurate anchor for the 24h period window. CR River Race seasons
    # can skew off the nominal 10:00 UTC reset, so trusting the observed
    # period-start beats the hardcoded hour. Fall back to war_reset_window_utc
    # only when we have no snapshot yet for this period.
    first_observed_dt = coerce_utc_datetime(first_observed_at)
    if first_observed_dt is not None:
        started_at = first_observed_dt
        ends_at = first_observed_dt + timedelta(days=1)
    else:
        reset_anchor = (
            (current_state or {}).get("observed_at")
            or (first_state or {}).get("observed_at")
            or last_observed_at
        )
        started_at, ends_at = war_reset_window_utc(reset_anchor)
    time_left_seconds = int((ends_at - observed_at).total_seconds()) if ends_at and observed_at else None
    if time_left_seconds is not None:
        time_left_seconds = max(0, time_left_seconds)

    phase = (current_state or first_state or {}).get("phase")
    battle_day_number = (current_state or first_state or {}).get("battle_day_number")
    practice_day_number = (current_state or first_state or {}).get("practice_day_number")
    day_number = battle_day_number if battle_day_number is not None else practice_day_number

    return {
        "war_day_key": war_day_key,
        "season_id": (current_state or first_state or {}).get("season_id"),
        "section_index": (current_state or first_state or {}).get("section_index"),
        "week": (current_state or first_state or {}).get("week"),
        "period_index": (current_state or first_state or {}).get("period_index"),
        "period_type": (current_state or first_state or {}).get("period_type"),
        "phase": phase,
        "phase_display": (current_state or first_state or {}).get("phase_display"),
        "day_number": day_number,
        "day_total": (
            (current_state or first_state or {}).get("battle_day_total")
            if phase == "battle"
            else (current_state or first_state or {}).get("practice_day_total")
        ),
        "race_rank": (current_state or first_state or {}).get("race_rank"),
        "clan_fame": (current_state or first_state or {}).get("fame"),
        "clan_score": (current_state or first_state or {}).get("clan_score"),
        "period_points": (current_state or first_state or {}).get("period_points"),
        "finish_time": (current_state or first_state or {}).get("finish_time"),
        "race_completed": (current_state or first_state or {}).get("race_completed"),
        "race_completed_at": (current_state or first_state or {}).get("race_completed_at"),
        "race_completed_early": (current_state or first_state or {}).get("race_completed_early"),
        "trophy_change": (current_state or first_state or {}).get("trophy_change"),
        "trophy_stakes_known": (current_state or first_state or {}).get("trophy_stakes_known"),
        "trophy_stakes_text": (current_state or first_state or {}).get("trophy_stakes_text"),
        "observed_at": (current_state or first_state or {}).get("observed_at") or last_observed_at,
        "first_observed_at": first_observed_at,
        "last_observed_at": last_observed_at,
        "period_started_at": format_utc_iso(started_at),
        "period_ends_at": format_utc_iso(ends_at),
        "time_left_seconds": time_left_seconds,
        "time_left_text": _format_duration_short(time_left_seconds),
        "total_participants": len(eligible_rows),
        "engaged_count": len(used_all) + len(used_some),
        "finished_count": len(used_all),
        "untouched_count": len(used_none),
        "used_all_4": used_all,
        "used_some": used_some,
        "used_none": used_none,
        "top_fame_today": top_fame_today[:5],
        "top_fame_total": top_fame_total[:5],
        "participants": participants,
    }


def get_current_war_day_state(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    return get_war_day_state(None, conn=conn)


@managed_connection
def list_war_day_keys(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT war_day_key FROM war_participant_snapshots ORDER BY war_day_key ASC"
    ).fetchall()
    return [row["war_day_key"] for row in rows if row["war_day_key"]]


@managed_connection
def get_latest_war_participant_snapshot_observed_at(war_day_key: str, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(observed_at) AS observed_at FROM war_participant_snapshots WHERE war_day_key = ?",
        (war_day_key,),
    ).fetchone()
    return row["observed_at"] if row else None


@managed_connection
def get_previous_war_participant_snapshot_observed_at(war_day_key: str, before_observed_at: str, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    row = conn.execute(
        "SELECT observed_at FROM war_participant_snapshots "
        "WHERE war_day_key = ? AND observed_at < ? "
        "ORDER BY observed_at DESC LIMIT 1",
        (war_day_key, before_observed_at),
    ).fetchone()
    return row["observed_at"] if row else None


@managed_connection
def list_war_participant_snapshot_times_after(war_day_key: str, after_observed_at: str, conn: Optional[sqlite3.Connection] = None) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT observed_at FROM war_participant_snapshots "
        "WHERE war_day_key = ? AND observed_at > ? "
        "ORDER BY observed_at ASC",
        (war_day_key, after_observed_at),
    ).fetchall()
    return [row["observed_at"] for row in rows if row["observed_at"]]


@managed_connection
def get_war_participant_snapshot_group(war_day_key: str, observed_at: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT member_id, player_tag, player_name, fame, repair_points, boat_attacks, "
        "decks_used_total, decks_used_today, phase, phase_day_number "
        "FROM war_participant_snapshots "
        "WHERE war_day_key = ? AND observed_at = ? "
        "ORDER BY player_name COLLATE NOCASE, player_tag ASC",
        (war_day_key, observed_at),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def list_recent_war_day_summaries(limit: int = 7, phase: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    where = []
    params = []
    if phase:
        where.append("phase = ?")
        params.append(phase)
    rows = conn.execute(
        "SELECT war_day_key, MAX(observed_at) AS observed_at "
        "FROM war_participant_snapshots "
        f"{'WHERE ' + ' AND '.join(where) if where else ''} "
        "GROUP BY war_day_key ORDER BY observed_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    summaries = []
    for row in rows:
        state = get_war_day_state(row["war_day_key"], conn=conn)
        if state:
            summaries.append(state)
    return summaries


@managed_connection
def get_war_week_summary(season_id: Optional[int] = None, section_index: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    current = get_current_war_status(conn=conn)
    if season_id is None:
        season_id = (current or {}).get("season_id")
    if section_index is None:
        section_index = (current or {}).get("section_index")
    if season_id is None or section_index is None:
        return None

    race = conn.execute(
        "SELECT war_race_id, season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
        "FROM war_races WHERE season_id = ? AND section_index = ?",
        (season_id, section_index),
    ).fetchone()
    top_participants = []
    participant_count = 0
    if race:
        participant_rows = conn.execute(
            "SELECT member_id, player_tag, player_name, fame, repair_points, boat_attacks, decks_used "
            "FROM war_participation WHERE war_race_id = ? "
            "ORDER BY COALESCE(fame, 0) DESC, COALESCE(decks_used, 0) DESC, player_name COLLATE NOCASE",
            (race["war_race_id"],),
        ).fetchall()
        participant_count = len(participant_rows)
        for row in participant_rows[:5]:
            item = {
                "tag": row["player_tag"],
                "name": row["player_name"],
                "fame": row["fame"] or 0,
                "repair_points": row["repair_points"] or 0,
                "boat_attacks": row["boat_attacks"] or 0,
                "decks_used": row["decks_used"] or 0,
            }
            if row["member_id"]:
                item = _member_reference_fields(conn, row["member_id"], item)
            top_participants.append(item)

    day_rows = conn.execute(
        "SELECT DISTINCT war_day_key FROM war_participant_snapshots "
        "WHERE season_id = ? AND section_index = ? ORDER BY observed_at DESC",
        (season_id, section_index),
    ).fetchall()
    day_summaries = []
    for row in day_rows:
        state = get_war_day_state(row["war_day_key"], conn=conn)
        if state:
            day_summaries.append({
                "war_day_key": state["war_day_key"],
                "phase": state["phase"],
                "phase_display": state["phase_display"],
                "engaged_count": state["engaged_count"],
                "finished_count": state["finished_count"],
                "top_fame_today": state["top_fame_today"][:3],
            })

    return {
        "season_id": season_id,
        "section_index": section_index,
        "week": section_index + 1,
        "race": dict(race) if race else None,
        "participant_count": participant_count,
        "top_participants": top_participants,
        "day_summaries": day_summaries,
    }


@managed_connection
def get_war_season_story(season_id: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    from storage.war_analytics import get_perfect_war_participants

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    season_summary = get_war_season_summary(season_id=season_id, top_n=10, conn=conn)
    if not season_summary:
        return None
    week_rows = conn.execute(
        "SELECT section_index FROM war_races WHERE season_id = ? ORDER BY section_index ASC",
        (season_id,),
    ).fetchall()
    weeks = [get_war_week_summary(season_id=season_id, section_index=row["section_index"], conn=conn) for row in week_rows]
    weeks = [week for week in weeks if week]
    return {
        "season_id": season_id,
        "weeks": weeks,
        "season_summary": season_summary,
        "perfect_participants": get_perfect_war_participants(season_id=season_id, conn=conn),
    }


@managed_connection
def get_latest_clan_boat_defense_status(clan_tag: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    current = get_current_war_status(conn=conn)
    canon_clan_tag = _canon_tag(clan_tag or (current or {}).get("clan_tag"))
    if not canon_clan_tag:
        return None

    where = ["clan_tag = ?", "num_defenses_remaining IS NOT NULL"]
    params = [canon_clan_tag]
    if current and current.get("season_id") is not None:
        where.append("season_id = ?")
        params.append(current["season_id"])

    row = conn.execute(
        "SELECT season_id, section_index, period_index, period_offset, clan_tag, clan_name, "
        "points_earned, progress_start_of_day, progress_end_of_day, end_of_day_rank, progress_earned, "
        "num_defenses_remaining, progress_earned_from_defenses, observed_at, raw_json "
        f"FROM war_period_clan_status WHERE {' AND '.join(where)} "
        "ORDER BY section_index DESC, period_index DESC, observed_at DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    if not row:
        return None

    result = dict(row)
    phase = _resolve_phase(None, result.get("period_index"))
    result["phase"] = phase
    result["week"] = (
        result["section_index"] + 1
        if result.get("section_index") is not None
        else None
    )
    result["battle_day_number"] = (
        _phase_day_number("battle", result.get("period_index"))
        if phase == "battle"
        else None
    )
    result["practice_day_number"] = (
        _phase_day_number("practice", result.get("period_index"))
        if phase == "practice"
        else None
    )
    result["phase_display"] = (
        f"Battle Day {result['battle_day_number']}"
        if result["battle_day_number"] is not None
        else f"Practice Day {result['practice_day_number']}"
        if result["practice_day_number"] is not None
        else None
    )
    if current:
        result["current_week_match"] = (
            current.get("season_id") == result.get("season_id")
            and current.get("section_index") == result.get("section_index")
        )
    else:
        result["current_week_match"] = None
    return result

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
        "clan_fame": state.get("clan_fame"),
        "clan_score": state.get("clan_score"),
        "period_points": state.get("period_points"),
        "used_all_4": state.get("used_all_4") or [],
        "used_some": state.get("used_some") or [],
        "used_none": state.get("used_none") or [],
        "top_fame_today": state.get("top_fame_today") or [],
        "top_fame_total": state.get("top_fame_total") or [],
        "engaged_count": state.get("engaged_count") or 0,
        "finished_count": state.get("finished_count") or 0,
        "untouched_count": state.get("untouched_count") or 0,
        "total_participants": state.get("total_participants") or 0,
    }

@managed_connection
def get_war_season_summary(season_id: Optional[int] = None, top_n: int = 5, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
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
        "FROM war_races WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    top = get_war_champ_standings(season_id=season_id, conn=conn)[:top_n]
    nonparticipants = get_members_without_war_participation(season_id=season_id, conn=conn)["members"]
    active_members = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE status = 'active'"
    ).fetchone()["cnt"]
    return {
        "season_id": season_id,
        "races": total_races["cnt"],
        "total_clan_fame": total_races["total_clan_fame"] or 0,
        "fame_per_active_member": round((total_races["total_clan_fame"] or 0) / active_members, 2) if active_members else 0,
        "top_contributors": top,
        "nonparticipants": nonparticipants,
    }

@managed_connection
def get_trophy_drops(days: int = 7, min_drop: int = 100, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT m.player_tag AS tag, m.current_name AS name, "
        "MIN(dm.trophies) AS min_trophies, MAX(dm.trophies) AS max_trophies, "
        "MAX(dm.metric_date) AS latest_metric_date, "
        "(MAX(dm.trophies) - MIN(dm.trophies)) AS spread "
        "FROM member_daily_metrics dm "
        "JOIN members m ON m.member_id = dm.member_id "
        "WHERE dm.metric_date >= ? AND m.status = 'active' "
        "GROUP BY dm.member_id "
        "HAVING spread >= ? "
        "ORDER BY spread DESC",
        (cutoff, min_drop),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["drop"] = item.pop("spread")
        result.append(item)
    return result

@managed_connection
def get_trophy_changes(since_hours: int = 24, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT m.player_tag AS tag, s.name, s.trophies, s.observed_at,
                ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at ASC) AS rn_asc,
                ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn_desc,
                s.member_id
            FROM member_state_snapshots s
            JOIN members m ON m.member_id = s.member_id
            WHERE s.observed_at >= ?
        )
        SELECT a.tag, a.name,
               a.trophies AS old_trophies,
               b.trophies AS new_trophies,
               (b.trophies - a.trophies) AS change
        FROM ranked a
        JOIN ranked b ON a.member_id = b.member_id
        WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
        ORDER BY ABS(change) DESC
        """,
        (cutoff,),
    ).fetchall()
    return _rowdicts(rows)

@managed_connection
def detect_milestones(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT s.*, m.player_tag AS tag,
                ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
            FROM member_state_snapshots s
            JOIN members m ON m.member_id = s.member_id
        )
        SELECT a.tag, a.name,
               b.trophies AS old_trophies, a.trophies AS new_trophies,
               b.arena_name AS old_arena, a.arena_name AS new_arena,
               a.observed_at AS observed_at
        FROM ranked a
        JOIN ranked b ON a.member_id = b.member_id
        WHERE a.rn = 1 AND b.rn = 2
        """
    ).fetchall()
    milestones = []
    for row in rows:
        if row["old_arena"] and row["new_arena"] and row["old_arena"] != row["new_arena"]:
            signal_log_type = (
                f"arena_change:{row['tag']}:{row['old_arena']}->{row['new_arena']}:{row['observed_at']}"
            )
            if _was_signal_sent_any_date(conn, signal_log_type):
                continue
            milestones.append({
                "tag": row["tag"],
                "name": row["name"],
                "type": "arena_change",
                "old_value": row["old_arena"],
                "new_value": row["new_arena"],
                "signal_log_type": signal_log_type,
            })
    return milestones

@managed_connection
def detect_role_changes(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT s.*, m.player_tag AS tag,
                ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
            FROM member_state_snapshots s
            JOIN members m ON m.member_id = s.member_id
        )
        SELECT a.tag, a.name, b.role AS old_role, a.role AS new_role, a.observed_at AS observed_at
        FROM ranked a
        JOIN ranked b ON a.member_id = b.member_id
        WHERE a.rn = 1 AND b.rn = 2 AND COALESCE(a.role, '') != COALESCE(b.role, '')
        """
    ).fetchall()
    changes = []
    for row in rows:
        change = dict(row)
        signal_log_type = (
            f"role_change:{row['tag']}:{row['old_role']}->{row['new_role']}:{row['observed_at']}"
        )
        if _was_signal_sent_any_date(conn, signal_log_type):
            continue
        change["signal_log_type"] = signal_log_type
        changes.append(change)
    return changes

@managed_connection
def get_war_history(n: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT war_race_id AS id, season_id, section_index, our_rank, our_fame, finish_time, created_date, raw_json AS standings_json FROM war_races ORDER BY created_date DESC LIMIT ?",
        (n,),
    ).fetchall()
    return _rowdicts(rows)

def get_current_season_id(conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
    current = get_current_war_status(conn=conn)
    return current.get("season_id") if current else None

def _season_bounds(conn: sqlite3.Connection, season_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(created_date) AS start_date, MAX(created_date) AS end_date "
        "FROM war_races WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    if not row or not row["start_date"] or not row["end_date"]:
        return None, None
    start_dt = _parse_cr_time(row["start_date"])
    end_dt = _parse_cr_time(row["end_date"])
    if not start_dt or not end_dt:
        return None, None
    end_dt = end_dt + timedelta(days=7)
    return start_dt.strftime("%Y%m%dT%H%M%S.000Z"), end_dt.strftime("%Y%m%dT%H%M%S.000Z")
