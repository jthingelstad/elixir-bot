"""heartbeat._helpers — Signal formatting, payload builders, cursor utils."""

import logging
from datetime import datetime, timezone

import db
from storage.war_calendar import is_colosseum_week, war_signal_date

log = logging.getLogger("elixir_heartbeat")

BATTLE_DAY_SECONDS = 24 * 60 * 60
_RACE_FAME_TARGET = 10_000
_COLOSSEUM_FAME_TARGET = 5_000
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

WAR_LIVE_STATE_CURSOR_KEY = "war_live_state_pipeline"
WAR_PARTICIPANT_CURSOR_KEY = "war_participant_pipeline"

LIKELY_KICKED_INACTIVITY_DAYS = 7


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


def _war_signal_date_for_values(*values):
    for value in values:
        signal_date = war_signal_date(value)
        if signal_date:
            return signal_date
    return db.chicago_today()


def _war_signal_date_for_state(war_state, *fallbacks):
    war_state = war_state or {}
    return _war_signal_date_for_values(
        war_state.get("observed_at"),
        war_state.get("period_started_at"),
        war_state.get("first_observed_at"),
        war_state.get("last_observed_at"),
        *fallbacks,
    )


def _battle_lead_payload(race_rank, previous_rank=None, *, war_state=None):
    war_state = war_state or {}
    race_completed = bool(war_state.get("race_completed"))
    payload = {
        "race_rank": race_rank,
        "in_first_place": race_rank == 1 if race_rank is not None else None,
        "needs_lead_recovery": bool(race_rank and race_rank > 1 and not race_completed),
        "race_completed": race_completed,
        "race_completed_at": war_state.get("race_completed_at"),
        "race_completed_early": bool(war_state.get("race_completed_early")),
        "trophy_change": war_state.get("trophy_change"),
        "trophy_stakes_known": bool(war_state.get("trophy_stakes_known")),
        "trophy_stakes_text": war_state.get("trophy_stakes_text"),
    }
    if race_completed:
        story = "POAP KINGS has already finished this week's race."
        if war_state.get("trophy_stakes_text"):
            story = f"{story} This week carried {war_state.get('trophy_stakes_text')}."
        payload.update({
            "lead_pressure": "complete",
            "lead_story": story,
            "lead_call_to_action": (
                "Shift the message to completion, recognition, and clean closure instead of urgency."
            ),
        })
    elif race_rank and race_rank > 1:
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


@db.managed_connection
def _detect_war_race_finished_live_for_pair(current, previous=None, conn=None):
    if not current or not current.get("race_completed"):
        return []
    if (
        previous
        and previous.get("season_id") == current.get("season_id")
        and previous.get("section_index") == current.get("section_index")
        and previous.get("race_completed")
    ):
        return []

    season_id = current.get("season_id")
    section_index = current.get("section_index")
    signal_log_type = (
        f"war_race_finished_live::{season_id}:{section_index}"
        if season_id is not None and section_index is not None
        else "war_race_finished_live"
    )
    signal_date = _war_signal_date_for_values(
        current.get("finish_time"),
        current.get("race_completed_at"),
        current.get("observed_at"),
    )
    if db.was_signal_sent_any_date(signal_log_type, conn=conn):
        return []

    return [{
        "type": "war_race_finished_live",
        "signal_log_type": signal_log_type,
        "signal_date": signal_date,
        "season_id": current.get("season_id"),
        "section_index": current.get("section_index"),
        "week": current.get("week"),
        "phase": current.get("phase"),
        "phase_display": current.get("phase_display"),
        "day_number": current.get("battle_day_number") or current.get("practice_day_number"),
        "day_total": current.get("battle_day_total") or current.get("practice_day_total"),
        "race_rank": current.get("race_rank"),
        "clan_fame": current.get("fame"),
        "clan_score": current.get("clan_score"),
        "period_points": current.get("period_points"),
        "finish_time": current.get("finish_time"),
        "race_completed_at": current.get("race_completed_at"),
        "race_completed": True,
        "race_completed_early": bool(current.get("race_completed_early")),
        "trophy_change": current.get("trophy_change"),
        "trophy_stakes_known": bool(current.get("trophy_stakes_known")),
        "trophy_stakes_text": current.get("trophy_stakes_text"),
        "message": "POAP KINGS has already finished this week's river race.",
    }]


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


def _cursor_update(detector_key, scope_key="", *, cursor_text=None, cursor_int=None, metadata=None):
    return {
        "detector_key": detector_key,
        "scope_key": scope_key or "",
        "cursor_text": cursor_text,
        "cursor_int": cursor_int,
        "metadata": metadata or {},
    }


def _enrich_leave_signal(tag, name, conn):
    """Add last-known activity context to a leave signal."""
    signal = {
        "type": "member_leave",
        "tag": tag,
        "name": name,
        "signal_log_type": f"member_leave:{tag}:{db.chicago_today()}",
    }
    if conn is None:
        return signal
    row = conn.execute(
        "SELECT cs.role, cs.last_seen_api, cs.trophies, cs.donations_week, mm.cr_games_per_day "
        "FROM members m "
        "JOIN member_current_state cs ON cs.member_id = m.member_id "
        "LEFT JOIN member_metadata mm ON mm.member_id = m.member_id "
        "WHERE m.player_tag = ?",
        (tag.lstrip("#"),),
    ).fetchone()
    if not row:
        return signal
    signal["last_role"] = row["role"]
    signal["last_trophies"] = row["trophies"]
    signal["last_donations_week"] = row["donations_week"]
    signal["games_per_day"] = row["cr_games_per_day"]
    last_seen_dt = db._parse_cr_time(row["last_seen_api"])
    if last_seen_dt is not None:
        days_inactive = (datetime.now(timezone.utc).replace(tzinfo=None) - last_seen_dt).days
        signal["days_inactive"] = days_inactive
        signal["likely_kicked"] = days_inactive >= LIKELY_KICKED_INACTIVITY_DAYS
    else:
        signal["likely_kicked"] = False
    return signal


def _compute_pace_status(clan_fame, day_number, day_total, hours_elapsed, period_type):
    """Compare current fame against linear pace expectation.

    Returns 'ahead_of_pace', 'on_pace', or 'behind_pace'.
    """
    fame_target = _COLOSSEUM_FAME_TARGET if is_colosseum_week(period_type) else _RACE_FAME_TARGET
    total_battle_hours = max(1, (day_total or 4) * 24)
    elapsed_battle_hours = max(0, ((day_number or 1) - 1) * 24 + (hours_elapsed or 0))
    if elapsed_battle_hours <= 0:
        return "on_pace"
    expected_fame = fame_target * elapsed_battle_hours / total_battle_hours
    ratio = (clan_fame or 0) / max(1, expected_fame)
    if ratio >= 1.15:
        return "ahead_of_pace"
    elif ratio <= 0.85:
        return "behind_pace"
    return "on_pace"
