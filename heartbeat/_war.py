"""heartbeat._war — War calendar event detectors."""

import logging
from datetime import datetime

import requests

import cr_api
import db
from heartbeat._helpers import (
    BATTLE_DAY_SECONDS,
    _BATTLE_DAY_CHECKPOINTS,
    _COLOSSEUM_FAME_TARGET,
    _RACE_FAME_TARGET,
    _battle_lead_payload,
    _completed_war_races,
    _compute_pace_status,
    _war_period_signal_log_type,
    _war_signal_date_for_state,
    _war_signal_date_for_values,
)
from storage.war_calendar import is_colosseum_week

log = logging.getLogger("elixir_heartbeat")


def _detect_war_day_transition_for_pair(current, previous=None, *, now=None, conn=None):
    """Detect API-native war phase transitions and notable phase states."""
    if not current:
        return []
    now = now or datetime.now()
    signals = []
    latest_clan_defense_status = db.get_latest_clan_boat_defense_status(conn=conn)
    current_signal_date = _war_signal_date_for_state(current, now)
    previous_signal_date = _war_signal_date_for_state(previous, now) if previous else current_signal_date

    if current.get("battle_phase_active") and (
        previous is None or not previous.get("battle_phase_active")
    ):
        signal_log_type = _war_period_signal_log_type("war_battle_phase_active", current)
        if not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
            signals.append({
                "type": "war_battle_phase_active",
                "signal_log_type": signal_log_type,
                "signal_date": current_signal_date,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
                "message": "Battle phase is live. Time to use those war decks.",
            })
    colosseum_week = current.get("colosseum_week", False)
    if current.get("practice_phase_active") and (
        previous is None or not previous.get("practice_phase_active")
    ):
        signal_log_type = _war_period_signal_log_type("war_practice_phase_active", current)
        if not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
            practice_signal = {
                "type": "war_practice_phase_active",
                "signal_log_type": signal_log_type,
                "signal_date": current_signal_date,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
                "colosseum_week": colosseum_week,
            }
            if colosseum_week:
                practice_signal["message"] = (
                    "Practice phase is live for Colosseum week — the final week of the season. "
                    "There are no boat defenses this week. Focus on preparing decks for battle."
                )
            else:
                practice_signal.update({
                    "boat_defense_setup_scope": "one_time_per_practice_week",
                    "boat_defense_tracking_available": False,
                    "latest_clan_defense_status": latest_clan_defense_status,
                    "boat_defense_tracking_note": (
                        "The live River Race API does not expose which members have placed "
                        "boat defenses. It only exposes clan-level defense performance in "
                        "period logs after days are logged."
                    ),
                    "message": (
                        "Practice phase is live. Boat defenses are a one-time setup during "
                        "practice days, so get them in early before battle days."
                    ),
                })
            signals.append(practice_signal)
    if current.get("final_practice_day_active"):
        signal_log_type = _war_period_signal_log_type("war_final_practice_day", current)
        if not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
            final_practice_signal = {
                "type": "war_final_practice_day",
                "signal_log_type": signal_log_type,
                "signal_date": current_signal_date,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
                "colosseum_week": colosseum_week,
            }
            if colosseum_week:
                final_practice_signal["message"] = (
                    "Last day of practice for Colosseum week. No boat defenses to set — "
                    "get decks ready for the final battles of the season."
                )
            else:
                final_practice_signal.update({
                    "boat_defense_setup_scope": "one_time_per_practice_week",
                    "boat_defense_tracking_available": False,
                    "latest_clan_defense_status": latest_clan_defense_status,
                    "boat_defense_tracking_note": (
                        "The live River Race API does not expose which members have placed "
                        "boat defenses. It only exposes clan-level defense performance in "
                        "period logs after days are logged."
                    ),
                    "message": (
                        "Last day of practice this week. Boat defenses are a one-time setup, "
                        "so make sure they are set before battle days start."
                    ),
                })
            signals.append(final_practice_signal)
    if current.get("final_battle_day_active"):
        signal_log_type = _war_period_signal_log_type("war_final_battle_day", current)
        if not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
            signals.append({
                "type": "war_final_battle_day",
                "signal_log_type": signal_log_type,
                "signal_date": current_signal_date,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
                "message": "Last day of battles this week. Use remaining decks!",
            })
    if (
        previous
        and previous.get("battle_phase_active")
        and not current.get("battle_phase_active")
    ):
        signal_log_type = _war_period_signal_log_type("war_battle_days_complete", previous)
        if not db.was_signal_sent(signal_log_type, previous_signal_date, conn=conn):
            signals.append({
                "type": "war_battle_days_complete",
                "signal_log_type": signal_log_type,
                "signal_date": previous_signal_date,
                "previous_season_id": previous.get("season_id"),
                "season_id": current.get("season_id"),
                "previous_week": previous.get("week"),
                "week": current.get("week"),
                "previous_period_type": previous.get("period_type"),
                "period_type": current.get("period_type"),
                "message": "Battle phase has ended. River Race has moved out of battle days.",
            })

    return signals


