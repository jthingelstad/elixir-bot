"""Assemble "the read" — the situation snapshot the awareness loop reviews.

``build_read`` collapses the v5.1 storage palette (the same modules the
Observatory reads through ``runtime/webapp/queries.py``) into a single dict
handed to ``agent.workflows.run_awareness_tick``. It is pure: local DB reads
only, no Discord I/O and no LLM call.

Every block is optional and degrades independently — a loader that raises
appends its name to ``_degraded`` and yields an empty/None block, so the read
renders sanely on an empty v5.1 DB. This mirrors the v4-era ``_note_degraded``
discipline, rebuilt from current v5.1 sources.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

import db
from capabilities import game_modes as game_mode_capability
from storage import cases, events_read, leader_actions, revisits, war_analytics, war_status

log = logging.getLogger("elixir")


# Signals the awareness loop is REQUIRED to address (awareness.md "Hard-Post
# Floors"). Both the doc names and the concrete v5.1 event_types are listed so
# the derivation works regardless of which vocabulary the stream carries.
HARD_POST_EVENT_TYPES = frozenset({
    "war_battle_rank_change",
    "war_week_complete",
    "war_season_complete",
    "capability_unlock",
    "member_join",
    "member_leave",
    # concrete v5.1 event_stream types
    "member_joined",
    # NB: raw "member_left" is deliberately NOT a hard-post — a departure's public
    # goodbye is HELD until a leader verifies it was an organic leave (vs a kick).
    # The verified-leave signal below is what earns a farewell; a kick earns none.
    "member_left_verified",
    "week_finished",
    "season_closed",
    # The clan's OWN founding anniversary is a can't-miss celebration (unlike
    # member birthdays / join anniversaries, which stay discretionary). It fires
    # once a year and ages out of the delta feed after one covered tick, so it
    # won't repeat-fail; cake_days_today keeps it visible all day as backup.
    "clan_birthday",
})


# event_type → lane. Player-stream events default to "milestone"; the streams
# themselves (player/clan/war) give the fallback lane.
_LANE_BY_EVENT_TYPE: dict[str, str] = {
    # war
    "season_closed": "war",
    "season_started": "war",
    "war_day_opened": "war",
    "week_finished": "war",
    "war_battle_rank_change": "war",
    # clan_event
    "member_joined": "clan_event",
    "member_left": "clan_event",
    "member_left_verified": "clan_event",
    "role_changed": "clan_event",
    "weekly_donation_leader": "clan_event",
    # award-race lead changes (War Champ / Rookie MVP) — see award_races block
    "war_champ_lead_change": "clan_event",
    "rookie_mvp_lead_change": "clan_event",
    # clan_event — calendar "cake days" (also surfaced all-day via cake_days_today)
    "member_birthday": "clan_event",
    "clan_birthday": "clan_event",
    "join_anniversary": "clan_event",
    "cr_account_anniversary": "clan_event",
    # battle_mode
    "pol_promotion": "battle_mode",
    "pol_season_closed": "battle_mode",
    # system
    "capability_unlock": "system",
}

_LANE_BY_STREAM: dict[str, str] = {
    "war": "war",
    "clan": "clan_event",
    "player": "milestone",
}

_LANE_KEYS = ("war", "battle_mode", "milestone", "clan_event", "leadership", "system")

# Channels whose recent traffic we surface as channel_memory. The brain posts
# only here; each delivered post is recorded in awareness_posts.
_CHANNEL_LANES = (
    "announcements",
    "elixir",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clock_block() -> dict:
    """The absolute wall-clock, in UTC plus reference conversions for the two
    largest member zones. POAP KINGS is an INTERNATIONAL clan — there is no single
    "local" time — so this is deliberately UTC-anchored: it gives the brain the
    unambiguous absolute frame (so it never misreads the raw UTC stamp as local)
    and the day-of-week for cadence reasoning. `us_central`/`india` are reference
    points (the two biggest member groups), NOT "the" local time — the shared clock
    everyone experiences identically is the GAME clock (`time` block / period_ends_at).
    """
    from zoneinfo import ZoneInfo

    now = _now()
    ct = now.astimezone(ZoneInfo("America/Chicago"))
    ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
    return {
        "utc": now.strftime("%A %Y-%m-%d %H:%M UTC"),
        "us_central_ref": ct.strftime("%A %Y-%m-%d %H:%M ") + ct.strftime("%Z"),
        "india_ref": ist.strftime("%A %Y-%m-%d %H:%M IST"),
    }


# The signal feed is a durable, oldest-first event-id backlog — not a rolling
# timestamp window. Player / clan / war are pure deltas; game events share the
# same acknowledgement boundary but also remain standing background context.
# GAME events are handled separately (see _game_context) — a new card / seasonal
# event is not a one-tick blip; the brain needs it as standing background so it
# can both announce "Ronin just appeared" AND, weeks later, recognize a member
# unlocking that new card.
_SIGNAL_STREAMS = ("player", "clan", "war")
_SIGNAL_LIMIT = 80
_GAME_CONTEXT_DAYS = 28


def _pending_event_batch(conn) -> dict:
    from runtime.awareness import store

    positions, initialized = store.ensure_event_cursors(conn)
    batch = events_read.list_events_after_cursors(
        positions,
        streams=("player", "clan", "war", "game"),
        limit=_SIGNAL_LIMIT,
        conn=conn,
    )
    batch["initialized"] = initialized
    return batch


def _signals(conn, pending_events: list[dict] | None = None) -> list[dict]:
    if pending_events is None:
        pending_events = _pending_event_batch(conn)["events"]
    return [
        event
        for event in pending_events
        if event.get("stream") in _SIGNAL_STREAMS and not event.get("backfilled")
    ]


def _game_context(conn, pending_events: list[dict] | None = None) -> dict:
    """Standing game-world background: recently-added cards and recent game
    events, over a wider window than the delta feed. ``is_new`` comes from the
    durable game-stream cursor, not wall-clock comparison, so an outage cannot
    age a change out before the brain sees it. Backfilled rows remain context
    but are never marked new."""
    if pending_events is None:
        pending_events = _pending_event_batch(conn)["events"]
    pending_game = {
        e.get("dedup_key"): e
        for e in pending_events
        if e.get("stream") == "game" and not e.get("backfilled")
    }
    rows = events_read.list_recent_events(
        days=_GAME_CONTEXT_DAYS, streams=("game",), limit=40, conn=conn,
    )
    known = {e.get("dedup_key") for e in rows}
    # A long outage may leave an unconsumed game event outside the 28-day
    # standing window. Keep it in this turn so durable means truly durable.
    rows.extend(e for key, e in pending_game.items() if key not in known)

    def _entry(e: dict) -> dict:
        payload = e.get("payload") or {}
        observed = e.get("observed_at") or ""
        return {
            "name": payload.get("name") or payload.get("title") or payload.get("event_tag"),
            "rarity": payload.get("rarity"),
            "elixir_cost": payload.get("elixir_cost"),
            "seen_at": observed[:10],
            "is_new": e.get("dedup_key") in pending_game,
        }

    cards = [_entry(e) for e in rows if e.get("event_type") == "card_added"]
    events = [_entry(e) for e in rows if e.get("event_type") == "event_started"]
    return {"recent_cards": cards, "recent_events": events, "window_days": _GAME_CONTEXT_DAYS}


_MODE_PULSE_DAYS = 7


def _mode_pulse(conn) -> dict:
    """Per-mode clan battle activity over the last 7 days across EVERY mode the
    clan plays (Path of Legends / 2v2 / events / Trophy Road / River Race /
    Friendly) — the ongoing-activity view, distinct from the one-off pol_promotion
    events in the battle_mode lane. Two parts:
      - ``mode_mix``: aggregate per mode (battles, active members, win rate, trophy
        delta) — so the brain always knows how much is happening in each mode.
      - ``top_by_mode``: the top-3 most-active NAMED members per mode (W/L, win
        rate, trophy delta; ranked rows carry PoL league) — so the brain sees WHO
        is grinding each mode, not just aggregates.
    Both are compact (~1.5K vs the ~14K raw summary). The brain drills deeper via
    the get_clan_game_modes tool."""
    snapshot = game_mode_capability.get_clan_game_modes(
        days=_MODE_PULSE_DAYS,
        limit=5,
        top_members=3,
        conn=conn,
    )

    def _mode_row(g: dict) -> dict:
        return {
            "mode": g.get("label") or g.get("mode_group"),
            "members_active": g.get("members_active"),
            "battles": g.get("battles"),
            "win_rate": g.get("win_rate"),
            "trophy_delta": g.get("trophy_delta"),
        }

    return {
        "window_days": snapshot.get("window_days"),
        "mode_mix": [_mode_row(g) for g in (snapshot.get("modes") or {}).values()],
        "top_by_mode": {
            mode.get("label") or key: list(mode.get("top_members") or [])
            for key, mode in (snapshot.get("modes") or {}).items()
        },
    }


# Cake-day event types the calendar emitter writes (engine/emitters/clan.py).
_CAKE_DAY_TYPES = (
    "member_birthday", "clan_birthday", "join_anniversary", "cr_account_anniversary",
)


def _cake_days_today(conn) -> list[dict]:
    """Today's member "cake days", surfaced for the WHOLE Chicago day rather than
    the one-tick delta window — so the brain reliably celebrates them once (it
    dedups against channel_memory) instead of missing a birthday that scrolled out
    of the signal feed between loops. ACTIVE members only: a departed ex-member is
    never surfaced (clan_birthday has a null subject and is clan-wide)."""
    today_chi = db.chicago_today()
    rows = conn.execute(
        """SELECT e.event_type, e.subject_tag, e.dedup_key, e.payload_json,
                  COALESCE(p.display_name, p.current_name) AS name
           FROM clan_events e
           LEFT JOIN players p ON p.player_tag = e.subject_tag
           WHERE e.event_type IN ({placeholders})
             AND e.observed_at LIKE ?
             AND (e.subject_tag IS NULL
                  OR EXISTS (SELECT 1 FROM clan_memberships cm
                             WHERE cm.player_tag = e.subject_tag AND cm.left_at IS NULL))
           ORDER BY e.event_type, name""".format(
            placeholders=", ".join("?" for _ in _CAKE_DAY_TYPES)
        ),
        (*_CAKE_DAY_TYPES, f"{today_chi}T%"),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        payload = _parse_json(r["payload_json"], {}) or {}
        entry = {
            "type": r["event_type"],
            "name": r["name"] or payload.get("name"),
            "subject_tag": r["subject_tag"],
            "signal_key": r["dedup_key"],
        }
        for k in ("years", "months", "is_annual"):
            if k in payload:
                entry[k] = payload[k]
        out.append(entry)
    return out


def _parse_json(value, default=None):
    import json

    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _signal_key(event: dict) -> str:
    """A stable identifier the agent echoes back in ``covers_signal_keys``."""
    key = event.get("dedup_key")
    if key:
        return str(key)
    return ":".join(str(event.get(field) or "") for field in (
        "stream", "event_type", "subject_tag", "observed_at",
    ))


def _lane_for(event: dict) -> str:
    event_type = event.get("event_type") or ""
    lane = _LANE_BY_EVENT_TYPE.get(event_type)
    if lane:
        return lane
    return _LANE_BY_STREAM.get(event.get("stream") or "", "system")


def _compact_signal(event: dict) -> dict:
    compact = {
        "signal_key": _signal_key(event),
        "event_type": event.get("event_type"),
        "stream": event.get("stream"),
        "subject_tag": event.get("subject_tag"),
        "observed_at": event.get("observed_at"),
        "scope": event.get("scope"),
    }
    # Tier the signal so the brain can tell a notable moment from routine grind
    # without re-deriving it. Badges: a one-off badge (no level) is a Legendary
    # badge — the game's "notable achievement" tier — vs a leveled mastery /
    # progression bump, which is routine. Arena changes carry the new arena.
    payload = event.get("payload") or {}
    et = event.get("event_type")
    if et == "badge_earned":
        compact["badge_name"] = payload.get("badge_name") or payload.get("name")
        compact["badge_tier"] = "legendary" if payload.get("level") is None else "routine"
    elif et in ("arena_changed", "arena_up"):
        compact["arena_name"] = payload.get("arena_name")
    return compact


# --------------------------------------------------------------------- blocks

def _war_clock_dict(conn) -> dict | None:
    """Live war clock rebuilt from the stored race projection — the same
    adapter as ``runtime.webapp.queries._war_clock_dict``."""
    from engine.clock import war_clock
    from engine.tick import HOME_CLAN

    row = conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind='riverrace' AND entity_tag=? AND aspect='race'",
        (HOME_CLAN,),
    ).fetchone()
    if row is None:
        return None
    p = _parse_json(row["payload_json"], {}) or {}
    cr_shaped = {
        "periodType": p.get("period_type"),
        "periodIndex": p.get("period_index"),
        "sectionIndex": p.get("section_index"),
        "clan": {"tag": p.get("our_tag"), "fame": p.get("our_fame")},
    }
    clock = asdict(war_clock(cr_shaped, _now(), season_id=p.get("season_id")))
    clock["baseline_observed_at"] = row["observed_at"]
    return clock


def _time_block(conn, war: dict | None) -> dict | None:
    """Authoritative "what moment is it in the war". None when no war is
    active. Built from war_status (which itself derives from the war clock);
    falls back to the raw war clock if war_status is unavailable."""
    if war:
        return {
            "phase": war.get("phase"),
            "phase_display": war.get("phase_display"),
            "day_number": (
                war.get("battle_day_number")
                if war.get("battle_day_number") is not None
                else war.get("practice_day_number")
            ),
            "is_final_battle_day": bool(war.get("final_battle_day_active")),
            "is_final_practice_day": bool(war.get("final_practice_day_active")),
            "is_colosseum_week": bool(war.get("colosseum_week")),
            "season_id": war.get("season_id"),
            "week": war.get("week"),
            "period_ends_at": war.get("period_ends_at"),
        }
    clock = _war_clock_dict(conn)
    if not clock:
        return None
    return {
        "phase": clock.get("phase"),
        "season_id": clock.get("season_id"),
        "week": clock.get("week"),
        "is_colosseum_week": bool(clock.get("is_colosseum_week")),
    }


def _standing_block(war: dict | None) -> dict | None:
    """The two River Race scoreboards, kept strictly separate so they can never
    be conflated:
      - ``weekly``: the fame boat race — who wins the week. Fame is 0 until the
        first battle day closes (``boat_scored`` says whether it has started).
        Absent in Colosseum weeks, which have no weekly fame.
      - ``today``: the period-point race — what players are driving right now;
        resets each day. Present only once the day has scored.
    ``primary_metric`` names the race that decides this week ("fame" normally,
    "period_points" in Colosseum). A clan's period points and another clan's
    fame are DIFFERENT races — never compare across the two blocks."""
    if not war:
        return None
    race = war.get("race_standings") or []
    day = war.get("day_standings") or []
    if not race and not day:
        return None
    colosseum = bool(war.get("colosseum_week"))

    weekly = None
    if race and not colosseum:
        us = next((s for s in race if s.get("is_us")), None)
        if us is not None:
            our_fame = us.get("fame") or 0
            leader_fame = race[0].get("fame") or 0
            # The weekly fame race is only RANKED once a clan has scored. Until
            # then (e.g. a practice day, every clan at 0 fame) the API's rank is
            # an arbitrary tiebreaker order, NOT a standing — surface it as
            # unranked so the brain never cites a phantom "rank N".
            race_ranked = any((s.get("fame") or 0) > 0 for s in race)
            weekly = {
                "race_ranked": race_ranked,
                "rank": us.get("rank") if race_ranked else None,
                "unranked_reason": None if race_ranked else "no clan has scored yet — the race is not ranked until the first battle day closes",
                "fame": our_fame,
                "leader_fame": leader_fame,
                "deficit_to_leader": (leader_fame - our_fame) if (race_ranked and us.get("rank") != 1) else 0,
                "finish_line": war.get("finish_line"),
                "boat_scored": bool(war.get("boat_scored")),
                # Fame projected at today's close = placement + boat-defense fame
                # (both from the API). clinches_finish_today => we cross the finish
                # line tonight, winning the week a day early.
                "projected_fame_at_close": war.get("projected_fame_at_close"),
                "projected_defense_fame": war.get("projected_defense_fame"),
                "defenses_remaining": war.get("defenses_remaining"),
                "clinches_finish_today": bool(war.get("clinches_finish_today")),
                "scoreboard": [
                    {"name": s.get("clan_name"), "fame": s.get("fame") or 0,
                     "rank": s.get("rank") if race_ranked else None}
                    for s in race
                ],
            }

    today = None
    if day and war.get("day_scored"):
        us = next((s for s in day if s.get("is_us")), None)
        if us is not None:
            our_pp = us.get("period_points") or 0
            leader_pp = day[0].get("period_points") or 0
            today = {
                "rank": us.get("rank"),
                "period_points": our_pp,
                "leader_period_points": leader_pp,
                "deficit_to_leader": (leader_pp - our_pp) if us.get("rank") != 1 else 0,
                # Fame we'd bank at day close if this rank holds (placement only;
                # defenses add more, unseen). Mirrors the in-game boat projection.
                "projected_fame_if_held": war.get("projected_day_fame"),
                "scoreboard": [
                    {"name": s.get("clan_name"), "period_points": s.get("period_points") or 0, "rank": s.get("rank")}
                    for s in day
                ],
            }

    if weekly is None and today is None:
        return None
    return {
        "primary_metric": war.get("primary_metric") or "fame",
        "field_size": len(race) or len(day),
        "weekly": weekly,
        "today": today,
    }


def _channel_memory(conn) -> dict:
    """Recent per-lane awareness posts, so the agent avoids repeating itself."""
    out: dict[str, dict] = {}
    for lane in _CHANNEL_LANES:
        posts = [dict(r) for r in conn.execute(
            "SELECT posted_at, discord_message_id, content_preview "
            "FROM awareness_posts WHERE lane = ? "
            "ORDER BY posted_at DESC LIMIT 5",
            (lane,),
        ).fetchall()]
        out[lane] = {
            "recent_posts": [
                {
                    "posted_at": r.get("posted_at"),
                    "posted": bool(r.get("discord_message_id")),
                    "preview": str(r.get("content_preview") or "")[:200],
                }
                for r in posts
            ],
        }
    return out


# Per-member spotlight cooldown window: members solo-highlighted this recently
# should not be re-soloed for a ROUTINE milestone (the brain applies the rule).
# 48h (was 72h): 72h stacked with roundup-preference into ~zero discretionary
# posts for a day (2026-07-12 audit); notable tiers (Legendary badge, arena
# climb, first-legendary) are cooldown-EXEMPT per the awareness prompt.
_SPOTLIGHT_LOOKBACK_HOURS = 48


def _recent_member_spotlights(conn) -> list[dict]:
    """Members already highlighted in a recent #elixir milestone / clan_event post,
    newest per member, so the brain can apply a per-member cooldown instead of
    re-soloing the same active players (the 24h review saw Andy spotlighted 3×,
    Sandeep twice for a 142-trophy incremental peak). Sourced from the plan
    history in ``awareness_thoughts``."""
    rows = conn.execute(
        "SELECT at, plan_json FROM awareness_thoughts "
        "WHERE chose_silence = 0 AND at >= datetime('now', ?) "
        "ORDER BY at DESC LIMIT 40",
        (f"-{_SPOTLIGHT_LOOKBACK_HOURS} hour",),
    ).fetchall()
    seen: dict[str, dict] = {}
    for r in rows:
        try:
            plan = json.loads(r["plan_json"] or "{}")
        except (ValueError, TypeError):
            continue
        for p in plan.get("posts", []):
            if p.get("leads_with") not in ("milestone", "clan_event"):
                continue
            names = p.get("member_names") or []
            solo = len(names) == 1
            for tag, name in zip(p.get("member_tags") or [], names):
                if not tag or tag in seen:
                    continue  # rows are DESC — first seen is the most recent
                seen[tag] = {
                    "member_tag": tag,
                    "member_ref": name,
                    "at": r["at"],
                    "solo": solo,
                    "summary": (p.get("summary") or "")[:100],
                }
    return list(seen.values())


# Clan heartbeat: hours since Elixir last posted anything member-facing. The
# brain uses this to avoid flat-lining — a long quiet stretch with any notable
# signal in the read should lean toward a warm roundup (see the awareness prompt).
_HEARTBEAT_QUIET_HOURS = 10


def _posting_pulse(conn) -> dict:
    """When did Elixir last post, and how long ago. Sourced from the fulfilled
    awareness_posts ledger. ``hours_since_last_post``
    is None on the very first run (no prior post)."""
    row = conn.execute("SELECT MAX(posted_at) FROM awareness_posts").fetchone()
    last = row[0] if row and row[0] else None
    hours = None
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hours = round((_now() - dt).total_seconds() / 3600.0, 1)
        except (ValueError, TypeError):
            hours = None
    return {
        "last_post_at": last,
        "hours_since_last_post": hours,
        "quiet_threshold_hours": _HEARTBEAT_QUIET_HOURS,
        "is_quiet_stretch": bool(hours is not None and hours >= _HEARTBEAT_QUIET_HOURS),
    }


def _recent_agent_writes(limit: int = 10) -> list[dict]:
    """Recent leadership-scope memories Elixir authored, so the agent avoids
    re-flagging a watch or re-writing an arc it just recorded.

    The memory store lives in its own database (``managed_memory_connection``
    opens it per call), so this never borrows the operational connection.
    """
    import memory_store

    mconn = memory_store.get_memory_connection()
    try:
        memory_store.ensure_memory_schema(mconn)
    finally:
        mconn.close()
    memories = memory_store.list_memories(viewer_scope="leadership", limit=limit * 3)
    out: list[dict] = []
    for m in memories:
        if m.get("source_type") not in {"elixir_inference", "elixir_synthesis"}:
            continue
        out.append({
            "memory_id": m.get("memory_id"),
            "title": m.get("title"),
            "tags": m.get("tags") or [],
            "member_tag": m.get("member_tag"),
            "created_at": m.get("created_at"),
        })
        if len(out) >= limit:
            break
    return out


def _leader_action_board(conn) -> dict:
    """Open #actions cards (undecided asks) + recent decisions."""
    open_cards = leader_actions.list_leader_actions(status="proposed", limit=15, conn=conn)
    recent = leader_actions.list_leader_actions(limit=15, conn=conn)
    decided = [a for a in recent if (a.get("status") or "proposed") != "proposed"]

    def _compact(action: dict) -> dict:
        return {
            "action_id": action.get("action_id"),
            "action_type": action.get("action_type"),
            "objective": action.get("objective"),
            "status": action.get("status"),
            "target_player_tag": action.get("target_player_tag"),
            "target_player_name": action.get("target_player_name"),
            "decision_emoji": action.get("decision_emoji"),
            "decision_note": action.get("decision_note"),
            "proposed_at": action.get("proposed_at"),
            "decided_at": action.get("decided_at"),
        }

    return {
        "open": [_compact(a) for a in open_cards],
        "recent_decisions": [_compact(a) for a in decided[:10]],
    }


