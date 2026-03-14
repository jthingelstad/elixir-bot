"""heartbeat.py — Hourly signal detection for Elixir bot.

Runs cheap deterministic checks against fresh clan data and the SQLite
history store.  Only calls the LLM when real signals are found.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import cr_api
import cr_knowledge
import db
import prompts

log = logging.getLogger("elixir_heartbeat")

BATTLE_DAY_SECONDS = 24 * 60 * 60
_BATTLE_DAY_CHECKPOINTS = (
    {
        "hour": 21,
        "signal_type": "war_battle_day_final_hours",
        "signal_key": "war_battle_day_checkpoint",
        "label": "final push",
        "hours_remaining": 3,
    },
    {
        "hour": 18,
        "signal_type": "war_battle_day_live_update",
        "signal_key": "war_battle_day_checkpoint",
        "label": "late push",
        "hours_remaining": 6,
    },
    {
        "hour": 12,
        "signal_type": "war_battle_day_live_update",
        "signal_key": "war_battle_day_checkpoint",
        "label": "midday check-in",
        "hours_remaining": 12,
    },
)


# ── Signal detectors ─────────────────────────────────────────────────────────
# Each returns a list of signal dicts (may be empty).


def _war_period_signal_log_type(base_type, war_state):
    if not war_state:
        return base_type
    war_day_key = war_state.get("war_day_key")
    if war_day_key:
        return f"{base_type}::{war_day_key}"
    section_index = war_state.get("section_index")
    period_index = war_state.get("period_index")
    if section_index is None or period_index is None:
        return base_type
    season_token = war_state.get("season_id")
    if season_token is None:
        season_token = "live"
    return f"{base_type}::s{season_token}:w{section_index}:p{period_index}"


def _battle_lead_payload(race_rank, previous_rank=None):
    payload = {
        "race_rank": race_rank,
        "in_first_place": race_rank == 1 if race_rank is not None else None,
        "needs_lead_recovery": bool(race_rank and race_rank > 1),
    }
    if race_rank and race_rank > 1:
        payload.update({
            "lead_pressure": "high",
            "lead_story": f"POAP KINGS is currently in place {race_rank} and needs battle wins to restore first place.",
            "lead_call_to_action": "Encourage members to finish their war battles and help restore first place.",
        })
    elif race_rank == 1:
        payload.update({
            "lead_pressure": "hold",
            "lead_story": "POAP KINGS is in first place right now and should protect the lead.",
            "lead_call_to_action": "Encourage members to keep battling so we stay on top.",
        })
    if previous_rank is not None and race_rank is not None and previous_rank != race_rank:
        payload["previous_rank"] = previous_rank
        payload["lost_ground"] = race_rank > previous_rank
        payload["gained_ground"] = race_rank < previous_rank
    return payload


def _completed_war_races(conn=None):
    close = conn is None
    conn = conn or db.get_connection()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT war_race_id, season_id, section_index, created_date, our_rank, trophy_change, "
                "our_fame, total_clans, finish_time "
                "FROM war_races ORDER BY season_id DESC, section_index DESC, war_race_id DESC"
            ).fetchall()
        ]
    finally:
        if close:
            conn.close()


def detect_joins_leaves(current_members, known_snapshot):
    """Compare current roster to known snapshot for joins/departures.

    current_members: list of member dicts from CR API memberList.
    known_snapshot: dict of {tag: name} from the previous roster.

    Returns (signals, updated_snapshot).
    """
    current = {m["tag"]: m["name"] for m in current_members}
    signals = []

    for tag, name in current.items():
        if tag not in known_snapshot:
            signals.append({
                "type": "member_join",
                "tag": tag,
                "name": name,
            })

    for tag, name in known_snapshot.items():
        if tag not in current:
            signals.append({
                "type": "member_leave",
                "tag": tag,
                "name": name,
            })

    return signals, current


def detect_arena_changes(conn=None):
    """Check DB for arena changes since last snapshot."""
    milestones = db.detect_milestones(conn=conn)
    return [
        {
            "type": "arena_change",
            "tag": m["tag"],
            "name": m["name"],
            "old_arena": m["old_value"],
            "new_arena": m["new_value"],
        }
        for m in milestones
        if m["type"] == "arena_change"
    ]


def detect_role_changes(conn=None):
    """Check DB for role promotions/demotions since last snapshot."""
    changes = db.detect_role_changes(conn=conn)
    return [
        {
            "type": "role_change",
            "tag": c["tag"],
            "name": c["name"],
            "old_role": c["old_role"],
            "new_role": c["new_role"],
        }
        for c in changes
    ]


def detect_war_day_transition(now=None, conn=None):
    """Detect API-native war phase transitions and notable phase states."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if not states:
        return []

    current = states[0]
    previous = states[1] if len(states) > 1 else None
    signals = []
    latest_clan_defense_status = db.get_latest_clan_boat_defense_status(conn=conn)

    if current.get("battle_phase_active") and (
        previous is None or not previous.get("battle_phase_active")
    ):
        signal_log_type = _war_period_signal_log_type("war_battle_phase_active", current)
        if not db.was_signal_sent(signal_log_type, today, conn=conn):
            signals.append({
                "type": "war_battle_phase_active",
                "signal_log_type": signal_log_type,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
                "message": "Battle phase is live. Time to use those war decks.",
            })
    if current.get("practice_phase_active") and (
        previous is None or not previous.get("practice_phase_active")
    ):
        signal_log_type = _war_period_signal_log_type("war_practice_phase_active", current)
        if not db.was_signal_sent(signal_log_type, today, conn=conn):
            signals.append({
                "type": "war_practice_phase_active",
                "signal_log_type": signal_log_type,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
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
    if current.get("final_practice_day_active"):
        signal_log_type = _war_period_signal_log_type("war_final_practice_day", current)
        if not db.was_signal_sent(signal_log_type, today, conn=conn):
            signals.append({
                "type": "war_final_practice_day",
                "signal_log_type": signal_log_type,
                "season_id": current.get("season_id"),
                "week": current.get("week"),
                "section_index": current.get("section_index"),
                "period_index": current.get("period_index"),
                "period_type": current.get("period_type"),
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
    if current.get("final_battle_day_active"):
        signal_log_type = _war_period_signal_log_type("war_final_battle_day", current)
        if not db.was_signal_sent(signal_log_type, today, conn=conn):
            signals.append({
                "type": "war_final_battle_day",
                "signal_log_type": signal_log_type,
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
        if not db.was_signal_sent(signal_log_type, today, conn=conn):
            signals.append({
                "type": "war_battle_days_complete",
                "signal_log_type": signal_log_type,
                "previous_season_id": previous.get("season_id"),
                "season_id": current.get("season_id"),
                "previous_week": previous.get("week"),
                "week": current.get("week"),
                "previous_period_type": previous.get("period_type"),
                "period_type": current.get("period_type"),
                "message": "Battle phase has ended. River Race has moved out of battle days.",
            })

    return signals


def detect_war_rollovers(conn=None):
    """Detect live war week and season rollovers from consecutive snapshots."""
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []

    current, previous = states[0], states[1]
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
    signals = []

    current_key = current.get("war_day_key")
    previous_key = previous.get("war_day_key") if previous else None
    if current_key and current_key != previous_key:
        current_day = db.get_current_war_day_state(conn=conn)
        if current_day:
            if current.get("phase") == "practice":
                if (current_day.get("day_number") or 0) <= 1:
                    signal_log_type = None
                else:
                    signal_log_type = _war_period_signal_log_type("war_practice_day_started", current)
                if signal_log_type and not db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
                    signals.append({
                        "type": "war_practice_day_started",
                        "signal_log_type": signal_log_type,
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
                if signal_log_type and not db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
                    signals.append({
                        "type": "war_battle_day_started",
                        "signal_log_type": signal_log_type,
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
                        **_battle_lead_payload(current_day.get("race_rank")),
                    })

    if previous and previous_key and current_key != previous_key:
        previous_day = db.get_war_day_state(previous_key, conn=conn)
        if previous_day:
            completed_at = current.get("observed_at")
            if previous.get("phase") == "practice":
                signal_log_type = _war_period_signal_log_type("war_practice_day_complete", previous)
                if not db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
                    signals.append({
                        "type": "war_practice_day_complete",
                        "signal_log_type": signal_log_type,
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
                if not db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
                    signals.append({
                        "type": "war_battle_day_complete",
                        "signal_log_type": signal_log_type,
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
                        **_battle_lead_payload(previous_day.get("race_rank")),
                    })
    return signals


def detect_war_battle_final_hours(conn=None, threshold_hours=6):
    current = db.get_current_war_day_state(conn=conn)
    if not current or current.get("phase") != "battle":
        return []
    time_left_seconds = current.get("time_left_seconds")
    if time_left_seconds is None or time_left_seconds <= 0 or time_left_seconds > int(threshold_hours * 3600):
        return []
    signal_log_type = _war_period_signal_log_type("war_battle_day_final_hours", current)
    if db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
        return []
    return [{
        "type": "war_battle_day_final_hours",
        "signal_log_type": signal_log_type,
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
        **_battle_lead_payload(current.get("race_rank")),
    }]


def detect_war_rank_changes(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []
    current, previous = states[0], states[1]
    if current.get("phase") != "battle" or previous.get("phase") != "battle":
        return []
    if current.get("war_day_key") != previous.get("war_day_key"):
        return []
    previous_rank = previous.get("race_rank")
    current_rank = current.get("race_rank")
    if previous_rank is None or current_rank is None or previous_rank == current_rank:
        return []
    current_day = db.get_current_war_day_state(conn=conn)
    signal_log_type = f"{_war_period_signal_log_type('war_battle_rank_change', current)}::rank{current_rank}"
    if db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
        return []
    return [{
        "type": "war_battle_rank_change",
        "signal_log_type": signal_log_type,
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
        **_battle_lead_payload(current_rank, previous_rank=previous_rank),
    }]


def detect_donation_leaders(current_members, conn=None):
    """Identify the top 3 donors from the current roster.

    Only fires once per day.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if db.was_signal_sent("donation_leaders", today, conn=conn):
        return []
    sorted_members = sorted(current_members, key=lambda m: m.get("donations", 0), reverse=True)
    top = sorted_members[:3]
    if not top or top[0].get("donations", 0) == 0:
        return []
    return [{
        "type": "donation_leaders",
        "leaders": [
            {"name": m.get("name", "?"), "donations": m.get("donations", 0), "rank": i + 1}
            for i, m in enumerate(top)
        ],
    }]


def detect_inactivity(current_members, now=None, conn=None):
    """Flag members not seen in 3+ days.

    Uses the lastSeen field from CR API (format: 20260304T120000.000Z).
    Only fires once per day.
    """
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    if db.was_signal_sent("inactive_members", today, conn=conn):
        return []
    now = now or datetime.now()
    signals = []
    inactive = []
    threshold = cr_knowledge.INACTIVITY_DAYS

    for m in current_members:
        last_seen = m.get("lastSeen", m.get("last_seen", ""))
        if not last_seen:
            continue
        try:
            # Parse CR API date format: 20260304T120000.000Z
            clean = last_seen.split(".")[0]  # Remove .000Z
            seen_dt = datetime.strptime(clean, "%Y%m%dT%H%M%S")
            days_away = (now - seen_dt).days
            if days_away >= threshold:
                inactive.append({
                    "name": m.get("name", "?"),
                    "tag": m.get("tag", ""),
                    "days_inactive": days_away,
                    "role": m.get("role", "member"),
                })
        except (ValueError, TypeError):
            continue

    if inactive:
        signals.append({
            "type": "inactive_members",
            "members": sorted(inactive, key=lambda x: x["days_inactive"], reverse=True),
        })

    return signals


def detect_war_battle_checkpoints(conn=None):
    """Emit battle-day updates at 12h, 18h, and 21h elapsed.

    If Elixir wakes up late, emit only the latest reached unsent checkpoint
    instead of replaying older checkpoints.
    """
    day_state = db.get_current_war_day_state(conn=conn) or {}
    if day_state.get("phase") != "battle":
        return []

    time_left_seconds = day_state.get("time_left_seconds")
    if time_left_seconds is None:
        return []

    elapsed_seconds = max(0, BATTLE_DAY_SECONDS - time_left_seconds)
    today = datetime.now().strftime("%Y-%m-%d")
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
        if db.was_signal_sent(signal_log_type, today, conn=conn):
            continue
        chosen_checkpoint = (checkpoint, signal_log_type)
        break

    if chosen_checkpoint is None:
        return []

    checkpoint, signal_log_type = chosen_checkpoint
    return [{
        "type": checkpoint["signal_type"],
        "signal_log_type": signal_log_type,
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
        **_battle_lead_payload(day_state.get("race_rank")),
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


def detect_war_season_completion(conn=None):
    states = db.get_recent_live_war_states(limit=2, conn=conn)
    if len(states) < 2:
        return []
    current, previous = states[0], states[1]
    current_season = current.get("season_id")
    previous_season = previous.get("season_id")
    if previous_season is None or current_season is None or previous_season == current_season:
        return []
    season_story = db.get_war_season_story(previous_season, conn=conn)
    if not season_story:
        return []
    signal_log_type = f"war_season_complete::{previous_season}"
    if db.was_signal_sent(signal_log_type, db.chicago_today(), conn=conn):
        return []
    return [{
        "type": "war_season_complete",
        "signal_log_type": signal_log_type,
        "season_id": previous_season,
        "next_season_id": current_season,
        "season_story": season_story,
    }]


def detect_war_completion(clan_tag, conn=None):
    """Fetch river race log, store results, and emit any unannounced completed wars."""
    try:
        race_log = cr_api.get_river_race_log()
    except Exception as e:
        log.warning("Failed to fetch river race log: %s", e)
        return []

    if not race_log:
        return []

    close = conn is None
    conn = conn or db.get_connection()
    try:
        db.store_war_log(race_log, clan_tag, conn=conn)

        signals = []
        for row in _completed_war_races(conn=conn):
            signal_log_type = f"war_completed::{row.get('season_id')}:{row.get('section_index')}"
            if db.was_signal_sent_any_date(signal_log_type, conn=conn):
                continue
            signals.append({
                "type": "war_completed",
                "signal_log_type": signal_log_type,
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
    finally:
        if close:
            conn.close()


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
            "season_id": season_id,
            "section_index": section_index,
            "week": section_index + 1 if section_index is not None else None,
            "standings": standings[:10],
            "leader": standings[0] if standings else None,
            "perfect_participants": perfect,
        })
    return signals


def detect_cake_days(today_str=None, conn=None):
    """Check for clan birthday, join anniversaries, and member birthdays.

    Uses cake_day_announcements table for dedup — only returns signals
    for events not yet announced today.

    Returns list of signal dicts.
    """
    close = conn is None
    conn = conn or db.get_connection()
    try:
        if today_str is None:
            today_str = datetime.now().strftime("%Y-%m-%d")

        signals = []

        # Clan birthday — founded date from config
        thresholds = prompts.thresholds()
        clan_founded = thresholds.get("clan_founded", "2026-02-04")
        if today_str[5:] == clan_founded[5:]:  # month-day match
            if not db.was_announcement_sent(today_str, "clan_birthday", None, conn=conn):
                years = int(today_str[:4]) - int(clan_founded[:4])
                signals.append({
                    "type": "clan_birthday",
                    "years": years,
                })

        # Join anniversaries
        anniversaries = db.get_join_anniversaries_today(today_str, conn=conn)
        unannounced = []
        for a in anniversaries:
            if not db.was_announcement_sent(today_str, "join_anniversary", a["tag"], conn=conn):
                unannounced.append(a)
        if unannounced:
            signals.append({
                "type": "join_anniversary",
                "members": unannounced,
            })

        # Member birthdays
        birthdays = db.get_birthdays_today(today_str, conn=conn)
        unannounced_bdays = []
        for b in birthdays:
            if not db.was_announcement_sent(today_str, "birthday", b["tag"], conn=conn):
                unannounced_bdays.append(b)
        if unannounced_bdays:
            signals.append({
                "type": "member_birthday",
                "members": unannounced_bdays,
            })

        return signals
    finally:
        if close:
            conn.close()


def detect_pending_system_signals(today_str=None, conn=None):
    del today_str
    return db.list_pending_system_signals(conn=conn)


# ── Main heartbeat tick ──────────────────────────────────────────────────────


@dataclass
class HeartbeatTickResult:
    """Full heartbeat output bundle for downstream consumers."""
    signals: list
    clan: dict
    war: dict

def tick(conn=None, *, include_nonwar=True, include_war=True):
    """Run one heartbeat cycle and return signals + fetched clan/war data.

    Steps:
    1. Fetch live clan + war data
    2. Snapshot members to DB
    3. Purge expired data
    4. Run all signal detectors
    5. Return collected signals with the fetched data bundle
    """
    try:
        clan = cr_api.get_clan()
    except Exception as e:
        log.error("Heartbeat: failed to fetch clan data: %s", e)
        return HeartbeatTickResult(signals=[], clan={}, war={})

    members = clan.get("memberList", [])
    if not members:
        log.warning("Heartbeat: empty member list from API")
        return HeartbeatTickResult(signals=[], clan=clan, war={})

    try:
        war = cr_api.get_current_war()
    except Exception:
        war = {}

    close = conn is None
    conn = conn or db.get_connection()
    try:
        # 1. Get known roster BEFORE snapshotting (so we compare old vs new)
        known = db.get_active_roster_map(conn=conn)

        # 2. Snapshot current state
        db.snapshot_members(members, conn=conn)
        if war:
            db.upsert_war_current_state(war, conn=conn)

        # 3. Purge old data
        db.purge_old_data(conn=conn)

        # 4. Collect signals from all detectors
        signals = []

        db.snapshot_clan_daily_metrics(clan, conn=conn)

        # Backfill join dates from historical snapshots (idempotent)
        db.backfill_join_dates(conn=conn)

        if include_nonwar:
            # Join/leave detection
            join_leave_signals, _ = detect_joins_leaves(members, known)
            signals.extend(join_leave_signals)

            # Record join dates for newly detected members; reset tenure for leavers
            for sig in join_leave_signals:
                if sig["type"] == "member_join":
                    db.record_join_date(sig["tag"], sig["name"],
                                        db.chicago_today(), conn=conn)
                elif sig["type"] == "member_leave":
                    db.clear_member_tenure(sig["tag"], conn=conn)

            # Arena changes
            signals.extend(detect_arena_changes(conn=conn))

            # Role changes
            signals.extend(detect_role_changes(conn=conn))

            # Donation leaders — only towards end of day
            now = datetime.now()
            if now.hour >= cr_knowledge.DONATION_HIGHLIGHT_HOUR:
                signals.extend(detect_donation_leaders(members, conn=conn))

            # Inactivity
            signals.extend(detect_inactivity(members, conn=conn))

            # Cake days — birthdays, join anniversaries, clan birthday
            signals.extend(detect_cake_days(conn=conn))

            # Upgrade and capability announcements queued by migrations or manual ops
            signals.extend(detect_pending_system_signals(today_str=datetime.now().strftime("%Y-%m-%d"), conn=conn))

        if include_war:
            # War day awareness
            signals.extend(detect_war_day_transition(conn=conn))
            signals.extend(detect_war_day_markers(conn=conn))

            # Live war week/season rollovers
            signals.extend(detect_war_rollovers(conn=conn))

            # Battle-day rank swings
            signals.extend(detect_war_rank_changes(conn=conn))

            # Battle-day checkpoint updates
            signals.extend(detect_war_battle_checkpoints(conn=conn))

            # War completion + week/season summaries
            clan_tag = cr_api.CLAN_TAG
            war_signals = detect_war_completion(clan_tag, conn=conn)
            signals.extend(war_signals)
            signals.extend(detect_war_week_complete(war_signals, conn=conn))
            signals.extend(detect_war_season_completion(conn=conn))

            # If a war just completed, also share War Champ standings
            if war_signals:
                signals.extend(detect_war_champ_update(war_signals, conn=conn))

        log.info("Heartbeat: %d signals detected", len(signals))
        return HeartbeatTickResult(signals=signals, clan=clan, war=war)
    finally:
        if close:
            conn.close()
