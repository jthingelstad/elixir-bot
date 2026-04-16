"""heartbeat._pipeline — Live state cursor scanning and ingestion."""

import logging

import requests

import cr_api
import db
from heartbeat._helpers import (
    WAR_LIVE_STATE_CURSOR_KEY,
    WAR_PARTICIPANT_CURSOR_KEY,
    _cursor_update,
    _detect_war_race_finished_live_for_pair,
)
from heartbeat._war import (
    _detect_war_day_markers_for_pair,
    _detect_war_day_transition_for_pair,
    _detect_war_rank_changes_for_pair,
    _detect_war_rollovers_for_pair,
    _detect_war_season_completion_for_pair,
    detect_war_battle_activity,
    detect_war_champ_update,
    detect_war_completion,
    detect_war_rival_activity,
    detect_war_surprise_participants,
    detect_war_week_complete,
)

log = logging.getLogger("elixir_heartbeat")


def _build_stored_clan_context(war_state) -> dict:
    war_state = war_state or {}
    return {
        "name": war_state.get("clan_name"),
        "tag": war_state.get("clan_tag"),
    }


def _scan_war_live_state_cursor(conn=None):
    cursor = db.get_signal_detector_cursor(WAR_LIVE_STATE_CURSOR_KEY, conn=conn)
    latest_war_id = db.get_latest_live_war_state_id(conn=conn)
    if latest_war_id is None:
        return [], []
    if not cursor or cursor.get("cursor_int") is None:
        return [], [
            _cursor_update(
                WAR_LIVE_STATE_CURSOR_KEY,
                cursor_int=latest_war_id,
                metadata={"mode": "seed"},
            )
        ]

    after_war_id = int(cursor.get("cursor_int") or 0)
    states = db.list_live_war_states_after(after_war_id, conn=conn)
    if not states:
        return [], []

    previous = db.get_live_war_state_by_id(after_war_id, conn=conn)
    start_index = 0
    if previous is None:
        previous = db.get_previous_live_war_state_before(states[0]["war_id"], conn=conn)
    if previous is None:
        previous = states[0]
        start_index = 1

    signals = []
    for current in states[start_index:]:
        signals.extend(_detect_war_race_finished_live_for_pair(current, previous, conn=conn))
        signals.extend(_detect_war_day_transition_for_pair(current, previous, conn=conn))
        signals.extend(_detect_war_day_markers_for_pair(current, previous, conn=conn))
        signals.extend(_detect_war_rollovers_for_pair(current, previous, conn=conn))
        signals.extend(_detect_war_rank_changes_for_pair(current, previous, conn=conn))
        signals.extend(_detect_war_season_completion_for_pair(current, previous, conn=conn))
        previous = current

    return signals, [
        _cursor_update(
            WAR_LIVE_STATE_CURSOR_KEY,
            cursor_int=states[-1]["war_id"],
            metadata={"mode": "advance"},
        )
    ]


def _scan_war_participant_cursors(conn=None):
    signals = []
    updates = []
    for war_day_key in db.list_war_day_keys(conn=conn):
        latest_observed_at = db.get_latest_war_participant_snapshot_observed_at(
            war_day_key,
            conn=conn,
        )
        if not latest_observed_at:
            continue
        cursor = db.get_signal_detector_cursor(WAR_PARTICIPANT_CURSOR_KEY, war_day_key, conn=conn)
        if not cursor or not cursor.get("cursor_text"):
            updates.append(
                _cursor_update(
                    WAR_PARTICIPANT_CURSOR_KEY,
                    war_day_key,
                    cursor_text=latest_observed_at,
                    metadata={"mode": "seed", "war_day_key": war_day_key},
                )
            )
            continue

        observed_times = db.list_war_participant_snapshot_times_after(
            war_day_key,
            cursor.get("cursor_text"),
            conn=conn,
        )
        if not observed_times:
            continue

        previous_observed_at = cursor.get("cursor_text")
        if not db.get_war_participant_snapshot_group(war_day_key, previous_observed_at, conn=conn):
            previous_observed_at = db.get_previous_war_participant_snapshot_observed_at(
                war_day_key,
                observed_times[0],
                conn=conn,
            )

        start_index = 0
        if not previous_observed_at:
            previous_observed_at = observed_times[0]
            start_index = 1

        for current_observed_at in observed_times[start_index:]:
            previous_observed_at = current_observed_at

        updates.append(
            _cursor_update(
                WAR_PARTICIPANT_CURSOR_KEY,
                war_day_key,
                cursor_text=previous_observed_at,
                metadata={"mode": "advance", "war_day_key": war_day_key},
            )
        )
    return signals, updates


@db.managed_connection
def ingest_live_war_state(conn=None, *, refresh_race_log=True):
    """Fetch and persist live war data without generating signals."""
    war = cr_api.get_current_war()
    if war:
        db.upsert_war_current_state(war, conn=conn)
    race_log_items = 0
    race_log_refreshed = False
    if refresh_race_log:
        try:
            race_log = cr_api.get_river_race_log()
        except requests.RequestException as exc:
            log.warning("War ingest: failed to refresh river race log: %s", exc)
            race_log = None
        if race_log:
            race_log_items = db.store_war_log(race_log, cr_api.CLAN_TAG, conn=conn)
            race_log_refreshed = True
    return {
        "war": war or {},
        "race_log_refreshed": race_log_refreshed,
        "race_log_items": race_log_items,
    }


@db.managed_connection
def detect_war_signals_from_storage(conn=None):
    """Run storage-backed war detection and return deferred cursor updates."""
    from heartbeat import WarAwarenessResult

    signals = []
    cursor_updates = []

    live_state_signals, live_state_updates = _scan_war_live_state_cursor(conn=conn)
    signals.extend(live_state_signals)
    cursor_updates.extend(live_state_updates)

    participant_signals, participant_updates = _scan_war_participant_cursors(conn=conn)
    signals.extend(participant_signals)
    cursor_updates.extend(participant_updates)

    signals.extend(detect_war_battle_activity(conn=conn))
    signals.extend(detect_war_surprise_participants(conn=conn))
    signals.extend(detect_war_rival_activity(conn=conn))

    war_signals = detect_war_completion(conn=conn, refresh_log=False)
    signals.extend(war_signals)
    signals.extend(detect_war_week_complete(war_signals, conn=conn))
    if war_signals:
        signals.extend(detect_war_champ_update(war_signals, conn=conn))

    war = db.get_current_war_status(conn=conn) or {}
    clan = _build_stored_clan_context(war)
    return WarAwarenessResult(
        signals=signals,
        clan=clan,
        war=war,
        cursor_updates=cursor_updates,
    )