def _decision_cases(conn) -> dict:
    """Compact due/open cases with no overlap — ``decision_case_snapshot``
    strips the internal ``state`` blob and dedupes ``open`` against ``due``
    ("due = needs attention now; open = already being monitored")."""
    return cases.decision_case_snapshot(open_limit=25, due_limit=25, dedupe=True, conn=conn)


def _management(conn) -> dict:
    """The engine's current promote/demote/kick verdicts — the authoritative
    source for management recommendations (see engine.management). Imported
    lazily to avoid a runtime->engine import at module load."""
    from engine.management import management_read_summary
    return management_read_summary(conn)


def _due_revisits(conn, limit: int = 20) -> list[dict]:
    rows = revisits.list_due_revisits(limit=limit, conn=conn)
    return [
        {
            "signal_key": row.get("signal_key"),
            "due_at": row.get("due_at"),
            "rationale": row.get("rationale"),
            "scheduled_at": row.get("created_at"),
        }
        for row in rows
    ]


def build_read(conn=None) -> dict:
    """Assemble the awareness read from the v5.1 storage palette.

    Pass ``conn`` to share a connection; otherwise a fresh read connection is
    opened and closed here. Every block degrades independently — the returned
    dict always carries the expected keys plus a ``_degraded`` list naming any
    block whose loader raised this build.
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    degraded: list[str] = []

    def _load(name, fn, default):
        try:
            return fn()
        except Exception:
            log.warning("build_read: block %s degraded", name, exc_info=True)
            degraded.append(name)
            return default

    try:
        war = _load("war_status", lambda: war_status.get_current_war_status(conn=conn), None)

        pending = _load(
            "event_backlog",
            lambda: _pending_event_batch(conn),
            {
                "events": [],
                "checkpoints": {},
                "heads": {},
                "remaining_by_stream": {},
                "has_more": False,
                "initialized": False,
            },
        )
        events = _signals(conn, pending.get("events") or [])
        signals_by_lane: dict[str, list[dict]] = {key: [] for key in _LANE_KEYS}
        hard_post_signals: list[dict] = []
        for event in events:
            lane = _lane_for(event)
            compact = _compact_signal(event)
            signals_by_lane.setdefault(lane, []).append(compact)
            if (event.get("event_type") or "") in HARD_POST_EVENT_TYPES:
                hard_post_signals.append(compact)

        read = {
            "generated_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clock": _load("clock", _clock_block, None),
            "time": _load("time", lambda: _time_block(conn, war), None),
            "standing": _load("standing", lambda: _standing_block(war), None),
            "war_season": _load(
                "war_season", lambda: war_status.get_war_season_snapshot(conn=conn), None
            ),
            "award_races": _load(
                "award_races", lambda: db.get_award_races(conn=conn),
                {"war_champ": [], "iron_king": [], "rookie_mvp": []},
            ),
            # Season-by-season War Champ + free-pass lineage (rolling 6), so a
            # war-week/season recap or a free-pass designation reflects the deep
            # history, not just the current season.
            "war_history": _load(
                "war_history", lambda: war_analytics.get_war_season_history(conn=conn), None
            ),
            "signals_by_lane": signals_by_lane,
            "hard_post_signals": hard_post_signals,
            "game_context": _load(
                "game_context", lambda: _game_context(conn, pending.get("events") or []),
                {"recent_cards": [], "recent_events": [], "window_days": _GAME_CONTEXT_DAYS},
            ),
            "mode_pulse": _load(
                "mode_pulse", lambda: _mode_pulse(conn),
                {"mode_mix": [], "top_by_mode": {}, "window_days": _MODE_PULSE_DAYS},
            ),
            "cake_days_today": _load("cake_days_today", lambda: _cake_days_today(conn), []),
            "decision_cases": _load("decision_cases", lambda: _decision_cases(conn), {"due": [], "open": []}),
            "channel_memory": _load("channel_memory", lambda: _channel_memory(conn), {}),
            "recent_member_spotlights": _load(
                "recent_member_spotlights", lambda: _recent_member_spotlights(conn), []),
            "posting_pulse": _load("posting_pulse", lambda: _posting_pulse(conn), {}),
            "recent_agent_writes": _load("recent_agent_writes", lambda: _recent_agent_writes(), []),
            "leader_action_board": _load(
                "leader_action_board", lambda: _leader_action_board(conn),
                {"open": [], "recent_decisions": []},
            ),
            "management": _load(
                "management", lambda: _management(conn),
                {"actionable": {"kick": [], "promote": [], "demote": []},
                 "building_counts": {}, "members_evaluated": 0},
            ),
            "due_revisits": _load("due_revisits", lambda: _due_revisits(conn), []),
        }
        read["_degraded"] = degraded
        read["_signal_count"] = len(events)
        read["_event_cursor_checkpoints"] = pending.get("checkpoints") or {}
        read["_event_backlog"] = {
            "has_more": bool(pending.get("has_more")),
            "remaining_by_stream": pending.get("remaining_by_stream") or {},
        }
        # The first post-cutover read may initialize stream_cursors from the
        # last successful historical thought. Persist that bridge before this
        # connection closes; subsequent advancement stays atomic with thoughts.
        if own and pending.get("initialized"):
            conn.commit()
        if degraded:
            log.info("build_read: %d block(s) degraded: %s", len(degraded), ", ".join(degraded))
        return read
    finally:
        if own:
            conn.close()


__all__ = ["build_read", "HARD_POST_EVENT_TYPES"]
