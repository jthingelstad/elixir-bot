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
from engine.clock import infer_season_id, period_anchor_from_events, war_clock
from engine.db import canon_tag, chicago_today, ensure_clan, ensure_player, utcnow
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


def _guard(counters: dict, step: str, conn=None):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc is not None:
                log.exception("engine tick step %s failed", step)
                counters[f"{step}_error"] = repr(exc)
                if conn is not None:
                    # Discard the failed step's partial writes. Every step
                    # commits at its own end, so only the broken step's work
                    # rolls back — without this, a later step's commit
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
    prelim = war_clock(cr_shaped, now, season_id=season_id)
    anchor = period_anchor_from_events(
        conn, prelim.season_id, prelim.section_index, prelim.war_day_index
    )
    if anchor is None:
        return prelim
    return war_clock(cr_shaped, now, season_id=season_id, period_anchor=anchor)


def _current_clock(conn, now: datetime):
    """Rebuild the clock from the stored race *projection* (snake_case,
    engine.emitters.war.project_race_aspect) by adapting it back to the
    CR payload keys war_clock reads. Fresh polls this tick recompute the
    clock from the raw payload anyway; this covers startup/gap ticks."""
    row = baselines.get_baseline(conn, "riverrace", HOME_CLAN, "race")
    if row is None:
        return None
    p = json.loads(row["payload_json"])
    cr_shaped = {
        "periodType": p.get("period_type"),
        "periodIndex": p.get("period_index"),
        "sectionIndex": p.get("section_index"),
        "clan": {"tag": p.get("our_tag"), "fame": p.get("our_fame")},
    }
    return _anchored_clock(conn, cr_shaped, now, p.get("season_id"))


