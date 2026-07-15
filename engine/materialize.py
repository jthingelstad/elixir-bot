"""Canonical application service for admitted Clash Royale observations.

Every operational caller applies an :class:`engine.observations.Observation`
through this module.  The engine remains deliberately hybrid: semantic deltas
land in event streams while current-state projections materialize from the same
admitted snapshot.  Keeping both writes here makes that dual-write explicit,
transactional, and reusable by production polling, interactive refreshes, and
offline replay.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from engine import baselines, ingest, polling, projections
from engine.clock import infer_season_id, period_anchor_from_events, war_clock
from engine.db import canon_tag, chicago_today, ensure_clan, ensure_player
from engine.emitters import emit
from engine.emitters.clan import (
    calendar_already_ran,
    emit_calendar,
    emit_verified_leave_events,
    mark_calendar_ran,
    project_clan_aspects,
)
from engine.emitters.player import project_player_aspects
from engine.emitters.war import project_race_aspect
from engine.normalize import parse_cr_time
from engine.observations import Observation

log = logging.getLogger("engine.materialize")


def configured_home_clan() -> str:
    import prompts

    return canon_tag(prompts.clan_tag())


@dataclass
class ApplyResult:
    endpoint: str
    entity_key: str
    events_emitted: int = 0
    battles_ingested: int = 0
    players_projected: int = 0
    clock: object | None = None
    degraded: list[str] = field(default_factory=list)


def _as_utc(value) -> datetime:
    parsed = parse_cr_time(value)
    if parsed is None:
        raise ValueError(f"invalid observation time: {value!r}")
    return parsed.astimezone(timezone.utc)


def anchored_clock(conn, cr_shaped: dict, now: datetime, season_id):
    """Build the live war clock with the learned per-period drift anchor."""
    prelim = war_clock(cr_shaped, now, season_id=season_id)
    anchor = period_anchor_from_events(
        conn, prelim.season_id, prelim.section_index, prelim.war_day_index
    )
    if anchor is None:
        return prelim
    return war_clock(cr_shaped, now, season_id=season_id, period_anchor=anchor)


def current_clock(conn, now: datetime, *, home_clan: str | None = None):
    """Rebuild the clock from the canonical current-race baseline."""
    home = canon_tag(home_clan or configured_home_clan())
    row = baselines.get_baseline(conn, "riverrace", home, "race")
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    cr_shaped = {
        "periodType": payload.get("period_type"),
        "periodIndex": payload.get("period_index"),
        "sectionIndex": payload.get("section_index"),
        "clan": {
            "tag": payload.get("our_tag"),
            "fame": payload.get("our_fame"),
        },
    }
    return anchored_clock(conn, cr_shaped, now, payload.get("season_id"))


def _ensure_open_membership(
    conn, player_tag: str, clan_tag: str, observed_at: str
) -> None:
    # Membership is temporal. Offline replay applies old observations against a
    # database that may already contain later leave/rejoin tenures, so checking
    # only today's open row can manufacture a second tenure at the historical
    # instant. Reuse any row active at the observation time; otherwise bound a
    # backfilled tenure by the next known join.
    row = conn.execute(
        "SELECT 1 FROM clan_memberships "
        "WHERE player_tag = ? AND clan_tag = ? AND joined_at <= ? "
        "AND (left_at IS NULL OR left_at > ?)",
        (player_tag, clan_tag, observed_at, observed_at),
    ).fetchone()
    if row is None:
        next_join = conn.execute(
            "SELECT MIN(joined_at) FROM clan_memberships "
            "WHERE player_tag = ? AND clan_tag = ? AND joined_at > ?",
            (player_tag, clan_tag, observed_at),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO clan_memberships "
            "(player_tag, clan_tag, joined_at, left_at, join_source, leave_source) "
            "VALUES (?, ?, ?, ?, 'backfill', ?)",
            (
                player_tag,
                clan_tag,
                observed_at,
                next_join,
                "next_known_join" if next_join else None,
            ),
        )


def _apply_clan(conn, observation: Observation, now: datetime) -> ApplyResult:
    payload = observation.payload
    if not isinstance(payload, dict):
        raise TypeError("clan observation payload must be an object")
    clan_tag = canon_tag(observation.entity_key)
    home = configured_home_clan()
    ensure_clan(
        conn,
        clan_tag,
        payload.get("name"),
        observation.observed_at,
        is_home=clan_tag == home,
    )
    roster: dict[str, dict] = {}
    for member in payload.get("memberList") or []:
        tag = canon_tag(member.get("tag"))
        ensure_player(conn, tag, member.get("name"), observation.observed_at)
        _ensure_open_membership(conn, tag, clan_tag, observation.observed_at)
        roster[tag] = projections.roster_state_from_api(member)
    polling.seed_new_members(conn, roster, observation.observed_at)

    emitted = 0
    for aspect, aspect_payload in project_clan_aspects(payload).items():
        emitted += emit(
            conn,
            "clan",
            clan_tag,
            aspect,
            aspect_payload,
            observation.observed_at,
        )
    for tag, roster_state in roster.items():
        projections.refresh_player_state(
            conn, tag, None, roster_state, observation.observed_at
        )
        projections.refresh_management_inputs(conn, tag, now=observation.observed_at)
    projections.refresh_clan_rollups(
        conn,
        payload,
        chicago_today(now),
        observation.observed_at,
    )
    return ApplyResult(
        observation.endpoint,
        observation.entity_key,
        events_emitted=emitted,
        players_projected=len(roster),
    )


def _apply_player(
    conn, observation: Observation, *, track_poll_freshness: bool
) -> ApplyResult:
    payload = observation.payload
    if not isinstance(payload, dict):
        raise TypeError("player observation payload must be an object")
    tag = canon_tag(observation.entity_key)
    ensure_player(conn, tag, payload.get("name"), observation.observed_at)
    emitted = 0
    for aspect, aspect_payload in project_player_aspects(payload).items():
        emitted += emit(
            conn,
            "player",
            tag,
            aspect,
            aspect_payload,
            observation.observed_at,
        )
    projections.refresh_player_state(conn, tag, payload, None, observation.observed_at)
    if payload.get("cards"):
        projections.refresh_card_collection(
            conn, tag, payload.get("cards") or [], observation.observed_at
        )
    projections.refresh_management_inputs(conn, tag, now=observation.observed_at)
    if track_poll_freshness:
        polling.note_poll_succeeded(conn, tag, "profile", observation.observed_at)
    return ApplyResult(
        observation.endpoint,
        observation.entity_key,
        events_emitted=emitted,
        players_projected=1,
    )


def _apply_battlelog(
    conn,
    observation: Observation,
    *,
    clock,
    now: datetime,
    track_poll_freshness: bool,
) -> ApplyResult:
    payload = observation.payload
    if not isinstance(payload, list):
        raise TypeError("battlelog observation payload must be a list")
    tag = canon_tag(observation.entity_key)
    ensure_player(conn, tag, None, observation.observed_at)
    resolved_clock = clock or current_clock(conn, now)
    inserted = ingest.mirror_battles(
        conn,
        tag,
        payload,
        observation.observed_at,
        resolved_clock,
        now=now,
    )
    if inserted:
        polling.update_heat(conn, tag, new_battles=True, now=observation.observed_at)
    projections.refresh_form(conn, tag, now=observation.observed_at)
    projections.refresh_rollups(conn, tag, chicago_today(now))
    projections.refresh_management_inputs(conn, tag, now=observation.observed_at)
    if track_poll_freshness:
        polling.note_poll_succeeded(conn, tag, "battlelog", observation.observed_at)
    return ApplyResult(
        observation.endpoint,
        observation.entity_key,
        battles_ingested=inserted,
        players_projected=1,
        clock=resolved_clock,
    )


def _apply_race(
    conn,
    observation: Observation,
    now: datetime,
    *,
    season_id_override: int | None = None,
) -> ApplyResult:
    payload = observation.payload
    if not isinstance(payload, dict):
        raise TypeError("current-race observation payload must be an object")
    season_id = (
        season_id_override
        if season_id_override is not None
        else infer_season_id(conn, payload)
    )
    clock = anchored_clock(conn, payload, now, season_id)
    emitted = 0
    if clock.season_id is not None:
        emitted = emit(
            conn,
            "riverrace",
            observation.entity_key,
            "race",
            project_race_aspect(payload, clock.season_id),
            observation.observed_at,
        )
    return ApplyResult(
        observation.endpoint,
        observation.entity_key,
        events_emitted=emitted,
        clock=clock,
    )


def apply_observation(
    conn,
    observation: Observation,
    *,
    clock=None,
    now: datetime | None = None,
    track_poll_freshness: bool = False,
    season_id_override: int | None = None,
    materialization_id: int | None = None,
) -> ApplyResult:
    """Apply one admitted observation inside the caller's transaction."""
    now = now or _as_utc(observation.observed_at)
    if materialization_id is not None:
        from engine import readiness

        readiness.add_materialization_input(conn, materialization_id, observation)
    if observation.endpoint == "clan":
        return _apply_clan(conn, observation, now)
    if observation.endpoint == "currentriverrace":
        return _apply_race(
            conn,
            observation,
            now,
            season_id_override=season_id_override,
        )
    if observation.endpoint == "player":
        return _apply_player(
            conn, observation, track_poll_freshness=track_poll_freshness
        )
    if observation.endpoint == "player_battlelog":
        return _apply_battlelog(
            conn,
            observation,
            clock=clock,
            now=now,
            track_poll_freshness=track_poll_freshness,
        )
    raise ValueError(f"unsupported observation endpoint: {observation.endpoint}")