def detect_war_day_transition(now=None, conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if not states:
        return []
    current = states[0]
    previous = states[1] if len(states) > 1 else None
    return _detect_war_day_transition_for_pair(current, previous, now=now, conn=conn)


def detect_war_rollovers(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []
    return _detect_war_rollovers_for_pair(states[0], states[1], conn=conn)


def _detect_war_rollovers_for_pair(current, previous, conn=None):
    """Detect live war week and season rollovers from consecutive snapshots."""
    if not current or not previous:
        return []
    if current["war_state"] in (None, "notInWar") or previous["war_state"] in (None, "notInWar"):
        return []

    current_section_index = current.get("section_index")
    previous_section_index = previous.get("section_index")
    if current_section_index is None or previous_section_index is None:
        return []
    if current_section_index == previous_section_index:
        return []

    current_season_id = current.get("season_id")
    previous_season_id = previous.get("season_id")

    signals = [{
        "type": "war_week_rollover",
        "previous_section_index": previous_section_index,
        "section_index": current_section_index,
        "previous_week": previous.get("week"),
        "week": current.get("week"),
        "previous_season_id": previous_season_id,
        "season_id": current_season_id,
        "season_changed": current_season_id != previous_season_id,
        "war_state": current["war_state"],
        "period_type": current.get("period_type"),
        "period_index": current.get("period_index"),
        "observed_at": current["observed_at"],
        "fame": current["fame"],
        "repair_points": current["repair_points"],
        "period_points": current["period_points"],
        "clan_score": current["clan_score"],
        "message": (
            f"War week rollover detected: season {current_season_id if current_season_id is not None else '?'} "
            f"week {current.get('week') if current.get('week') is not None else '?'} is now live."
        ),
    }]

    if (
        previous_season_id is not None
        and current_season_id is not None
        and current_season_id != previous_season_id
    ) or current_section_index < previous_section_index:
        signals.append({
            "type": "war_season_rollover",
            "previous_season_id": previous_season_id,
            "season_id": current_season_id,
            "previous_week": previous.get("week"),
            "week": current.get("week"),
            "war_state": current["war_state"],
            "period_type": current.get("period_type"),
            "period_index": current.get("period_index"),
            "observed_at": current["observed_at"],
            "fame": current["fame"],
            "repair_points": current["repair_points"],
            "period_points": current["period_points"],
            "clan_score": current["clan_score"],
            "message": (
                f"War season rollover detected: season "
                f"{current_season_id if current_season_id is not None else '?'} has started."
            ),
        })

    return signals


def detect_war_day_markers(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if not states:
        return []
    current = states[0]
    previous = states[1] if len(states) > 1 else None
    return _detect_war_day_markers_for_pair(current, previous, conn=conn)


def _detect_war_day_markers_for_pair(current, previous=None, conn=None):
    if not current:
        return []
    signals = []

    current_key = current.get("war_day_key")
    previous_key = previous.get("war_day_key") if previous else None
    if current_key and current_key != previous_key:
        current_day = db.get_war_day_state(current_key, observed_at=current.get("observed_at"), conn=conn)
        if current_day:
            current_signal_date = _war_signal_date_for_state(current_day, current)
            if current.get("phase") == "practice":
                if (current_day.get("day_number") or 0) <= 1:
                    signal_log_type = None
                else:
                    signal_log_type = _war_period_signal_log_type("war_practice_day_started", current)
                if signal_log_type and not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
                    signals.append({
                        "type": "war_practice_day_started",
                        "signal_log_type": signal_log_type,
                        "signal_date": current_signal_date,
                        "season_id": current_day.get("season_id"),
                        "week": current_day.get("week"),
                        "phase": current_day.get("phase"),
                        "phase_display": current_day.get("phase_display"),
                        "day_number": current_day.get("day_number"),
                        "day_total": current_day.get("day_total"),
                        "time_left_seconds": current_day.get("time_left_seconds"),
                        "time_left_text": current_day.get("time_left_text"),
                    })
            elif current.get("phase") == "battle":
                if (current_day.get("day_number") or 0) <= 1:
                    signal_log_type = None
                else:
                    signal_log_type = _war_period_signal_log_type("war_battle_day_started", current)
                if signal_log_type and not db.was_signal_sent(signal_log_type, current_signal_date, conn=conn):
                    signals.append({
                        "type": "war_battle_day_started",
                        "signal_log_type": signal_log_type,
                        "signal_date": current_signal_date,
                        "season_id": current_day.get("season_id"),
                        "week": current_day.get("week"),
                        "phase": current_day.get("phase"),
                        "phase_display": current_day.get("phase_display"),
                        "day_number": current_day.get("day_number"),
                        "day_total": current_day.get("day_total"),
                        "race_rank": current_day.get("race_rank"),
                        "clan_fame": current_day.get("clan_fame"),
                        "clan_score": current_day.get("clan_score"),
                        "time_left_seconds": current_day.get("time_left_seconds"),
                        "time_left_text": current_day.get("time_left_text"),
                        "top_fame_total": current_day.get("top_fame_total") or [],
                        **_battle_lead_payload(current_day.get("race_rank"), war_state=current_day),
                    })

    if previous and previous_key and current_key != previous_key:
        previous_day = db.get_war_day_state(previous_key, observed_at=previous.get("observed_at"), conn=conn)
        if previous_day:
            completed_at = current.get("observed_at")
            previous_signal_date = _war_signal_date_for_state(previous_day, previous, completed_at)
            if previous.get("phase") == "practice":
                signal_log_type = _war_period_signal_log_type("war_practice_day_complete", previous)
                if not db.was_signal_sent(signal_log_type, previous_signal_date, conn=conn):
                    signals.append({
                        "type": "war_practice_day_complete",
                        "signal_log_type": signal_log_type,
                        "signal_date": previous_signal_date,
                        "season_id": previous_day.get("season_id"),
                        "week": previous_day.get("week"),
                        "phase_display": previous_day.get("phase_display"),
                        "day_number": previous_day.get("day_number"),
                        "day_total": previous_day.get("day_total"),
                        "completed_at": completed_at,
                        "latest_clan_defense_status": db.get_latest_clan_boat_defense_status(conn=conn),
                    })
            elif previous.get("phase") == "battle":
                signal_log_type = _war_period_signal_log_type("war_battle_day_complete", previous)
                if not db.was_signal_sent(signal_log_type, previous_signal_date, conn=conn):
                    signals.append({
                        "type": "war_battle_day_complete",
                        "signal_log_type": signal_log_type,
                        "signal_date": previous_signal_date,
                        "season_id": previous_day.get("season_id"),
                        "week": previous_day.get("week"),
                        "phase_display": previous_day.get("phase_display"),
                        "day_number": previous_day.get("day_number"),
                        "day_total": previous_day.get("day_total"),
                        "completed_at": completed_at,
                        "race_rank": previous_day.get("race_rank"),
                        "clan_fame": previous_day.get("clan_fame"),
                        "clan_score": previous_day.get("clan_score"),
                        "engaged_count": previous_day.get("engaged_count"),
                        "finished_count": previous_day.get("finished_count"),
                        "untouched_count": previous_day.get("untouched_count"),
                        "used_all_4": previous_day.get("used_all_4") or [],
                        "used_some": previous_day.get("used_some") or [],
                        "used_none": previous_day.get("used_none") or [],
                        "top_fame_today": previous_day.get("top_fame_today") or [],
                        "top_fame_total": previous_day.get("top_fame_total") or [],
                        **_battle_lead_payload(previous_day.get("race_rank"), war_state=previous_day),
                    })
    return signals


def detect_war_battle_final_hours(conn=None, threshold_hours=6):
    current = db.get_current_war_day_state(conn=conn)
    if not current or current.get("phase") != "battle":
        return []
    if current.get("race_completed"):
        return []
    time_left_seconds = current.get("time_left_seconds")
    if time_left_seconds is None or time_left_seconds <= 0 or time_left_seconds > int(threshold_hours * 3600):
        return []
    signal_log_type = _war_period_signal_log_type("war_battle_day_final_hours", current)
    signal_date = _war_signal_date_for_state(current)
    if db.was_signal_sent(signal_log_type, signal_date, conn=conn):
        return []
    return [{
        "type": "war_battle_day_final_hours",
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": current.get("season_id"),
        "week": current.get("week"),
        "phase_display": current.get("phase_display"),
        "day_number": current.get("day_number"),
        "day_total": current.get("day_total"),
        "race_rank": current.get("race_rank"),
        "time_left_seconds": current.get("time_left_seconds"),
        "time_left_text": current.get("time_left_text"),
        "used_all_4": current.get("used_all_4") or [],
        "used_some": current.get("used_some") or [],
        "used_none": current.get("used_none") or [],
        "top_fame_today": current.get("top_fame_today") or [],
        **_battle_lead_payload(current.get("race_rank"), war_state=current),
    }]


def detect_war_rank_changes(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []
    return _detect_war_rank_changes_for_pair(states[0], states[1], conn=conn)


def _detect_war_rank_changes_for_pair(current, previous, conn=None):
    if not current or not previous:
        return []
    if current.get("phase") != "battle" or previous.get("phase") != "battle":
        return []
    if current.get("race_completed"):
        return []
    if current.get("war_day_key") != previous.get("war_day_key"):
        return []
    previous_rank = previous.get("race_rank")
    current_rank = current.get("race_rank")
    if previous_rank is None or current_rank is None or previous_rank == current_rank:
        return []
    current_day = db.get_war_day_state(current.get("war_day_key"), observed_at=current.get("observed_at"), conn=conn)
    signal_log_type = f"{_war_period_signal_log_type('war_battle_rank_change', current)}::rank{current_rank}"
    signal_date = _war_signal_date_for_state(current_day, current)
    if db.was_signal_sent(signal_log_type, signal_date, conn=conn):
        return []
    return [{
        "type": "war_battle_rank_change",
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": current.get("season_id"),
        "week": current.get("week"),
        "phase_display": current.get("phase_display"),
        "previous_rank": previous_rank,
        "race_rank": current_rank,
        "clan_fame": current.get("fame"),
        "clan_score": current.get("clan_score"),
        "time_left_seconds": (current_day or {}).get("time_left_seconds"),
        "time_left_text": (current_day or {}).get("time_left_text"),
        "top_fame_today": (current_day or {}).get("top_fame_today") or [],
        **_battle_lead_payload(current_rank, previous_rank=previous_rank, war_state=current_day or current),
    }]


def detect_war_battle_checkpoints(conn=None):
    """Emit battle-day updates at 12h, 18h, and 21h elapsed.

    If Elixir wakes up late, emit only the latest reached unsent checkpoint
    instead of replaying older checkpoints.
    """
    day_state = db.get_current_war_day_state(conn=conn) or {}
    if day_state.get("phase") != "battle":
        return []
    if day_state.get("race_completed"):
        return []

    time_left_seconds = day_state.get("time_left_seconds")
    if time_left_seconds is None:
        return []

    elapsed_seconds = max(0, BATTLE_DAY_SECONDS - time_left_seconds)
    signal_date = _war_signal_date_for_state(day_state)
    war_state = {
        "war_day_key": day_state.get("war_day_key"),
        "season_id": day_state.get("season_id"),
        "section_index": day_state.get("section_index"),
        "period_index": day_state.get("period_index"),
    }

    chosen_checkpoint = None
    for checkpoint in _BATTLE_DAY_CHECKPOINTS:
        if elapsed_seconds < checkpoint["hour"] * 3600:
            continue
        signal_log_type = (
            f"{_war_period_signal_log_type(checkpoint['signal_key'], war_state)}"
            f"::h{checkpoint['hour']}"
        )
        if db.was_signal_sent(signal_log_type, signal_date, conn=conn):
            continue
        chosen_checkpoint = (checkpoint, signal_log_type)
        break

    if chosen_checkpoint is None:
        return []

    checkpoint, signal_log_type = chosen_checkpoint
    return [{
        "type": checkpoint["signal_type"],
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": day_state.get("season_id"),
        "week": day_state.get("week"),
        "phase_display": day_state.get("phase_display"),
        "day_number": day_state.get("day_number"),
        "day_total": day_state.get("day_total"),
        "race_rank": day_state.get("race_rank"),
        "clan_fame": day_state.get("clan_fame"),
        "clan_score": day_state.get("clan_score"),
        "period_points": day_state.get("period_points"),
        "time_left_seconds": time_left_seconds,
        "time_left_text": day_state.get("time_left_text"),
        "used_all_4": day_state.get("used_all_4") or [],
        "used_some": day_state.get("used_some") or [],
        "used_none": day_state.get("used_none") or [],
        "top_fame_today": day_state.get("top_fame_today") or [],
        "top_fame_total": day_state.get("top_fame_total") or [],
        "engaged_count": day_state.get("engaged_count") or 0,
        "finished_count": day_state.get("finished_count") or 0,
        "untouched_count": day_state.get("untouched_count") or 0,
        "total_participants": day_state.get("total_participants") or 0,
        "checkpoint_hour": checkpoint["hour"],
        "checkpoint_label": checkpoint["label"],
        "checkpoint_hours_remaining": checkpoint["hours_remaining"],
        "hours_elapsed": elapsed_seconds // 3600,
        "hours_remaining": max(0, time_left_seconds) // 3600,
        "engagement_pct": round(100 * (day_state.get("engaged_count") or 0) / max(1, day_state.get("total_participants") or 1)),
        "completion_pct": round(100 * (day_state.get("finished_count") or 0) / max(1, day_state.get("total_participants") or 1)),
        "pace_status": _compute_pace_status(
            day_state.get("clan_fame"),
            day_state.get("day_number"),
            day_state.get("day_total"),
            elapsed_seconds // 3600,
            day_state.get("period_type"),
        ),
        "fame_target": _COLOSSEUM_FAME_TARGET if is_colosseum_week(day_state.get("period_type")) else _RACE_FAME_TARGET,
        **_battle_lead_payload(day_state.get("race_rank"), war_state=day_state),
    }]


def detect_war_deck_usage(war_data, conn=None):
    """Compatibility wrapper for older callers.

    Battle-day engagement updates are now time-based checkpoints, not first
    activity detection. `war_data` is ignored.
    """
    del war_data
    return detect_war_battle_checkpoints(conn=conn)


def detect_war_week_complete(completion_signals, conn=None):
    signals = []
    for signal in completion_signals or []:
        if signal.get("type") != "war_completed":
            continue
        signal_log_type = (
            f"war_week_complete::{signal.get('season_id')}:{signal.get('section_index')}"
        )
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            continue
        week_summary = db.get_war_week_summary(
            season_id=signal.get("season_id"),
            section_index=signal.get("section_index"),
            conn=conn,
        )
        if not week_summary:
            continue
        signals.append({
            "type": "war_week_complete",
            "signal_log_type": signal_log_type,
            "signal_date": signal.get("signal_date") or _war_signal_date_for_values(signal.get("finish_time"), signal.get("created_date")),
            "season_id": signal.get("season_id"),
            "section_index": signal.get("section_index"),
            "week": (signal.get("section_index") + 1) if signal.get("section_index") is not None else None,
            "won": signal.get("won"),
            "our_rank": signal.get("our_rank"),
            "our_fame": signal.get("our_fame"),
            "total_clans": signal.get("total_clans"),
            "week_summary": week_summary,
        })
    return signals


def build_situation_time(*, war_day_state=None, conn=None):
    """Compact time/phase awareness for any channel post.

    Lifts hours-remaining, day index, phase, and colosseum awareness out of
    checkpoint-only scope so non-checkpoint posts (streaks, milestones,
    standings) can reason about *when* in the war week they're firing.
    Returns ``None`` when there is no current war state.
    """
    if war_day_state is None:
        war_day_state = db.get_current_war_day_state(conn=conn) or {}
    if not war_day_state:
        return None

    phase = war_day_state.get("phase")
    period_type = war_day_state.get("period_type")
    day_number = war_day_state.get("day_number")
    day_total = war_day_state.get("day_total")
    time_left_seconds = war_day_state.get("time_left_seconds")
    hours_remaining_in_day = (
        max(0, time_left_seconds) // 3600 if time_left_seconds is not None else None
    )
    is_final_battle_day = (
        phase == "battle"
        and day_number is not None
        and day_total is not None
        and day_number == day_total
    )

    return {
        "phase": phase,
        "phase_display": war_day_state.get("phase_display"),
        "day_number": day_number,
        "day_total": day_total,
        "hours_remaining_in_day": hours_remaining_in_day,
        "time_left_text": war_day_state.get("time_left_text"),
        "is_final_battle_day": is_final_battle_day,
        "is_colosseum_week": is_colosseum_week(period_type),
        "season_id": war_day_state.get("season_id"),
        "week": war_day_state.get("week"),
        "race_completed": bool(war_day_state.get("race_completed")),
    }


def detect_war_season_completion(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []
    return _detect_war_season_completion_for_pair(states[0], states[1], conn=conn)


def _detect_war_season_completion_for_pair(current, previous, conn=None):
    if not current or not previous:
        return []
    current_season = current.get("season_id")
    previous_season = previous.get("season_id")
    if previous_season is None or current_season is None or previous_season == current_season:
        return []
    season_story = db.get_war_season_story(previous_season, conn=conn)
    if not season_story:
        return []
    signal_log_type = f"war_season_complete::{previous_season}"
    signal_date = _war_signal_date_for_state(previous, current)
    if db.was_signal_sent(signal_log_type, signal_date, conn=conn):
        return []
    return [{
        "type": "war_season_complete",
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": previous_season,
        "next_season_id": current_season,
        "season_story": season_story,
    }]


@db.managed_connection
def detect_war_completion(clan_tag=None, conn=None, *, refresh_log=True):
    """Emit any unannounced completed wars, optionally refreshing the race log first."""
    if refresh_log:
        try:
            race_log = cr_api.get_river_race_log()
        except requests.RequestException as e:
            log.warning("Failed to fetch river race log: %s", e)
            return []
        if not race_log:
            return []
        db.store_war_log(race_log, clan_tag or cr_api.CLAN_TAG, conn=conn)

    signals = []
    for row in _completed_war_races(conn=conn):
        signal_log_type = f"war_completed::{row.get('season_id')}:{row.get('section_index')}"
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            continue
        signals.append({
            "type": "war_completed",
            "signal_log_type": signal_log_type,
            "signal_date": _war_signal_date_for_values(row.get("finish_time"), row.get("created_date")),
            "season_id": row.get("season_id"),
            "section_index": row.get("section_index"),
            "our_rank": row.get("our_rank"),
            "our_fame": row.get("our_fame") or 0,
            "total_clans": row.get("total_clans"),
            "won": row.get("our_rank") == 1,
            "finish_time": row.get("finish_time"),
            "created_date": row.get("created_date"),
            "trophy_change": row.get("trophy_change"),
        })

    return signals


def detect_war_champ_update(completion_signals=None, conn=None):
    """Generate War Champ standings for completed war weeks that still need a recap."""
    signals = []
    seen = set()
    for signal in completion_signals or []:
        if signal.get("type") != "war_completed":
            continue
        season_id = signal.get("season_id")
        section_index = signal.get("section_index")
        key = (season_id, section_index)
        if key in seen:
            continue
        seen.add(key)
        signal_log_type = f"war_champ_standings::{season_id}:{section_index}"
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            continue
        standings = db.get_war_champ_standings(season_id=season_id, conn=conn)
        if not standings:
            continue
        perfect = db.get_perfect_war_participants(season_id=season_id, conn=conn)
        signals.append({
            "type": "war_champ_standings",
            "signal_log_type": signal_log_type,
            "signal_date": signal.get("signal_date") or _war_signal_date_for_values(signal.get("finish_time"), signal.get("created_date")),
            "season_id": season_id,
            "section_index": section_index,
            "week": section_index + 1 if section_index is not None else None,
            "standings": standings[:10],
            "leader": standings[0] if standings else None,
            "perfect_participants": perfect,
        })
    return signals