def run_tick(conn, now: datetime | None = None, *, api, send_fn=None, compose_fn=None,
             editor_gate=None, deliver: bool = True) -> dict:
    """Run one engine tick.

    ``deliver`` gates the proactive posting path (steps 6–7: RECOGNIZE +
    DELIVER). When False, the engine still polls, projects, runs MANAGE and
    leader-actions, and emits the event stream the awareness brain reads — it
    just stops raising communication intents and composing/sending posts. The
    scheduled runtime always passes deliver=False: the awareness brain is the
    sole proactive poster, so there's no double-posting and no unbounded
    pending-intent backlog (only consume() expires intents)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    counters: dict = {}

    # -- step 0/1: POLL (clan heartbeat + clock-gated riverrace + budgeted players)
    clan_payload = None
    race_payload = None
    player_payloads: dict[str, dict] = {}
    battlelogs: dict[str, list] = {}

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
        clan_payload = api.get_clan()
        if _riverrace_due(conn, clock, now):
            race_payload = api.get_current_war()
        if clan_payload:
            ensure_clan(conn, HOME_CLAN, clan_payload.get("name"), now_iso, is_home=True)
        roster_tags = []
        for m in (clan_payload or {}).get("memberList", []):
            tag = canon_tag(m.get("tag"))
            roster_tags.append(tag)
            # players row must exist before poll_state / projections touch the
            # tag (FK) — live DBs carry migrated rows, but a fresh DB (cold
            # start, simulator) bootstraps right here.
            ensure_player(conn, tag, m.get("name"), now_iso)
            # Self-healing membership backfill: polling.plan only considers
            # players with an OPEN membership, and first sight of a roster
            # deliberately emits nothing — so a member without a row (cold
            # start, or drift) would never be deep-polled at all (simulator
            # finding). A current roster member IS an open membership by
            # definition; the roster-diff emitter still owns the
            # member_joined event and skips its insert when a row exists.
            if conn.execute(
                "SELECT 1 FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
                (tag,),
            ).fetchone() is None:
                conn.execute(
                    "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
                    "VALUES (?, ?, ?, 'backfill')",
                    (tag, HOME_CLAN, now_iso),
                )
        polling.seed_new_members(conn, roster_tags, now_iso)
        plan = polling.plan(conn, now_iso)
        counters["planned_calls"] = len(plan)
        for endpoint, tag in plan:
            # Release the writer BEFORE the fetch: cr_api persists the raw
            # payload on its own connection during the call, so the tick must
            # not hold a write transaction across the HTTP round-trip
            # (that's a 30 s busy-timeout stall per call — observed live).
            ensure_player(conn, tag, None, now_iso)
            conn.commit()
            if endpoint == "battlelog":
                battlelogs[tag] = api.get_player_battle_log(tag) or []
                polling.note_polled(conn, tag, "battlelog", now_iso)
            else:
                player_payloads[tag] = api.get_player(tag) or {}
                polling.note_polled(conn, tag, "profile", now_iso)
            conn.commit()

    # refresh the clock from the fresh race payload before ingest needs it
    if race_payload:
        season_id = infer_season_id(conn, race_payload)
        clock = _anchored_clock(conn, race_payload, now, season_id)

    # -- step 2: INGEST — battle mirror (war keys from the battle's own time)
    with _guard(counters, "ingest", conn):
        new_battles_total = 0
        for tag, battlelog in battlelogs.items():
            n = ingest.mirror_battles(conn, tag, battlelog, now_iso, clock, now=now)
            new_battles_total += n
            if n:
                polling.update_heat(conn, tag, new_battles=True, now=now_iso)
        counters["battles_ingested"] = new_battles_total
        conn.commit()

    # -- step 3: EMIT — diff baselines → events; calendar once per Chicago day
    with _guard(counters, "emit", conn):
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
        # A verified LEAVE (leader confirmed via a departure_verification card)
        # emits member_left_verified — the held goodbye the brain may now narrate.
        # A confirmed KICK emits nothing. Raw member_left is no longer a hard-post.
        from engine.emitters.clan import emit_verified_leave_events
        emitted += emit_verified_leave_events(conn, HOME_CLAN, now_iso)
        # Award-race lead changes (War Champ / Rookie MVP) → clan_events, so the
        # season-long competitions are event-driven. Fail-soft: an awards read
        # must never sink the core emit above. Only while a season is active.
        if clock and clock.season_id is not None:
            try:
                import db as _db
                races = _db.get_award_races(conn=conn)
                champ = races.get("war_champ_leader")
                rookie = (races.get("rookie_mvp") or [None])[0]
                payload = {
                    "season_id": clock.season_id,
                    "war_champ_leader": {k: champ.get(k) for k in ("tag", "name", "points")} if champ else None,
                    "rookie_mvp_leader": {k: rookie.get(k) for k in ("tag", "name", "points")} if rookie else None,
                }
                emitted += emit(conn, "clan", HOME_CLAN, "award_races", payload, now_iso)
            except Exception:
                log.warning("emit award_races failed (non-fatal)", exc_info=True)
        # Game-level stream: new /events + event badges from the sentinel store
        # (new cards emit from the daily catalog sync). Fail-soft — a sentinel
        # read must never sink the player/clan/war emit above; the emit:game
        # cursor + dedup keys make a retry next tick safe.
        try:
            from engine.emitters.game import emit_game_from_sentinel
            emitted += emit_game_from_sentinel(conn, now_iso)
        except Exception:
            log.exception("game emit from sentinel failed; retrying next tick")
        counters["events_emitted"] = emitted
        conn.commit()

    # -- step 4: PROJECT — refresh what the polls touched
    with _guard(counters, "project", conn):
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
                projections.refresh_card_collection(conn, tag, profile.get("cards") or [], now_iso)
            if tag in battlelogs:
                projections.refresh_form(conn, tag, now=now_iso)
                projections.refresh_rollups(conn, tag, today)
            projections.refresh_management_inputs(conn, tag, now=now_iso)
        if clan_payload:
            projections.refresh_clan_rollups(conn, clan_payload, today, now_iso)
        counters["players_projected"] = len(touched)
        conn.commit()

    # -- step 5: MANAGE — kick_state is reactive (Q1); weekly grain rolls in the review
    with _guard(counters, "manage", conn):
        transitions = management.run_tick_evaluators(conn, now=now_iso)
        counters["kick_transitions"] = len(transitions)
        withdrawals = management.withdraw_stale_actions(conn, now=now_iso)
        counters["management_withdrawals"] = sum(int(w.get("count", 1)) for w in withdrawals)
        # Propagate terminal leader-action state onto its backing decision case:
        # a done kick/promotion/demotion resolves its review the moment the leader
        # decides — before the member even leaves the roster. Nothing wired this
        # before, so completed actions left their cases OPEN and they resurfaced
        # as stale recommendations to the awareness brain.
        from storage.cases import (
            reconcile_departed_member_cases,
            expire_departure_verification_cards,
            raise_departure_verification_cards,
            reconcile_uncorroborated_member_cases,
            sync_terminal_leader_action_cases,
        )
        synced_cases = sync_terminal_leader_action_cases(now=now_iso, conn=conn)
        counters["action_cases_synced"] = len(synced_cases)
        if synced_cases:
            log.info(
                "synced %d decision case(s) from terminal leader-actions: %s",
                len(synced_cases),
                ", ".join(f"{c['target_player_name']}({c['case_type']}:{c['outcome']})" for c in synced_cases),
            )
        # Backstop: close any member-review case whose subject has left but whose
        # departure wasn't already resolved above (organic leave, manual clear).
        # A fulfilled kick resolves as kicked; an organic leave dismisses.
        departed_cases = reconcile_departed_member_cases(now=now_iso, conn=conn)
        counters["departed_cases_reconciled"] = len(departed_cases)
        if departed_cases:
            log.info(
                "reconciled %d member-review case(s) for departed members: %s",
                len(departed_cases),
                ", ".join(f"{c['target_player_name']}({c['case_type']}:{c['outcome']})" for c in departed_cases),
            )
        # Backstop 2: dismiss in-clan member-review cases the engine no longer
        # corroborates (state=none past the grace window, no open card) — the
        # brain/signal path can open cases the deterministic engine never backs
        # (Ratko #365 lingered as a "due" nag for a week). Runs after the terminal
        # + departed reconcilers so it only sees genuinely-orphaned open cases.
        uncorroborated = reconcile_uncorroborated_member_cases(now=now_iso, conn=conn)
        counters["uncorroborated_cases_dismissed"] = len(uncorroborated)
        if uncorroborated:
            log.info(
                "dismissed %d uncorroborated member-review case(s): %s",
                len(uncorroborated),
                ", ".join(f"{c['target_player_name']}({c['case_type']})" for c in uncorroborated),
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
        conn.commit()

    # -- steps 6/7: RECOGNIZE + DELIVER — proactive posting. Skipped entirely
    # when the awareness brain owns posting (deliver=False): no intents raised,
    # nothing composed or sent. Everything above (poll, project, MANAGE,
    # leader-actions, event emit) still runs, so the brain's read is intact.
    if deliver:
        # -- step 6: RECOGNIZE — score → coalesce → claim → raise intents
        with _guard(counters, "recognize", conn):
            clock_dict = asdict(clock) if clock is not None else None
            rec = recognition.run_recognizers(conn, clock_dict, now_iso)
            counters.update({f"recognize_{k}": v for k, v in rec.items()})
            conn.commit()

        # -- step 7: DELIVER — at-least-once
        with _guard(counters, "deliver", conn):
            d = delivery.consume(conn, send_fn, compose_fn, now_iso,
                                 editor_gate=editor_gate)
            counters.update({f"deliver_{k}": v for k, v in d.items()})
            conn.commit()
    else:
        counters["proactive_posting"] = "disabled"

    counters["tick_completed_at"] = utcnow()
    return counters
