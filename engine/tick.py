"""The v5.1 materialization tick — runtime.md §2's five steps, one function.

Dependency-injected: `api` (cr_api-shaped), so offline rehearsal reuses the
data path with no network or Discord.
Single process, single writer; each step is guarded — a step that throws logs
and the tick continues (idempotent dedup keys make re-processing safe).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from engine import baselines, management, materialize, observations, polling, readiness
from engine.db import canon_tag, utcnow

log = logging.getLogger("engine.tick")

HOME_CLAN = "#J2RGCRVG"
TRAINING_RIVERRACE_POLL_SECONDS = 3600  # hourly on training days (runtime.md §4)

_OBSERVATION_COUNTER_PREFIX = {
    "clan": "clan",
    "currentriverrace": "race",
    "player": "profile",
    "player_battlelog": "battlelog",
}


def _count_admission(counters: dict, result, contract_rejections: dict) -> bool:
    """Count one admission decision and retain contract failures for one
    endpoint-level incident per tick.  Transport failures already have CR API
    telemetry, so the engine records them in counters without duplicating an
    incident for every unavailable member endpoint."""
    prefix = _OBSERVATION_COUNTER_PREFIX[result.endpoint]
    if result.accepted:
        counters["observations_accepted"] = counters.get("observations_accepted", 0) + 1
        counters[f"{prefix}_observations_accepted"] = (
            counters.get(f"{prefix}_observations_accepted", 0) + 1
        )
        return True
    counters["observation_rejections"] = counters.get("observation_rejections", 0) + 1
    counters[f"{prefix}_observation_rejections"] = (
        counters.get(f"{prefix}_observation_rejections", 0) + 1
    )
    if result.transport_failure:
        counters["observation_transport_failures"] = (
            counters.get("observation_transport_failures", 0) + 1
        )
        counters[f"{prefix}_observation_transport_failures"] = (
            counters.get(f"{prefix}_observation_transport_failures", 0) + 1
        )
    else:
        counters["observation_contract_rejections"] = (
            counters.get("observation_contract_rejections", 0) + 1
        )
        counters[f"{prefix}_observation_contract_rejections"] = (
            counters.get(f"{prefix}_observation_contract_rejections", 0) + 1
        )
        contract_rejections.setdefault(result.endpoint, []).append(
            {"entity_key": result.entity_key, "errors": list(result.errors)}
        )
    return False


def _record_contract_rejections(contract_rejections: dict) -> None:
    """Collapse a malformed endpoint batch into one error-log line per endpoint.

    This used to write the incident ledger and suppress a repeat while an
    identical row stayed unresolved. The ledger is gone (2026-07-28), and with
    it the cross-tick suppression: the per-tick counters already say how often
    this is firing, and an operator reading logs/elixir-error.log wants to see
    that it is still happening, not one row from 25 days ago.
    """
    if not contract_rejections:
        return
    for endpoint, rejected in contract_rejections.items():
        log.error(
            "engine.observation.%s: CR observation rejected by admission boundary: "
            "count=%s observations=%s truncated=%s",
            endpoint,
            len(rejected),
            rejected[:10],
            len(rejected) > 10,
        )


def _guard(counters: dict, step: str, conn=None):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc is not None:
                log.exception("engine tick step %s failed", step)
                counters[f"{step}_error"] = repr(exc)
                if conn is not None:
                    # Discard the failed transaction's partial writes. Polling
                    # control commits separately; apply/readiness/manage share
                    # one generation transaction — without this, a later commit
                    # persists half an emit (e.g. a member_joined event whose
                    # membership insert failed), the baseline never rolls,
                    # and the diff re-fires every tick (simulator finding:
                    # 286 welcome intents from one join).
                    conn.rollback()
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
        if last.tzinfo is None:  # engine convention: suffixless timestamps are UTC
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - last).total_seconds() >= TRAINING_RIVERRACE_POLL_SECONDS


def _anchored_clock(conn, cr_shaped: dict, now: datetime, season_id):
    """Two-pass clock: compute once to locate the current period, then again
    with the drift-adaptive anchor (the war_day_opened event's observed_at —
    carried learning: CR's reset hour skews per season)."""
    return materialize.anchored_clock(conn, cr_shaped, now, season_id)


def _current_clock(conn, now: datetime):
    """Rebuild the clock from the stored race *projection* (snake_case,
    engine.emitters.war.project_race_aspect) by adapting it back to the
    CR payload keys war_clock reads. Fresh polls this tick recompute the
    clock from the raw payload anyway; this covers startup/gap ticks."""
    return materialize.current_clock(conn, now, home_clan=HOME_CLAN)


def run_tick(conn, now: datetime | None = None, *, api) -> dict:
    """Poll → ingest → emit → project → manage.

    This production entrypoint cannot compose or deliver proactive posts. The
    awareness loop consumes its event streams independently and is now the sole
    proactive owner — the deterministic recognizer/intent pipeline it replaced
    was retired entirely in #207.
    """
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    counters: dict = {}
    materialization_id = readiness.start_materialization(
        conn, started_at=now_iso, run_kind="scheduled"
    )
    conn.commit()
    counters["materialization_id"] = materialization_id

    # -- step 0/1: POLL (clan heartbeat + clock-gated riverrace + budgeted players)
    clan_payload = None
    clan_observation = None
    race_observation = None
    player_observations: dict[str, observations.Observation] = {}
    battlelog_observations: dict[str, observations.Observation] = {}

    clock = _current_clock(conn, now)

    # Heat decays at tick START, before poll planning and before any
    # update_heat call site (battle ingest in step 2, war decksUsedToday in
    # step 3) — decaying after re-heats knocked freshly-hot players back to
    # warm in the same tick, making 'hot' unreachable and quietly adding up
    # to ~30 min recognition latency (cold review 2026-07-04 #4; matches
    # polling.decay_all's own docstring).
    with _guard(counters, "decay", conn):
        polling.decay_all(conn, now_iso)
        conn.commit()

    with _guard(counters, "poll", conn):
        contract_rejections: dict[str, list[dict]] = {}
        receipt_admissions = []
        raw_clan_payload = api.get_clan()
        result, admitted = observations.observe(
            "clan", HOME_CLAN, raw_clan_payload, now_iso, source="engine_tick"
        )
        receipt_admissions.append((result, raw_clan_payload))
        if _count_admission(counters, result, contract_rejections):
            clan_payload = raw_clan_payload
            clan_observation = admitted
        if _riverrace_due(conn, clock, now):
            raw_race_payload = api.get_current_war()
            result, admitted = observations.observe(
                "currentriverrace",
                HOME_CLAN,
                raw_race_payload,
                now_iso,
                source="engine_tick",
            )
            receipt_admissions.append((result, raw_race_payload))
            if _count_admission(counters, result, contract_rejections):
                race_observation = admitted
        # Poll planning can use the admitted roster without first mutating
        # identity, membership, or poll state. Those semantic writes belong to
        # the generation's atomic application transaction below.
        roster_tags = [
            canon_tag(member.get("tag")) for member in (clan_payload or {}).get("memberList", [])
        ]
        plan = polling.plan(conn, now_iso, roster_tags=roster_tags)
        counters["planned_calls"] = len(plan)
        for endpoint, tag in plan:
            # Release the writer BEFORE the fetch: cr_api persists the raw
            # payload on its own connection during the call, so the tick must
            # not hold a write transaction across the HTTP round-trip
            # (that's a 30 s busy-timeout stall per call — observed live).
            conn.commit()
            if endpoint == "battlelog":
                raw_battlelog = api.get_player_battle_log(tag)
                result, admitted = observations.observe(
                    "player_battlelog",
                    tag,
                    raw_battlelog,
                    now_iso,
                    source="engine_tick",
                )
                receipt_admissions.append((result, raw_battlelog))
                if _count_admission(counters, result, contract_rejections):
                    assert admitted is not None
                    battlelog_observations[tag] = admitted
            else:
                raw_player_payload = api.get_player(tag)
                result, admitted = observations.observe(
                    "player",
                    tag,
                    raw_player_payload,
                    now_iso,
                    source="engine_tick",
                )
                receipt_admissions.append((result, raw_player_payload))
                if _count_admission(counters, result, contract_rejections):
                    assert admitted is not None
                    player_observations[tag] = admitted
            conn.commit()
        for decision, payload in receipt_admissions:
            readiness.record_admission_decision(conn, decision, payload)
        _record_contract_rejections(contract_rejections)
        conn.commit()

    poll_succeeded = "poll_error" not in counters
    readiness.update_materialization(
        conn,
        materialization_id,
        status="running",
        poll_ok=poll_succeeded,
    )
    conn.commit()

    # -- steps 2-4: APPLY — one atomic observation-to-data transaction.
    # Native battle rows, emitted deltas, current-state projections, rollups,
    # and poll freshness now either advance together or not at all.
    materialization_succeeded = poll_succeeded
    if materialization_succeeded:
        with _guard(counters, "materialize", conn):
            emitted = 0
            battles_ingested = 0
            projected: set[str] = set()

            if race_observation is not None:
                applied = materialize.apply_observation(
                    conn,
                    race_observation,
                    now=now,
                    track_poll_freshness=True,
                    materialization_id=materialization_id,
                )
                emitted += applied.events_emitted
                if applied.clock is not None:
                    clock = applied.clock
            if clan_observation is not None:
                applied = materialize.apply_observation(
                    conn,
                    clan_observation,
                    now=now,
                    track_poll_freshness=True,
                    materialization_id=materialization_id,
                )
                emitted += applied.events_emitted
                projected.update(
                    canon_tag(member.get("tag"))
                    for member in (clan_payload or {}).get("memberList", [])
                )
            for tag, observation in player_observations.items():
                applied = materialize.apply_observation(
                    conn,
                    observation,
                    now=now,
                    track_poll_freshness=True,
                    materialization_id=materialization_id,
                )
                emitted += applied.events_emitted
                projected.add(tag)
            for tag, observation in battlelog_observations.items():
                applied = materialize.apply_observation(
                    conn,
                    observation,
                    clock=clock,
                    now=now,
                    track_poll_freshness=True,
                    materialization_id=materialization_id,
                )
                battles_ingested += applied.battles_ingested
                projected.add(tag)
            derived = materialize.apply_tick_derivations(
                conn,
                clock=clock,
                observed_at=now_iso,
            )
            emitted += derived.events_emitted
            counters["materialization_degraded"] = derived.degraded
            counters["battles_ingested"] = battles_ingested
            counters["events_emitted"] = emitted
            counters["players_projected"] = len(projected)
        materialization_succeeded = "materialize_error" not in counters
    else:
        counters["materialize_skipped"] = "poll_failed"

    source_freshness = None
    if materialization_succeeded:
        with _guard(counters, "readiness", conn):
            source_freshness = readiness.evaluate_source_freshness(
                conn,
                now=now_iso,
                home_clan=HOME_CLAN,
            )
            counters["management_members_ready"] = source_freshness["members_ready"]
            counters["management_members_held"] = source_freshness["members_held"]
            readiness.update_materialization(
                conn,
                materialization_id,
                status="running",
                apply_ok=True,
                source_freshness=source_freshness,
            )
        materialization_succeeded = "readiness_error" not in counters

    if not materialization_succeeded:
        counters["manage_skipped"] = "materialization_not_ready"
        counters["proactive_posting"] = "awareness_only"
        counters["tick_completed_at"] = utcnow()
        errors = {key: value for key, value in counters.items() if key.endswith("_error")}
        readiness.update_materialization(
            conn,
            materialization_id,
            status="failed",
            completed_at=counters["tick_completed_at"],
            poll_ok=poll_succeeded,
            apply_ok=False,
            manage_ok=False,
            counters=counters,
            errors=errors,
        )
        conn.commit()
        return counters

    # -- step 5: MANAGE — kick_state is reactive (Q1); weekly grain rolls in the review
    with _guard(counters, "manage", conn):
        transitions = management.run_tick_evaluators(
            conn,
            now=now_iso,
            readiness=source_freshness,
            materialization_id=materialization_id,
        )
        counters["kick_transitions"] = len(transitions)
        withdrawals = management.withdraw_stale_actions(conn, now=now_iso)
        counters["management_withdrawals"] = sum(int(w.get("count", 1)) for w in withdrawals)
        # Departure verification remains action-board state. Decision cases were
        # retired in #216; leader_action_recommendations is the sole decision
        # lifecycle and therefore needs no cross-store reconciliation.
        from storage.cases import (
            expire_departure_verification_cards,
            raise_departure_verification_cards,
        )

        # Departure verification: a leave and a kick are different signals the
        # roster diff can't tell apart. Raise a #leader-actions card for recent
        # unverified departures (settling already-enacted kicks silently), and
        # auto-settle cards leaders never answered.
        departure_cards = raise_departure_verification_cards(now=now_iso, conn=conn)
        counters["departure_cards_raised"] = len(departure_cards)
        if departure_cards:
            log.info(
                "raised %d departure-verification card(s): %s",
                len(departure_cards),
                ", ".join(f"{c['player_name']}" for c in departure_cards),
            )
        expired_departures = expire_departure_verification_cards(now=now_iso, conn=conn)
        counters["departure_cards_expired"] = len(expired_departures)
        if transitions:
            from storage.leader_actions import create_leader_action_recommendation

            for t in transitions:
                create_leader_action_recommendation(
                    action_type="kick_recommendation",
                    target_player_tag=t["player_tag"],
                    target_player_name=t.get("player_name"),
                    objective=t.get("objective")
                    or f"Review kick candidacy for {t.get('player_name') or t['player_tag']}",
                    rationale=t.get("rationale") or json.dumps(t),
                    source_signal_key=f"engine:kick:{t['player_tag']}:{now_iso}",
                    source_signal_type="engine_kick_state",
                    conn=conn,
                )
        # Re-nominate persistently-idle members whose last kick card was declined
        # once the decline cooldown lapses (defer retired 2026-07-10 — the engine
        # reconsiders on sustained evidence, not a leader-set clock). Transition-
        # fire above can't catch these: their kick_state never left 'recommended'.
        renominations = management.renominate_after_cooldown(conn, now=now_iso)
        counters["kick_renominations"] = len(renominations)
        if renominations:
            from storage.leader_actions import create_leader_action_recommendation

            for r in renominations:
                create_leader_action_recommendation(
                    action_type="kick_recommendation",
                    target_player_tag=r["player_tag"],
                    target_player_name=r.get("player_name"),
                    objective=f"Review kick candidacy for {r.get('player_name') or r['player_tag']}",
                    rationale=r.get("rationale") or "Re-nominated after decline cooldown.",
                    source_signal_key=f"engine:kick-renominate:{r['player_tag']}:{now_iso}",
                    source_signal_type="engine_kick_state",
                    conn=conn,
                )

    counters["proactive_posting"] = "awareness_only"
    counters["tick_completed_at"] = utcnow()
    manage_succeeded = "manage_error" not in counters
    errors = {key: value for key, value in counters.items() if key.endswith("_error")}
    if not manage_succeeded:
        # The manage guard rolled back the entire application transaction, so
        # the generation must not claim that its observation writes survived.
        readiness.update_materialization(
            conn,
            materialization_id,
            status="failed",
            completed_at=counters["tick_completed_at"],
            poll_ok=poll_succeeded,
            apply_ok=False,
            manage_ok=False,
            derivations_ok=False,
            counters=counters,
            errors=errors,
        )
        conn.commit()
        return counters

    derivations_ok = not bool(counters.get("materialization_degraded"))
    complete = bool(source_freshness and source_freshness["ready"]) and derivations_ok
    readiness.update_materialization(
        conn,
        materialization_id,
        status="complete" if complete else "partial",
        completed_at=counters["tick_completed_at"],
        poll_ok=poll_succeeded,
        apply_ok=True,
        manage_ok=manage_succeeded,
        derivations_ok=derivations_ok,
        source_freshness=source_freshness,
        counters=counters,
        errors=errors,
    )
    conn.commit()
    return counters
