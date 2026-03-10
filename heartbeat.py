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


# ── Signal detectors ─────────────────────────────────────────────────────────
# Each returns a list of signal dicts (may be empty).


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
        if not db.was_signal_sent("war_battle_phase_active", today, conn=conn):
            signals.append({
                "type": "war_battle_phase_active",
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
        if not db.was_signal_sent("war_practice_phase_active", today, conn=conn):
            signals.append({
                "type": "war_practice_phase_active",
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
        if not db.was_signal_sent("war_final_practice_day", today, conn=conn):
            signals.append({
                "type": "war_final_practice_day",
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
        if not db.was_signal_sent("war_final_battle_day", today, conn=conn):
            signals.append({
                "type": "war_final_battle_day",
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
        if not db.was_signal_sent("war_battle_days_complete", today, conn=conn):
            signals.append({
                "type": "war_battle_days_complete",
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


def detect_war_deck_usage(war_data, conn=None):
    """Check who has and hasn't used their 4 war decks today.

    war_data: dict from cr_api.get_current_war().
    Returns a signal with players who used decks and who haven't, only on battle days.
    Only fires once per day.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if db.was_signal_sent("war_deck_usage", today, conn=conn):
        return []
    current_war = db.get_current_war_status(conn=conn)
    if not current_war or not current_war.get("battle_phase_active"):
        return []

    if not war_data or war_data.get("state") in (None, "notInWar"):
        return []

    participants = war_data.get("clan", {}).get("participants", [])
    if not participants:
        return []

    used_all = []
    used_some = []
    used_none = []

    for p in participants:
        decks_today = p.get("decksUsedToday", 0)
        name = p.get("name", "?")
        tag = p.get("tag", "")
        if decks_today >= 4:
            used_all.append({"name": name, "tag": tag, "decks": decks_today})
        elif decks_today > 0:
            used_some.append({"name": name, "tag": tag, "decks": decks_today})
        else:
            used_none.append({"name": name, "tag": tag, "decks": 0})

    return [{
        "type": "war_deck_usage",
        "used_all_4": used_all,
        "used_some": used_some,
        "used_none": used_none,
        "total_participants": len(participants),
    }]


def detect_war_champ_update(conn=None):
    """Generate a War Champ standings signal when a new war result has been stored.

    This is triggered after detect_war_completion stores a new result.
    Returns the current season standings so the LLM can share weekly rankings.
    Also includes perfect participation info.
    """
    standings = db.get_war_champ_standings(conn=conn)
    if not standings:
        return []

    season_id = db.get_current_season_id(conn=conn)
    perfect = db.get_perfect_war_participants(season_id=season_id, conn=conn)

    signals = [{
        "type": "war_champ_standings",
        "season_id": season_id,
        "standings": standings[:10],  # Top 10
        "leader": standings[0] if standings else None,
        "perfect_participants": perfect,
    }]
    return signals


def detect_war_completion(clan_tag, conn=None):
    """Fetch river race log and check for newly completed wars.

    Stores any new results in the DB and returns signals for races
    that weren't previously recorded.
    """
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
        # Check what we already have
        existing = set()
        for row in conn.execute("SELECT season_id, section_index FROM war_races").fetchall():
            existing.add((row["season_id"], row["section_index"]))

        # Store new results
        db.store_war_log(race_log, clan_tag, conn=conn)

        # Find newly added results
        signals = []
        for entry in race_log.get("items", []):
            key = (entry.get("seasonId"), entry.get("sectionIndex"))
            if key in existing:
                continue

            # This is a new war result — generate a signal
            standings = entry.get("standings", [])
            our = None
            for s in standings:
                clan = s.get("clan", {})
                if clan.get("tag", "").replace("#", "") == clan_tag.replace("#", ""):
                    our = s
                    break

            if our:
                signals.append({
                    "type": "war_completed",
                    "season_id": entry.get("seasonId"),
                    "section_index": entry.get("sectionIndex"),
                    "our_rank": our.get("rank"),
                    "our_fame": our.get("clan", {}).get("fame", 0),
                    "total_clans": len(standings),
                    "won": our.get("rank") == 1,
                })

        return signals
    finally:
        if close:
            conn.close()


def detect_cake_days(today_str=None, conn=None):
    """Check for clan birthday, join anniversaries, and member birthdays.

    Uses cake_day_announcements table for dedup — only returns signals
    for events not yet announced today. Marks them as announced.

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
                db.mark_announcement_sent(today_str, "clan_birthday", None, conn=conn)

        # Join anniversaries
        anniversaries = db.get_join_anniversaries_today(today_str, conn=conn)
        unannounced = []
        for a in anniversaries:
            if not db.was_announcement_sent(today_str, "join_anniversary", a["tag"], conn=conn):
                unannounced.append(a)
                db.mark_announcement_sent(today_str, "join_anniversary", a["tag"], conn=conn)
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
                db.mark_announcement_sent(today_str, "birthday", b["tag"], conn=conn)
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

def tick(conn=None):
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

        # Join/leave detection
        join_leave_signals, _ = detect_joins_leaves(members, known)
        signals.extend(join_leave_signals)

        # Record join dates for newly detected members; reset tenure for leavers
        for sig in join_leave_signals:
            if sig["type"] == "member_join":
                db.record_join_date(sig["tag"], sig["name"],
                                    datetime.now().strftime("%Y-%m-%d"), conn=conn)
            elif sig["type"] == "member_leave":
                db.clear_member_tenure(sig["tag"], conn=conn)

        # Backfill join dates from historical snapshots (idempotent)
        db.backfill_join_dates(conn=conn)

        # Arena changes
        signals.extend(detect_arena_changes(conn=conn))

        # Role changes
        signals.extend(detect_role_changes(conn=conn))

        # War day awareness
        signals.extend(detect_war_day_transition(conn=conn))

        # Live war week/season rollovers
        signals.extend(detect_war_rollovers(conn=conn))

        # War deck usage — thank players who used 4 decks, nudge those who haven't
        signals.extend(detect_war_deck_usage(war, conn=conn))

        # War completion + War Champ standings
        clan_tag = cr_api.CLAN_TAG
        war_signals = detect_war_completion(clan_tag, conn=conn)
        signals.extend(war_signals)

        # If a war just completed, also share War Champ standings
        if war_signals:
            signals.extend(detect_war_champ_update(conn=conn))

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

        # Mark emitted signals so they don't re-fire today
        today = datetime.now().strftime("%Y-%m-%d")
        for sig in signals:
            if sig.get("signal_key"):
                continue
            db.mark_signal_sent(sig.get("signal_log_type") or sig["type"], today, conn=conn)

        log.info("Heartbeat: %d signals detected", len(signals))
        return HeartbeatTickResult(signals=signals, clan=clan, war=war)
    finally:
        if close:
            conn.close()