def apply_interactive_observation(
    conn,
    observation: Observation,
    *,
    clock=None,
    now: datetime | None = None,
    track_poll_freshness: bool = True,
) -> ApplyResult:
    """Apply one interactive refresh as a complete, attributable generation."""
    from engine import readiness

    materialization_id = readiness.start_materialization(
        conn,
        started_at=observation.observed_at,
        run_kind="interactive",
    )
    result = apply_observation(
        conn,
        observation,
        clock=clock,
        now=now,
        track_poll_freshness=track_poll_freshness,
        materialization_id=materialization_id,
    )
    readiness.update_materialization(
        conn,
        materialization_id,
        status="complete",
        completed_at=observation.observed_at,
        poll_ok=True,
        apply_ok=True,
        manage_ok=False,
        derivations_ok=True,
        counters={"observations_applied": 1, "source": observation.source},
    )
    return result


def apply_tick_derivations(conn, *, clock, observed_at: str) -> ApplyResult:
    """Apply clock/system derivations that are not owned by one API response."""
    result = ApplyResult("tick", configured_home_clan())
    today = chicago_today(_as_utc(observed_at))
    if not calendar_already_ran(conn, today):
        result.events_emitted += emit_calendar(conn, today)
        mark_calendar_ran(conn, today)
    result.events_emitted += emit_verified_leave_events(
        conn, configured_home_clan(), observed_at
    )

    if clock and clock.season_id is not None:
        try:
            import db as db_facade

            races = db_facade.get_award_races(conn=conn)
            champ = races.get("war_champ_leader")
            rookie = (races.get("rookie_mvp") or [None])[0]
            payload = {
                "season_id": clock.season_id,
                "war_champ_leader": {
                    key: champ.get(key) for key in ("tag", "name", "points")
                }
                if champ
                else None,
                "rookie_mvp_leader": {
                    key: rookie.get(key) for key in ("tag", "name", "points")
                }
                if rookie
                else None,
            }
            result.events_emitted += emit(
                conn,
                "clan",
                configured_home_clan(),
                "award_races",
                payload,
                observed_at,
            )
        except Exception:
            log.warning("award-race derivation degraded", exc_info=True)
            result.degraded.append("award_races")
    try:
        from engine.emitters.game import emit_game_from_sentinel

        result.events_emitted += emit_game_from_sentinel(conn, observed_at)
    except Exception:
        log.warning("game-event derivation degraded", exc_info=True)
        result.degraded.append("game_events")
    return result


__all__ = [
    "ApplyResult",
    "anchored_clock",
    "apply_observation",
    "apply_interactive_observation",
    "apply_tick_derivations",
    "configured_home_clan",
    "current_clock",
]
