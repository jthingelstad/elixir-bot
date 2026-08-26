"""The scoped responder — one wake, one author, minutes not hours.

Agentic Loop v2, Phase 1 (docs/plans/agentic-loop.md). A wake fires; this builds
a small, precise context for exactly what happened, runs one chassis turn, and
hands the staged posts to the SAME delivery path the awareness brain uses.

Why this is not a second author, which is the failure that killed v4: a wake's
posts are composed in ONE turn from ONE set of facts, including the in-game
clan-chat sibling, and they are delivered through
``runtime.awareness.deliver.deliver_posts`` — the single path that owns the
hard-post floor, the durable outbox, idempotency, and copy policy. Nothing here
sends a message.

The floor is the safety property worth stating plainly. A wake carries the
signal keys it MUST cover. After the turn, coverage is checked against what
actually reached the outbox, never against what the model said it did. An
uncovered floor fails the wake, the cursors do not advance, and the daily
deliberation inherits the signals — the same guarantee the brain has always had,
enforced at a new call site.

The escalation ladder means a wake can only ever fail *upward*: the cheap model
first, the strong model if the cheap one produced nothing usable, and the daily
brain if both fell short. Silence is successful only when the job permits it and
the model calls the explicit silence tool.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from agent import chassis
from engine.event_contracts import hard_post_event_types

log = logging.getLogger("elixir")

HARD_POST_EVENT_TYPES = hard_post_event_types()


@dataclass(frozen=True)
class JobSpec:
    """One job: the events it claims, and the surfaces it may speak on.

    This table IS the per-event-type behaviour. Design rule 2 of the plan says
    the day ``respond.py`` grows an ``if event_type ==`` branch we are rebuilding
    v4's ``delivery.py`` — so everything that differs between a welcome and a
    farewell lives here as data, or in the job's prose file, and nowhere else.

    ``surfaces`` is what the job MAY use, not what it must: the farewell carries
    clan chat because a notable departure deserves it, and ``farewell.md``
    decides which departures qualify. ``required_when_available`` is the narrow
    exception for a surface whose eligibility already means the editorial bar
    cleared. A milestone batch is subtler: several co-arriving events make clan
    chat available for a genuine roundup, while only the named mandatory event
    types make it compulsory. Availability is data; the editorial call is prose.
    """

    name: str
    event_types: frozenset
    surfaces: frozenset
    silence_allowed: bool = False
    required_surface: str | None = None
    mandatory_clan_chat_event_types: frozenset | None = None
    required_when_available: frozenset = frozenset()


_ANNOUNCE = "discord:announcements"
_ELIXIR = "discord:elixir"
_CLAN_CHAT = "clan_chat"

# Surfaces below are the ones the brain actually used, measured over 31 days of
# delivered intents rather than chosen: joins 12/12 to announcements with a
# clan-chat sibling every time; role changes 7/7 announcements and 0/7 clan chat;
# the one podium to announcements; every arena/badge/ranked milestone to #elixir
# (28/28). Milestones never earned their own clan-chat line — the siblings that
# looked like theirs belonged to a join co-covered by the same post.
JOBS = (
    JobSpec(
        "welcome",
        frozenset({"member_joined"}),
        frozenset({_ANNOUNCE, _CLAN_CHAT}),
        required_surface=_ANNOUNCE,
    ),
    JobSpec(
        "farewell",
        frozenset({"member_left_verified"}),
        frozenset({_ANNOUNCE, _CLAN_CHAT}),
        required_surface=_ANNOUNCE,
    ),
    JobSpec(
        "role_change",
        frozenset({"role_changed"}),
        frozenset({_ANNOUNCE}),
        required_surface=_ANNOUNCE,
    ),
    JobSpec(
        "podium",
        frozenset({"pol_season_podium"}),
        frozenset({_ANNOUNCE}),
        required_surface=_ANNOUNCE,
    ),
    JobSpec(
        "milestone_batch",
        frozenset(
            {
                "arena_changed",
                "legendary_badge_earned",
                "champion_league_reached",
                "ultimate_champion_reached",
            }
        ),
        frozenset({_ELIXIR, _CLAN_CHAT}),
        required_surface=_ELIXIR,
        # A lone arena climb or Legendary badge belongs on Discord only. The
        # measured clan-chat bar is a Champion-tier arrival or a real batch;
        # keeping this as registry data preserves one generic responder path.
        mandatory_clan_chat_event_types=frozenset(
            {"champion_league_reached", "ultimate_champion_reached"}
        ),
        # Champion-tier arrivals deterministically clear the ratified high bar.
        # A multi-event wake only makes clan chat available so the composer can
        # choose it for a genuine roundup; co-arrival alone does not require it.
        required_when_available=frozenset({_CLAN_CHAT}),
    ),
    # Phase 3. ONE job for the whole war boundary, and the plan asked for two
    # (`war_week.md` + `war_season.md`). Two would have been a bug: at a season
    # close, `week_finished`, `season_closed` and `clan_league_changed` all emit
    # at the SAME instant (measured 2026-08-03T11:17:22Z), and wakes group by
    # (class, model, job) — so two jobs means two groups, two wakes, and two
    # posts narrating one moment. That is the divergence this architecture
    # exists to prevent, and the plan's own text demands the season close "land
    # as ONE post". One job makes the batching structural instead of hoped-for.
    # A plain week close carries only `week_finished` and reads the same way.
    JobSpec(
        "war_close",
        frozenset({"week_finished", "season_closed", "clan_league_changed"}),
        frozenset({_ELIXIR, _CLAN_CHAT}),
        required_surface=_ELIXIR,
    ),
    # Its own job, deliberately: a tournament result shares (immediate, chat)
    # with the war boundary but is a different story, and the grouping key is
    # what keeps them from colliding into one confused post.
    JobSpec(
        "tournament",
        frozenset({"tournament_finished"}),
        frozenset({_ELIXIR}),
        required_surface=_ELIXIR,
    ),
    # Annual, and the last hard post that had no job. Without one it would fall
    # to the brain — which this phase cuts to once a day, so the clan's own
    # birthday could arrive up to 24h late. Registering it is what makes the
    # cadence cut safe.
    JobSpec(
        "clan_birthday",
        frozenset({"clan_birthday"}),
        frozenset({_ANNOUNCE, _CLAN_CHAT}),
        required_surface=_ANNOUNCE,
    ),
    # Phase 5. Both surfaces available, but `followup.md` sends almost every one
    # to clan chat: a check-in is about one person and belongs where they will
    # see it. It is also the one job whose correct answer is often no post at
    # all, which is why it carries no floor.
    JobSpec(
        "followup",
        frozenset({"followup_due"}),
        frozenset({_ELIXIR, _CLAN_CHAT}),
        silence_allowed=True,
    ),
)

JOBS_BY_NAME = {spec.name: spec for spec in JOBS}

# Derived, never hand-maintained: a second list of the same facts is how the two
# drift apart. Many event types mapping to one job is the whole mechanism behind
# the mixed milestone wake — `job_for` collapses them to a single job name.
JOB_BY_EVENT_TYPE = {event_type: spec.name for spec in JOBS for event_type in spec.event_types}

# Discord surface -> lane, matching agent.chassis._DISCORD_SURFACES. Lanes drive
# the recent-posts context (repetition avoidance); surfaces drive which posting
# tools and lane prompts the turn gets.
_LANE_BY_SURFACE = {_ANNOUNCE: "announcements", _ELIXIR: "elixir"}

# The escalation ladder. Same attention, stronger composer.
_LADDER = {"lightweight": "wake_response", "chat": "wake_response_chat"}


def job_spec(job: str) -> JobSpec | None:
    return JOBS_BY_NAME.get(job)


def lanes_for(spec: JobSpec) -> tuple:
    return tuple(lane for surface, lane in _LANE_BY_SURFACE.items() if surface in spec.surfaces)


def surfaces_for(spec: JobSpec, events: list[dict]) -> frozenset:
    """Resolve this wake's usable surfaces from the job's data contract."""
    surfaces = set(spec.surfaces)
    mandatory_types = spec.mandatory_clan_chat_event_types
    if (
        _CLAN_CHAT in surfaces
        and mandatory_types is not None
        and len(events) == 1
        and events[0].get("event_type") not in mandatory_types
    ):
        surfaces.remove(_CLAN_CHAT)
    return frozenset(surfaces)


def _has_surface(posts: list[dict], surface: str) -> bool:
    if surface == _CLAN_CHAT:
        return any(post.get("channel") == _CLAN_CHAT or post.get("clan_chat") for post in posts)
    lane = _LANE_BY_SURFACE.get(surface)
    return bool(lane) and any(post.get("channel") == lane for post in posts)


def _missing_required_surfaces(
    spec: JobSpec,
    posts: list[dict],
    available_surfaces: frozenset,
    events: list[dict],
) -> tuple[str, ...]:
    required = set(spec.required_when_available & available_surfaces)
    mandatory_types = spec.mandatory_clan_chat_event_types
    if (
        _CLAN_CHAT in required
        and mandatory_types is not None
        and not any(event.get("event_type") in mandatory_types for event in events)
    ):
        # A mixed soft batch may earn one clan-chat line, but the model must make
        # that editorial call. Input multiplicity alone is not evidence that the
        # whole clan should see it; natural R320 proved that forcing the surface
        # turns a selected lone arena climb into a low-value relay.
        required.remove(_CLAN_CHAT)
    if spec.required_surface is not None:
        required.add(spec.required_surface)
    return tuple(sorted(surface for surface in required if not _has_surface(posts, surface)))


def responder_enabled() -> bool:
    """Phase 1 ships OFF. The wake evaluator keeps shadowing either way."""
    return os.getenv("ELIXIR_WAKE_RESPONDER", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def job_for(events: list[dict]) -> str | None:
    """The job that covers this wake, or None if no job claims it.

    A wake whose events map to more than one job is NOT split into two turns —
    it falls through to the daily brain. Two composers for one moment is exactly
    the divergence this design exists to avoid, and a mixed batch is rare enough
    that paying a brain tick for it is the right trade.
    """
    if not events:
        return None
    jobs = {JOB_BY_EVENT_TYPE.get(e.get("event_type")) for e in events}
    # An UNMAPPED event in the batch is disqualifying, not ignorable. Dropping
    # the None here would hand the wake to the one job that matched while the
    # unmapped event rode along uncovered — and if it were a hard post, the
    # floor would fail the whole wake after paying for the turn.
    if len(jobs) != 1 or None in jobs:
        return None
    return jobs.pop()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_seed(events: list[dict], conn) -> dict:
    """The scoped read: this wake's events, resolved just enough to compose.

    Deliberately small. The point of the whole architecture is that a welcome
    does not need the clan's full situation — it needs THIS member. Everything
    else the turn wants, it fetches with a tool.

    What IS precomputed here is what the 2026-08-04 experiment showed models get
    wrong when left to infer it: whether a joiner is actually returning, and the
    live join floor (a remembered value once told a member who joined at 7,053
    that they were clear of a "2,000-trophy entry line" when the floor was
    7,000).
    """
    import prompts

    seed_events: list[dict] = []
    for event in events:
        entry = {
            "signal_key": event.get("signal_key"),
            "event_type": event.get("event_type"),
            "observed_at": event.get("observed_at"),
            "subject_tag": event.get("subject_tag"),
        }
        payload = event.get("payload")
        if isinstance(payload, dict):
            entry["payload"] = payload
        tag = event.get("subject_tag")
        if tag:
            entry["member"] = _member_facts(conn, tag)
        # War-boundary events have no subject_tag — the subject is the clan. The
        # facts they need are resolved the same way and for the same reason.
        if event.get("event_type") in _WAR_FACT_EVENT_TYPES:
            war = _war_facts(conn, event)
            if war:
                entry["war"] = war
        seed_events.append(entry)

    return {
        "wake": "hard_post" if any(_is_floor(e) for e in events) else "signal",
        "now": _utcnow(),
        "events": seed_events,
        "clan": {
            "name": "POAP KINGS",
            # The live floor, never a remembered one. CLAN.md substitutes the
            # current value; quoting a stale number told a member who joined at
            # 7,053 they were "well clear of our 2,000-trophy entry line".
            "required_trophies": prompts._live_required_trophies(),
        },
    }


def _is_floor(event: dict) -> bool:
    return (event.get("event_type") or "") in HARD_POST_EVENT_TYPES


def _member_facts(conn, tag: str) -> dict:
    """Identity plus the one inference a welcome must not get wrong."""
    facts: dict = {"player_tag": tag}
    row = conn.execute(
        "SELECT COALESCE(display_name, current_name) AS name, first_seen_at "
        "FROM players WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    if row:
        facts["name"] = row["name"]
        facts["first_seen_at"] = row["first_seen_at"]
    stints = conn.execute(
        "SELECT joined_at, left_at FROM clan_memberships WHERE player_tag = ? ORDER BY joined_at",
        (tag,),
    ).fetchall()
    facts["clan_stints"] = [dict(r) for r in stints]
    # The distinction a welcome turns on. Left to infer from a stint list, a
    # model can and does read a returning member as brand new.
    facts["is_returning"] = len([s for s in stints if s["left_at"]]) > 0
    return facts


# Read off the job registry rather than restated, so a war type added to
# `war_close` cannot silently miss its resolved facts.
_WAR_FACT_EVENT_TYPES = JOBS_BY_NAME["war_close"].event_types

_LEAGUE_ORDER = ("Bronze", "Silver", "Gold", "Legendary")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


def _league_rank(name: str | None) -> tuple[int, int] | None:
    """Order a war league name so direction can be stated, not guessed.

    Clash Royale numbers war-league tiers ASCENDING inside a band — Silver II is
    above Silver I — which is the opposite of the ranked ladder's convention and
    is exactly the "league-direction semantics" the plan flags as a trap. A model
    asked to infer it from two names gets it right about half the time, and
    "POAP KINGS drops to Silver II" after a promotion is the worst sentence this
    job could write.
    """
    if not name:
        return None
    parts = str(name).split()
    band = next((i for i, b in enumerate(_LEAGUE_ORDER) if parts and parts[0] == b), None)
    if band is None:
        return None
    tier = next((_ROMAN[p] for p in parts if p in _ROMAN), 0)
    return (band, tier)


def _war_facts(conn, event: dict) -> dict:
    """Resolve what a war-boundary post must state and must not derive.

    Three things, all measured as traps rather than imagined:

    - **Clan names.** ``week_finished`` standings carry clan TAGS only. A model
      that cannot resolve ``#RJQQLLV9`` either prints the tag or invents a name.
      Resolved here for the same reason ``_member_facts`` resolves a player: a
      join is cheap and an invention is not.
    - **The human week label.** ``section_index`` is 0-based and the clan says
      "Week 1". Off-by-one in a headline is the kind of error nobody forgives.
    - **League direction.** See ``_league_rank``.
    """
    facts: dict = {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    key = str(event.get("signal_key") or "")

    # dedup_key shape is `<type>:<season>:<section>` — the season and week live
    # on the row, not in the payload, so this is where they re-enter.
    bits = key.split(":")
    if len(bits) >= 3 and bits[1].isdigit() and bits[2].isdigit():
        facts["season_id"] = int(bits[1])
        facts["week_label"] = f"Week {int(bits[2]) + 1}"
    elif len(bits) >= 2 and bits[1].isdigit():
        facts["season_id"] = int(bits[1])

    standings = payload.get("standings")
    if isinstance(standings, list) and standings:
        names = {}
        tags = [s.get("clan_tag") for s in standings if isinstance(s, dict) and s.get("clan_tag")]
        if tags:
            rows = conn.execute(
                f"SELECT clan_tag, name FROM clans WHERE clan_tag IN "
                f"({','.join('?' for _ in tags)})",
                tuple(tags),
            ).fetchall()
            names = {r["clan_tag"]: r["name"] for r in rows}
        facts["standings"] = [
            {**s, "clan_name": names.get(s.get("clan_tag"))}
            for s in standings
            if isinstance(s, dict)
        ]

    if event.get("event_type") == "clan_league_changed":
        before, after = (
            _league_rank(payload.get("prev_league")),
            _league_rank(payload.get("league")),
        )
        if before and after and before != after:
            facts["direction"] = "promoted" if after > before else "demoted"
        elif before and after:
            facts["direction"] = "unchanged"
    return facts


def floor_keys(events: list[dict]) -> frozenset:
    return frozenset(
        str(e.get("signal_key")) for e in events if _is_floor(e) and e.get("signal_key")
    )


def respond(
    wake: dict,
    *,
    deliver_fn,
    conn=None,
    on_event=None,
) -> dict:
    """Compose and deliver one wake. Returns an outcome dict.

    ``deliver_fn(read, plan) -> dict`` is the caller's binding of
    ``deliver_posts`` — the same callable the awareness loop uses, so the
    responder cannot invent a delivery path of its own.
    """
    import db

    events = wake.get("events") or []
    job = job_for(events)
    if job is None:
        return {
            "consumed": False,
            "delivered": 0,
            "intentionally_silent": False,
            "handled": False,
            "reason": "no job claims this wake",
        }

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        seed = build_seed(events, conn)
    finally:
        if owns_conn:
            conn.close()

    floor = floor_keys(events)
    spec = job_spec(job)
    if spec is None:
        # job_for() only returns names from this table, so this is unreachable
        # unless the two fall out of step. Fail to the brain rather than guess a
        # surface — a wrong lane is a member-facing error.
        log.error("wake responder: job %r has no spec; leaving for the daily brain", job)
        return {
            "consumed": False,
            "delivered": 0,
            "intentionally_silent": False,
            "handled": False,
            "reason": f"job {job!r} has no surface declaration",
        }
    lanes = lanes_for(spec)
    surfaces = surfaces_for(spec, events)

    attempts = []
    tiers = ["lightweight"]
    if wake.get("wake_model") == "chat":
        tiers = ["chat"]
    elif os.getenv("ELIXIR_WAKE_ESCALATE", "1").strip().lower() not in ("0", "false", "no"):
        tiers.append("chat")

    for tier in tiers:
        attention = chassis.Attention(
            job=job,
            trigger={
                "wake_class": wake.get("wake_class"),
                "reason": wake.get("reason"),
                "signal_keys": [e.get("signal_key") for e in events],
            },
            scope=chassis.Scope(
                member_tags=tuple(e["subject_tag"] for e in events if e.get("subject_tag")),
                lanes=lanes,
                signal_keys=tuple(str(e.get("signal_key")) for e in events if e.get("signal_key")),
            ),
            surfaces=frozenset(surfaces),
            floor=floor,
            silence_allowed=spec.silence_allowed,
            workflow=_LADDER[tier],
        )
        episode = chassis.run_turn(attention, seed, on_event=on_event)
        attempts.append(episode)

        # Measured over the Phase 2 gate: hard-post wakes that ended without a
        # tool call need one same-tier retry before paying for the stronger model.
        # A soft wake is different: it may correctly conclude that the moment is
        # not worth a member-facing post. Nudging it to call a posting tool turns
        # that internal verdict into literal output (for example, "No post — ...")
        # instead of leaving the signal for the daily deliberation.
        #
        # Only on the clean signature: a turn that produced rejections was trying
        # and failing, and a nudge would just spend another round on it.
        if floor and not (
            episode.get("posts")
            or episode.get("intentionally_silent")
            or episode.get("rejections")
            or episode.get("error")
        ):
            log.info("wake responder: %s tier ended without posting; nudging once", tier)
            episode = chassis.run_turn(
                attention,
                seed,
                on_event=on_event,
                nudge=(
                    "Your previous turn ended without posting. You have already read "
                    "what you need. Call a posting tool now with the finished post — "
                    "prose in your reply reaches nobody. If this job permits silence "
                    "and no post is appropriate, call choose_silence with the reason."
                ),
            )
            attempts.append(episode)

        posts = episode.get("posts") or []
        covered = {k for post in posts for k in post.get("covers_signal_keys") or []}
        uncovered = sorted(floor - covered)
        if episode.get("intentionally_silent"):
            if posts or floor or not spec.silence_allowed:
                log.warning("wake responder: invalid silence outcome for job=%s", job)
                continue
            if len(attempts) > 1:
                episode = {**episode, "preceding_attempts": attempts[:-1]}
            return {
                "consumed": True,
                "delivered": 0,
                "intentionally_silent": True,
                "handled": True,
                "reason": episode.get("silence_reason") or "explicit successful silence",
                "attempts": len(attempts),
                "tier": tier,
                "episode": episode,
            }
        if not posts:
            log.warning(
                "wake responder: %s tier produced no post (job=%s, rejections=%s)",
                tier,
                job,
                episode.get("rejections"),
            )
            continue
        missing_surfaces = _missing_required_surfaces(spec, posts, surfaces, events)
        if missing_surfaces:
            missing = ", ".join(missing_surfaces)
            rejection = f"missing required surface {missing}"
            episode = {
                **episode,
                "rejections": [*(episode.get("rejections") or []), rejection],
            }
            attempts[-1] = episode
            log.warning(
                "wake responder: %s tier missed required surface %s (job=%s)",
                tier,
                missing,
                job,
            )
            continue
        if uncovered:
            log.warning("wake responder: %s tier left floor signals uncovered: %s", tier, uncovered)
            continue

        result = _deliver(episode, seed, floor, deliver_fn)
        # Carry the rungs that failed on the way here. Only the winning tier's
        # episode was stored before, so an escalation left NO durable trace of
        # why the cheap tier lost — over the Phase 2 gate, 10 of 41 wakes
        # escalated and every one of them could only be diagnosed from a log
        # line. Same class of blindness as the floor miss that a fully-failed
        # wake used to leave, one level down.
        if len(attempts) > 1:
            episode = {**episode, "preceding_attempts": attempts[:-1]}
        result["episode"] = episode
        result["attempts"] = len(attempts)
        result["tier"] = tier
        if not result.get("failed"):
            return {
                "consumed": True,
                "intentionally_silent": False,
                "handled": True,
                **result,
            }
        log.warning("wake responder: delivery failed on %s tier: %s", tier, result.get("reason"))

    # A wake that failed every tier is the evidence the exit gate asks for, so it
    # has to leave a durable trace. Before this, a fully-failed wake wrote only a
    # log line: `episode` was set on the successful tier alone, and the caller
    # records nothing when it is absent. A floor miss was therefore invisible to
    # exactly the query meant to find it.
    return {
        "consumed": False,
        "delivered": 0,
        "intentionally_silent": False,
        "handled": False,
        "reason": "no tier produced a deliverable post; leaving for the daily deliberation",
        "attempts": len(attempts),
        "episodes": attempts,
        "episode": {
            "job": job,
            "workflow": _LADDER[tiers[-1]] if tiers else None,
            "failed_tiers": tiers,
            "floor": sorted(floor),
            "attempts": attempts,
        },
        # Names the obligations that went unmet, so the nightly check can count
        # floor misses without re-deriving which signals were mandatory.
        "uncovered_floor": sorted(floor),
    }


def _deliver(episode: dict, seed: dict, floor: frozenset, deliver_fn) -> dict:
    """Hand the staged posts to the one delivery path.

    The ``read`` handed to ``deliver_posts`` carries this wake's floor as
    ``hard_post_signals`` — that is what makes the floor check inside
    ``deliver_posts`` verify THIS wake's obligations rather than a whole tick's.
    """
    read = {
        "hard_post_signals": [
            {
                "signal_key": event.get("signal_key"),
                "event_type": event.get("event_type"),
                "subject_tag": event.get("subject_tag"),
            }
            for event in seed.get("events") or []
            if event.get("signal_key") in floor
        ],
        "signals_by_category": {},
        "recent_member_spotlights": [],
    }
    plan = {"posts": [dict(post) for post in episode.get("posts") or []]}
    try:
        return deliver_fn(read, plan)
    except Exception as exc:
        log.exception("wake responder: delivery raised")
        return {"delivered": 0, "failed": True, "reason": f"delivery raised: {exc}"}


def record_episode(episode: dict, outcome: dict) -> None:
    """Persist what this wake thought and did — write-only, admin-facing.

    Lives in the telemetry database because an episode is observation about the
    agent, not a fact about the clan. **Nothing in Elixir's behaviour may ever
    read it back.** The telemetry file is operational history for humans: if it
    were deleted, we lose the ability to explain what happened, never the
    ability to do the right thing next time.

    One reader exists and it is a report — `runtime/awareness/divergence.py`
    counts floor misses for the daily #leaders message, and degrades to
    "unavailable" when the file is missing.

    An earlier version of this docstring called episodes "the substrate the
    nightly reflection reads". That was wrong and worth correcting rather than
    deleting: Phase 4's reflection reads 24h of **delivery intents** from the
    clan database, plus reactions and lessons that also live there. If a future
    phase ever wants an episode to change what Elixir does, the episode has to
    move to the clan DB first — that is the line.
    """
    try:
        from storage import telemetry

        # The table is declared in storage/telemetry._SCHEMA, not here: schema
        # lives in one place per database, and an inline CREATE is how two
        # definitions of the same table start to drift.
        conn = telemetry.connect()
        conn.execute(
            "INSERT INTO wake_episodes (recorded_at, job, workflow, tier, handled, "
            "delivered, reason, episode_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow(),
                episode.get("job"),
                episode.get("workflow"),
                outcome.get("tier"),
                1 if outcome.get("consumed") else 0,
                int(outcome.get("delivered") or 0),
                str(outcome.get("reason") or "")[:400],
                json.dumps(episode, default=str)[:200000],
            ),
        )
        conn.commit()
    except Exception:
        log.debug("wake responder: episode record failed", exc_info=True)


__all__ = [
    "JOB_BY_EVENT_TYPE",
    "build_seed",
    "floor_keys",
    "job_for",
    "record_episode",
    "respond",
    "responder_enabled",
    "surfaces_for",
]
