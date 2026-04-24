"""heartbeat._war — War calendar event detectors."""

import logging
from datetime import datetime, timezone

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
from storage.war_status import (
    _format_duration_short as _format_remaining_short,
    _fresh_time_left_seconds,
    is_colosseum_week_confirmed,
)

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
        if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
        if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
        if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
        if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
        if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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

    season_token = current_season_id if current_season_id is not None else "live"
    week_signal_log_type = f"war_week_rollover::s{season_token}:w{current_section_index}"
    signals = []
    if not db.was_signal_sent_any_date(week_signal_log_type, conn=conn):
        signals.append({
            "type": "war_week_rollover",
            "signal_log_type": week_signal_log_type,
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
        })

    if (
        previous_season_id is not None
        and current_season_id is not None
        and current_season_id != previous_season_id
    ) or current_section_index < previous_section_index:
        season_signal_log_type = f"war_season_rollover::s{season_token}"
        if not db.was_signal_sent_any_date(season_signal_log_type, conn=conn):
            signals.append({
                "type": "war_season_rollover",
                "signal_log_type": season_signal_log_type,
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
                if signal_log_type and not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
                if signal_log_type and not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
                if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
                if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
    if db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
    if db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
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
    """Compatibility wrapper for older callers."""
    del war_data
    return detect_war_battle_checkpoints(conn=conn)


def detect_war_battle_activity(conn=None):
    """Event-driven battle-day activity signals.

    Fires when members COMPLETE their war attacks (all 4 decks used) — the
    discrete event that matters, not a time checkpoint. Batches all new
    completions into one signal per tick so the agent can frame them together.

    Members who achieve 900 fame (4 wins, 0 losses) are flagged as
    ``perfect: true`` in the payload — an exceptional accomplishment.

    Runs in the war_awareness pipeline (hourly at :44) so it reacts to each
    war-poll's fresh data. No activity → no signal. Burst → signal.
    """
    day_state = db.get_current_war_day_state(conn=conn) or {}
    if day_state.get("phase") != "battle":
        return []

    battle_date = day_state.get("war_day_key")
    if not battle_date:
        return []

    used_all = day_state.get("used_all_4") or []
    if not used_all:
        return []

    season_id = day_state.get("season_id")
    week = day_state.get("week")
    week_key = f"s{season_id}:w{week}"

    new_completions = []
    for member in used_all:
        tag = member.get("tag")
        if not tag:
            continue
        signal_log_type = f"war_attacks_complete:{tag}:{week_key}"
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            continue
        new_completions.append({
            "tag": tag,
            "name": member.get("name") or member.get("member_ref") or "?",
            "fame": member.get("fame") or member.get("fame_today") or 0,
            "perfect": (member.get("fame") or member.get("fame_today") or 0) >= 900,
            "signal_log_type": signal_log_type,
        })

    if not new_completions:
        return []

    signal_date = _war_signal_date_for_state(day_state)
    return [{
        "type": "war_attacks_complete",
        "signal_date": signal_date,
        "battle_date": battle_date,
        "season_id": day_state.get("season_id"),
        "week": day_state.get("week"),
        "day_number": day_state.get("day_number"),
        "day_total": day_state.get("day_total"),
        "phase_display": day_state.get("phase_display"),
        "members": new_completions,
        "clan_fame": day_state.get("clan_fame"),
        "race_rank": day_state.get("race_rank"),
    }]


def detect_war_surprise_participants(conn=None):
    """Emit war_surprise_participant when a 'never' or 'rare' war player attacks.

    A member who historically doesn't participate in war suddenly playing is
    a notable positive event — especially if it's their first war ever. Uses
    the existing _war_player_type classification (regular/occasional/rare/never).
    """
    from storage.war_analytics import war_player_types_by_tag, has_played_earlier_this_week

    day_state = db.get_current_war_day_state(conn=conn) or {}
    if day_state.get("phase") != "battle":
        return []

    battle_date = day_state.get("war_day_key")
    if not battle_date:
        return []

    engaged = (day_state.get("used_all_4") or []) + (day_state.get("used_some") or [])
    if not engaged:
        return []

    tags = [m.get("tag") for m in engaged if m.get("tag")]
    if not tags:
        return []

    type_map = war_player_types_by_tag(conn, tags)

    week_key = f"s{day_state.get('season_id')}:w{day_state.get('week')}"
    season_id = day_state.get("season_id")
    section_index = day_state.get("section_index")
    period_index = day_state.get("period_index")

    surprises = []
    for member in engaged:
        tag = member.get("tag")
        if not tag:
            continue
        player_type = type_map.get(tag, "unknown")
        if player_type not in {"never", "rare"}:
            continue
        # The "never"/"rare" classification only counts *closed* races
        # (war_participation), so a brand-new clan member who has been
        # playing every battle day of the current open race still classifies
        # as "never" until the race finalizes. Treat any in-progress-week
        # play as evidence they're not actually a surprise this week.
        if has_played_earlier_this_week(conn, tag, season_id, section_index, period_index):
            continue
        signal_log_type = f"war_surprise_participant:{tag}:{week_key}"
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            continue
        surprises.append({
            "tag": tag,
            "name": member.get("name") or member.get("member_ref") or "?",
            "fame": member.get("fame") or member.get("fame_today") or 0,
            "decks_used": member.get("decks_used_today") or 0,
            "war_player_type": player_type,
            "first_war_ever": player_type == "never",
            "signal_log_type": signal_log_type,
        })

    if not surprises:
        return []

    signal_date = _war_signal_date_for_state(day_state)
    return [{
        "type": "war_surprise_participant",
        "signal_date": signal_date,
        "battle_date": battle_date,
        "season_id": day_state.get("season_id"),
        "week": day_state.get("week"),
        "day_number": day_state.get("day_number"),
        "phase_display": day_state.get("phase_display"),
        "members": surprises,
    }]


def detect_war_rival_activity(conn=None):
    """Emit war_rival_woke_up when an opponent's periodPoints goes from 0 to >0,
    and war_lead_change when our lead/deficit shifts significantly.

    Both use signal_detector_cursors to compare the current race standings
    against the last observed snapshot. Fires in the war_awareness pipeline.
    """
    DETECTOR_KEY = "war_standings"
    import json

    day_state = db.get_current_war_day_state(conn=conn) or {}
    if day_state.get("phase") not in {"battle", "training"}:
        return []

    war_state = db.get_current_war_status(conn=conn) or {}
    raw = json.loads(war_state.get("raw_json") or "{}") if isinstance(war_state.get("raw_json"), str) else (war_state.get("raw_json") or {})
    clans = raw.get("clans") or []
    our_tag = (raw.get("clan", {}).get("tag") or "").strip("#").upper()
    if not our_tag or not clans:
        return []

    ranked = sorted(clans, key=lambda c: (c.get("periodPoints") or 0), reverse=True)
    current = {(c.get("tag") or "").strip("#").upper(): c.get("periodPoints") or 0 for c in ranked}
    our_points = current.get(our_tag, 0)

    cursor = db.get_signal_detector_cursor(DETECTOR_KEY, conn=conn)
    prev_data = json.loads((cursor or {}).get("cursor_text") or "{}") if cursor else {}
    prev_standings = prev_data.get("standings", {})
    prev_our_points = prev_data.get("our_points", 0)

    db.upsert_signal_detector_cursor(
        DETECTOR_KEY,
        cursor_text=json.dumps({"standings": current, "our_points": our_points}),
        conn=conn,
    )

    if not prev_standings:
        return []

    signals = []
    signal_date = _war_signal_date_for_state(day_state)
    battle_date = day_state.get("war_day_key") or ""

    for clan in ranked:
        tag = (clan.get("tag") or "").strip("#").upper()
        if tag == our_tag:
            continue
        prev_pts = prev_standings.get(tag, 0)
        curr_pts = clan.get("periodPoints") or 0
        if prev_pts == 0 and curr_pts > 0:
            sig_key = f"war_rival_woke_up:{tag}:{battle_date}"
            if not db.was_signal_sent_any_date(sig_key, conn=conn):
                signals.append({
                    "type": "war_rival_woke_up",
                    "signal_date": signal_date,
                    "rival_name": clan.get("name"),
                    "rival_tag": clan.get("tag"),
                    "rival_points": curr_pts,
                    "our_points": our_points,
                    "signal_log_type": sig_key,
                })

    second_place_pts = ranked[1].get("periodPoints", 0) if len(ranked) > 1 else 0
    prev_lead = prev_our_points - max((v for k, v in prev_standings.items() if k != our_tag), default=0) if prev_standings else 0
    curr_lead = our_points - second_place_pts
    lead_delta = curr_lead - prev_lead
    if abs(lead_delta) >= 2000 and prev_lead != 0:
        sig_key = f"war_lead_change:{battle_date}:{our_points}"
        if not db.was_signal_sent_any_date(sig_key, conn=conn):
            signals.append({
                "type": "war_lead_change",
                "signal_date": signal_date,
                "our_points": our_points,
                "second_place_points": second_place_pts,
                "lead": curr_lead,
                "previous_lead": prev_lead,
                "lead_delta": lead_delta,
                "direction": "growing" if lead_delta > 0 else "shrinking",
                "signal_log_type": sig_key,
            })

    return signals


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

    As of #20 the remaining-time fields are computed from ``period_ends_at``
    against wall-clock time, so the values stay accurate between polls
    instead of aging with the stored ``time_left_seconds``.
    """
    if war_day_state is None:
        war_day_state = db.get_current_war_day_state(conn=conn) or {}
    if not war_day_state:
        return None

    phase = war_day_state.get("phase")
    period_type = war_day_state.get("period_type")
    day_number = war_day_state.get("day_number")
    day_total = war_day_state.get("day_total")
    fresh_seconds = _fresh_time_left_seconds(war_day_state)
    hours_remaining_in_day = (
        max(0, fresh_seconds) // 3600 if fresh_seconds is not None else None
    )
    minutes_remaining_in_day = (
        max(0, fresh_seconds) // 60 if fresh_seconds is not None else None
    )
    time_left_text = _format_remaining_short(fresh_seconds) or war_day_state.get("time_left_text")
    final_day_matches_total = (
        day_number is not None
        and day_total is not None
        and day_number == day_total
    )
    is_final_battle_day = phase == "battle" and final_day_matches_total
    is_final_practice_day = phase == "practice" and final_day_matches_total

    days_after_today = (
        max(0, day_total - day_number)
        if day_number is not None and day_total is not None
        else None
    )
    battle_days_after_today = days_after_today if phase == "battle" else None
    practice_days_after_today = days_after_today if phase == "practice" else None

    return {
        "phase": phase,
        "phase_display": war_day_state.get("phase_display"),
        "day_number": day_number,
        "day_total": day_total,
        "hours_remaining_in_day": hours_remaining_in_day,
        "minutes_remaining_in_day": minutes_remaining_in_day,
        "time_left_seconds": fresh_seconds,
        "time_left_text": time_left_text,
        "is_final_battle_day": is_final_battle_day,
        "is_final_practice_day": is_final_practice_day,
        "battle_days_after_today": battle_days_after_today,
        "practice_days_after_today": practice_days_after_today,
        "is_colosseum_week": is_colosseum_week_confirmed(
            period_type,
            war_day_state.get("trophy_change"),
            trophy_stakes_known=bool(war_day_state.get("trophy_stakes_known")),
        ),
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
    if db.was_signal_sent_any_date(signal_log_type, conn=conn):
        return []
    return [{
        "type": "war_season_complete",
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": previous_season,
        "next_season_id": current_season,
        "season_story": season_story,
    }]


_WAR_COMPLETION_MAX_AGE_DAYS = 7


def _is_stale_war_race_timestamp(value) -> bool:
    """True when a CR-format timestamp is missing, unparseable, epoch-sentinel,
    or older than _WAR_COMPLETION_MAX_AGE_DAYS. Used to reject war_races rows
    whose finish_time/created_date came back corrupted — otherwise a late-
    arriving bad row fires a 'this war just finished' signal for a race
    that ended weeks ago.
    """
    if not value:
        return True
    parsed = db._parse_cr_time(value)
    if parsed is None:
        return True
    if parsed.year < 2000:
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (now - parsed).total_seconds() > _WAR_COMPLETION_MAX_AGE_DAYS * 86400:
        return True
    return False


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
        finish_time = row.get("finish_time")
        created_date = row.get("created_date")
        if _is_stale_war_race_timestamp(finish_time) and _is_stale_war_race_timestamp(created_date):
            log.warning(
                "war_completion_skipped_stale_finish_time season=%s section=%s finish_time=%r created_date=%r",
                row.get("season_id"), row.get("section_index"), finish_time, created_date,
            )
            continue
        signals.append({
            "type": "war_completed",
            "signal_log_type": signal_log_type,
            "signal_date": _war_signal_date_for_values(finish_time, created_date),
            "season_id": row.get("season_id"),
            "section_index": row.get("section_index"),
            "our_rank": row.get("our_rank"),
            "our_fame": row.get("our_fame") or 0,
            "total_clans": row.get("total_clans"),
            "won": row.get("our_rank") == 1,
            "finish_time": finish_time,
            "created_date": created_date,
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
