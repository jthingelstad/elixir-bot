"""The v5.1 engine tick — runtime.md §2's seven steps, one function.

Dependency-injected: `api` (cr_api-shaped), `send_fn`/`compose_fn` (delivery),
so the offline rehearsal reuses steps 2–7 with no network and no Discord.
Single process, single writer; each step is guarded — a step that throws logs
and the tick continues (idempotent dedup keys make re-processing safe).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from engine import baselines, delivery, ingest, management, polling, projections, recognition
from engine.clock import infer_season_id, war_clock
from engine.db import canon_tag, chicago_today, ensure_player, utcnow
from engine.emitters import emit
from engine.emitters.clan import (
    calendar_already_ran,
    emit_calendar,
    mark_calendar_ran,
    project_clan_aspects,
)
from engine.emitters.player import project_player_aspects
from engine.emitters.war import project_race_aspect

log = logging.getLogger("engine.tick")

HOME_CLAN = "#J2RGCRVG"
TRAINING_RIVERRACE_POLL_SECONDS = 3600  # hourly on training days (runtime.md §4)


def _guard(counters: dict, step: str):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc is not None:
                log.exception("engine tick step %s failed", step)
                counters[f"{step}_error"] = repr(exc)
                return True  # tick continues; cursor not advanced by the step
            return False

    return _Ctx()


def _riverrace_due(conn, clock, now: datetime) -> bool:
    row = baselines.get_baseline(conn, "riverrace", HOME_CLAN, "race")
    if row is None:
        return True
    if clock is None or clock.phase != "training":
        return True  # war/colosseum days: every tick
    try:
        last = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - last).total_seconds() >= TRAINING_RIVERRACE_POLL_SECONDS


def _current_clock(conn, now: datetime):
    row = baselines.get_baseline(conn, "riverrace", HOME_CLAN, "race")
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    raw = payload.get("_raw") or payload
    return war_clock(raw, now, season_id=payload.get("season_id"))


def run_tick(conn, now: datetime | None = None, *, api, send_fn, compose_fn) -> dict:
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    counters: dict = {}

    # -- step 0/1: POLL (clan heartbeat + clock-gated riverrace + budgeted players)
    clan_payload = None
    race_payload = None
    player_payloads: dict[str, dict] = {}
    battlelogs: dict[str, list] = {}

    clock = _current_clock(conn, now)

    with _guard(counters, "poll"):
        clan_payload = api.get_clan()
        if _riverrace_due(conn, clock, now):
            race_payload = api.get_current_war()
        roster_tags = [
            canon_tag(m.get("tag")) for m in (clan_payload or {}).get("memberList", [])
        ]
        polling.seed_new_members(conn, roster_tags, now_iso)
        plan = polling.plan(conn, now_iso)
        counters["planned_calls"] = len(plan)
        for endpoint, tag in plan:
            ensure_player(conn, tag, None, now_iso)
            if endpoint == "battlelog":
                battlelogs[tag] = api.get_player_battle_log(tag) or []
                polling.note_polled(conn, tag, "battlelog", now_iso)
            else:
                player_payloads[tag] = api.get_player(tag) or {}
                polling.note_polled(conn, tag, "profile", now_iso)

    # refresh the clock from the fresh race payload before ingest needs it
    if race_payload:
        season_id = infer_season_id(conn, race_payload)
        clock = war_clock(race_payload, now, season_id=season_id)

    # -- step 2: INGEST — battle mirror (war keys from the battle's own time)
    with _guard(counters, "ingest"):
        new_battles_total = 0
        for tag, battlelog in battlelogs.items():
            n = ingest.mirror_battles(conn, tag, battlelog, now_iso, clock, now=now)
            new_battles_total += n
            if n:
                polling.update_heat(conn, tag, new_battles=True, now=now_iso)
        counters["battles_ingested"] = new_battles_total

    # -- step 3: EMIT — diff baselines → events; calendar once per Chicago day
    with _guard(counters, "emit"):
        emitted = 0
        if clan_payload:
            for aspect, aspect_payload in project_clan_aspects(clan_payload).items():
                emitted += emit(conn, "clan", HOME_CLAN, aspect, aspect_payload, now_iso)
        if race_payload and clock and clock.season_id is not None:
            race_aspect = project_race_aspect(race_payload, clock.season_id)
            emitted += emit(conn, "riverrace", HOME_CLAN, "race", race_aspect, now_iso)
        for tag, payload in player_payloads.items():
            aspects = project_player_aspects(payload)
            for aspect, aspect_payload in aspects.items():
                emitted += emit(conn, "player", tag, aspect, aspect_payload, now_iso)
        today = chicago_today()
        if not calendar_already_ran(conn, today):
            emitted += emit_calendar(conn, today)
            mark_calendar_ran(conn, today)
        counters["events_emitted"] = emitted
        polling.decay_all(conn, now_iso)
        conn.commit()

    # -- step 4: PROJECT — refresh what the polls touched
    with _guard(counters, "project"):
        roster_by_tag = {
            canon_tag(m.get("tag")): m
            for m in (clan_payload or {}).get("memberList", [])
        }
        touched = set(player_payloads) | set(battlelogs) | set(roster_by_tag)
        today = chicago_today()
        for tag in touched:
            profile = player_payloads.get(tag)
            projections.refresh_player_state(
                conn, tag, profile, roster_by_tag.get(tag), now_iso
            )
            if profile and profile.get("cards"):
                projections.refresh_card_collection(conn, tag, profile, now_iso)
            if tag in battlelogs:
                projections.refresh_form(conn, tag, now=now_iso)
                projections.refresh_rollups(conn, tag, today)
            projections.refresh_management_inputs(conn, tag, now=now_iso)
        if clan_payload:
            projections.refresh_clan_rollups(conn, clan_payload, today, now_iso)
        counters["players_projected"] = len(touched)
        conn.commit()

    # -- step 5: MANAGE — kick_state is reactive (Q1); weekly grain rolls in the review
    with _guard(counters, "manage"):
        transitions = management.run_tick_evaluators(conn, now=now_iso)
        counters["kick_transitions"] = len(transitions)
        if transitions:
            from storage.leader_actions import create_leader_action_recommendation

            for t in transitions:
                create_leader_action_recommendation(
                    action_type="kick_recommendation",
                    target_player_tag=t["player_tag"],
                    target_player_name=t.get("player_name"),
                    objective=t.get("objective")
                    or f"Review kick candidacy for {t['player_tag']}",
                    rationale=t.get("rationale") or json.dumps(t),
                    source_signal_key=f"engine:kick:{t['player_tag']}:{now_iso}",
                    source_signal_type="engine_kick_state",
                    conn=conn,
                )
        conn.commit()

    # -- step 6: RECOGNIZE — score → coalesce → claim → raise intents
    with _guard(counters, "recognize"):
        clock_dict = asdict(clock) if clock is not None else None
        rec = recognition.run_recognizers(conn, clock_dict, now_iso)
        counters.update({f"recognize_{k}": v for k, v in rec.items()})
        conn.commit()

    # -- step 7: DELIVER — at-least-once
    with _guard(counters, "deliver"):
        d = delivery.consume(conn, send_fn, compose_fn, now_iso)
        counters.update({f"deliver_{k}": v for k, v in d.items()})
        conn.commit()

    counters["tick_completed_at"] = utcnow()
    return counters
