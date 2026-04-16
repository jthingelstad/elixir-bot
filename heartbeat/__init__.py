"""heartbeat — Hourly signal detection for Elixir bot.

Runs cheap deterministic checks against fresh clan data and the SQLite
history store.  Only calls the LLM when real signals are found.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import requests

import cr_api
import cr_knowledge
import db
from heartbeat._helpers import (
    BATTLE_DAY_SECONDS,
    WAR_LIVE_STATE_CURSOR_KEY,
    WAR_PARTICIPANT_CURSOR_KEY,
    _BATTLE_DAY_CHECKPOINTS,
    _COLOSSEUM_FAME_TARGET,
    _RACE_FAME_TARGET,
    _battle_lead_payload,
    _completed_war_races,
    _compute_pace_status,
    _cursor_update,
    _detect_war_race_finished_live_for_pair,
    _enrich_leave_signal,
    _war_period_signal_log_type,
    _war_signal_date_for_state,
    _war_signal_date_for_values,
)
from heartbeat._pipeline import (
    _build_stored_clan_context,
    _scan_war_live_state_cursor,
    _scan_war_participant_cursors,
    detect_war_signals_from_storage,
    ingest_live_war_state,
)
from heartbeat._roster import (
    detect_arena_changes,
    detect_cake_days,
    detect_clan_rank_top_spot,
    detect_clan_score_records,
    detect_deck_archetype_changes,
    detect_form_slumps,
    detect_returning_members,
    detect_donation_leaders,
    detect_inactivity,
    detect_joins_leaves,
    detect_pending_system_signals,
    detect_role_changes,
    detect_weekly_donation_leader,
)
from heartbeat._war import (
    _detect_war_day_markers_for_pair,
    _detect_war_day_transition_for_pair,
    _detect_war_rank_changes_for_pair,
    _detect_war_rollovers_for_pair,
    _detect_war_season_completion_for_pair,
    build_situation_time,
    detect_war_battle_checkpoints,
    detect_war_battle_final_hours,
    detect_war_champ_update,
    detect_war_completion,
    detect_war_day_markers,
    detect_war_day_transition,
    detect_war_deck_usage,
    detect_war_rank_changes,
    detect_war_rollovers,
    detect_war_season_completion,
    detect_war_week_complete,
)

log = logging.getLogger("elixir_heartbeat")


@dataclass
class HeartbeatTickResult:
    """Full heartbeat output bundle for downstream consumers."""
    signals: list
    clan: dict
    war: dict


@dataclass
class WarAwarenessResult:
    """Stored-war detection bundle plus deferred cursor updates."""
    signals: list
    clan: dict
    war: dict
    cursor_updates: list[dict]


@db.managed_connection
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
    except requests.RequestException as e:
        log.error("Heartbeat: failed to fetch clan data: %s", e)
        return HeartbeatTickResult(signals=[], clan={}, war={})

    members = clan.get("memberList", [])
    if not members:
        log.warning("Heartbeat: empty member list from API")
        return HeartbeatTickResult(signals=[], clan=clan, war={})

    war = {}
    if include_war:
        try:
            war = cr_api.get_current_war()
        except requests.RequestException:
            war = {}

    # 1. Get known roster BEFORE snapshotting (so we compare old vs new)
    known = db.get_active_roster_map(conn=conn)

    # 2. Snapshot current state
    db.snapshot_members(members, conn=conn)
    if include_war and war:
        db.upsert_war_current_state(war, conn=conn)

    # 3. Collect signals from all detectors
    signals = []

    db.snapshot_clan_daily_metrics(clan, conn=conn)

    # Backfill join dates from historical snapshots (idempotent)
    db.backfill_join_dates(conn=conn)

    if include_nonwar:
        # Join/leave detection
        join_leave_signals, _ = detect_joins_leaves(members, known, conn=conn)
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

        # Weekly donation leader — Mondays, covers the prior CR week.
        signals.extend(detect_weekly_donation_leader(conn=conn))

        # Inactivity
        signals.extend(detect_inactivity(members, conn=conn))

        # Inverse of inactivity: members who went dark and are back.
        signals.extend(detect_returning_members(conn=conn))

        # Leapfrogging into clan rank #1 is a durable #player-progress moment.
        signals.extend(detect_clan_rank_top_spot(conn=conn))

        # Form slumps: reliable player drops from strong/hot into slumping/cold.
        signals.extend(detect_form_slumps(conn=conn))

        # Deck archetype swap: deck now differs by 4+ cards from 24h ago.
        signals.extend(detect_deck_archetype_changes(conn=conn))

        # Clan-level trophy / war-trophy records (new all-time high).
        signals.extend(detect_clan_score_records(conn=conn))

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
        war_signals = detect_war_completion(clan_tag, conn=conn, refresh_log=True)
        signals.extend(war_signals)
        signals.extend(detect_war_week_complete(war_signals, conn=conn))
        signals.extend(detect_war_season_completion(conn=conn))

        # If a war just completed, also share War Champ standings
        if war_signals:
            signals.extend(detect_war_champ_update(war_signals, conn=conn))

    log.info("Heartbeat: %d signals detected", len(signals))
    return HeartbeatTickResult(signals=signals, clan=clan, war=war)


__all__ = [
    # Constants
    "BATTLE_DAY_SECONDS",
    "WAR_LIVE_STATE_CURSOR_KEY",
    "WAR_PARTICIPANT_CURSOR_KEY",
    # Dataclasses
    "HeartbeatTickResult",
    "WarAwarenessResult",
    # Main entry point
    "tick",
    # Roster detectors
    "detect_joins_leaves",
    "detect_arena_changes",
    "detect_role_changes",
    "detect_donation_leaders",
    "detect_inactivity",
    "detect_returning_members",
    "detect_clan_rank_top_spot",
    "detect_clan_score_records",
    "detect_weekly_donation_leader",
    "detect_deck_archetype_changes",
    "detect_form_slumps",
    "detect_cake_days",
    "detect_pending_system_signals",
    # War detectors
    "detect_war_day_transition",
    "detect_war_rollovers",
    "detect_war_day_markers",
    "detect_war_battle_final_hours",
    "detect_war_rank_changes",
    "detect_war_battle_checkpoints",
    "detect_war_deck_usage",
    "detect_war_week_complete",
    "detect_war_season_completion",
    "detect_war_completion",
    "detect_war_champ_update",
    # Situation helpers
    "build_situation_time",
    # Pipeline
    "ingest_live_war_state",
    "detect_war_signals_from_storage",
]
