"""Tournament watch."""

__all__ = [
    "maybe_autowatch_tournament",
    "TOURNAMENT_POLL_MINUTES",
    "TOURNAMENT_BATTLE_LOG_SPACING_SECONDS",
    "_TOURNAMENT_JOB_ID",
    "_tournament_watch_tick",
    "_tournament_recap",
    "start_tournament_watch",
    "stop_tournament_watch",
]

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.jobstores.base import JobLookupError

import cr_api
import db
import elixir_agent
from engine import observations
from runtime import status as runtime_status
from runtime.discord_posting import compose_and_post
from runtime.helpers import (
    _channel_msg_kwargs,
    _channel_scope,
    _get_singleton_channel_id,
)
from runtime.helpers._common import _post_to_elixir

TOURNAMENT_POLL_MINUTES = int(os.getenv("TOURNAMENT_POLL_MINUTES", "5"))
# Auto-detect: how many DISTINCT current members must appear in the same
# tournament before Elixir starts watching it unasked. 1 would fire on a member
# joining any random public tournament; 2+ is the clan doing something together.
TOURNAMENT_AUTOWATCH_MIN_MEMBERS = int(os.getenv("TOURNAMENT_AUTOWATCH_MIN_MEMBERS", "2"))
# How far back to look for tournament battles. The battle-log poll lags real
# play by roughly 20 minutes, so this must comfortably exceed that or a
# tournament is detected only after it ends.
TOURNAMENT_AUTOWATCH_LOOKBACK_MINUTES = int(
    os.getenv("TOURNAMENT_AUTOWATCH_LOOKBACK_MINUTES", "90")
)
TOURNAMENT_BATTLE_LOG_SPACING_SECONDS = 0.5
TOURNAMENT_RECAP_DELAY_SECONDS = int(os.getenv("TOURNAMENT_RECAP_DELAY_SECONDS", "120"))
_TOURNAMENT_JOB_ID = "tournament-watch"
log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


def _build_battle_played_signal(
    tournament_tag: str,
    tournament_name: str,
    battle_info: dict,
    *,
    tournament_timing: Optional[dict] = None,
) -> dict:
    """Shape a tournament_battle_played signal payload for awareness delivery.

    The audience framing (supportive clan commentary vs. neutral observation)
    is encoded as an explicit payload flag so the prompt can pick tone
    without re-deriving it from member metadata. Dedup is already guaranteed
    by store_tournament_battle's INSERT OR IGNORE on the canonicalized
    (p1_tag, p2_tag, battle_time) triple, so the signal fires exactly once
    per match even though we see it in both players' battle logs.

    ``tournament_timing`` carries the tournament's own clock (started_time,
    duration_minutes, ends_time) so the awareness prompt does not reach for
    war/river-race state to time the match.
    """
    p1_is_member = battle_info.get("player1_is_clan_member")
    p2_is_member = battle_info.get("player2_is_clan_member")
    both_members = bool(p1_is_member and p2_is_member)
    one_member = bool(p1_is_member) ^ bool(p2_is_member)
    if both_members:
        audience = "clan_internal"
    elif one_member:
        audience = "clan_one_side"
    else:
        audience = "external_observed"

    winner_tag = battle_info.get("winner_tag")
    p1_tag = battle_info.get("player1_tag")
    p2_tag = battle_info.get("player2_tag")
    p1_crowns = battle_info.get("player1_crowns")
    p2_crowns = battle_info.get("player2_crowns")
    if winner_tag and winner_tag == p1_tag:
        winner_name = battle_info.get("player1_name")
        loser_name = battle_info.get("player2_name")
        winner_crowns, loser_crowns = p1_crowns, p2_crowns
    elif winner_tag and winner_tag == p2_tag:
        winner_name = battle_info.get("player2_name")
        loser_name = battle_info.get("player1_name")
        winner_crowns, loser_crowns = p2_crowns, p1_crowns
    else:
        winner_name = None
        loser_name = None
        winner_crowns = None
        loser_crowns = None

    # Crown-shape facts. Surfaced explicitly so the LLM doesn't have to infer
    # "3-crown" / "shutout" / "close game" from the raw crowns each time.
    if isinstance(p1_crowns, int) and isinstance(p2_crowns, int):
        crown_differential = abs(p1_crowns - p2_crowns)
        is_draw = p1_crowns == p2_crowns
        is_three_crown = (winner_crowns == 3) if winner_crowns is not None else False
        is_shutout = (loser_crowns == 0) if loser_crowns is not None else False
        is_close = crown_differential == 1
        # match_shape: "blowout" 3-0 | "three_crown" 3-1/3-2 | "decisive" 2-0 |
        # "close" 1-crown margin | "draw" tied
        if is_draw:
            match_shape = "draw"
        elif is_three_crown and is_shutout:
            match_shape = "blowout"
        elif is_three_crown:
            match_shape = "three_crown"
        elif is_shutout:
            match_shape = "decisive"
        elif is_close:
            match_shape = "close"
        else:
            match_shape = "standard"
    else:
        crown_differential = None
        is_draw = False
        is_three_crown = False
        is_shutout = False
        is_close = False
        match_shape = "unknown"

    return {
        "type": "tournament_battle_played",
        "signal_key": (
            f"tournament_battle_played|{tournament_tag}"
            f"|{battle_info.get('battle_time')}|{p1_tag}|{p2_tag}"
        ),
        "tournament_tag": tournament_tag,
        "tournament_name": tournament_name,
        "battle_time": battle_info.get("battle_time"),
        "audience": audience,
        "player1": {
            "tag": p1_tag,
            "name": battle_info.get("player1_name"),
            "is_clan_member": p1_is_member,
            "crowns": battle_info.get("player1_crowns"),
            "deck": battle_info.get("player1_deck") or [],
            "deck_avg_elixir": battle_info.get("player1_deck_avg_elixir"),
            **(battle_info.get("player1_context") or {}),
        },
        "player2": {
            "tag": p2_tag,
            "name": battle_info.get("player2_name"),
            "is_clan_member": p2_is_member,
            "crowns": battle_info.get("player2_crowns"),
            "deck": battle_info.get("player2_deck") or [],
            "deck_avg_elixir": battle_info.get("player2_deck_avg_elixir"),
            **(battle_info.get("player2_context") or {}),
        },
        "shared_cards": battle_info.get("shared_cards") or [],
        "winner_tag": winner_tag,
        "winner_name": winner_name,
        "loser_name": loser_name,
        "winner_crowns": winner_crowns,
        "loser_crowns": loser_crowns,
        "crown_differential": crown_differential,
        "is_three_crown": is_three_crown,
        "is_shutout": is_shutout,
        "is_close": is_close,
        "is_draw": is_draw,
        "match_shape": match_shape,
        "deck_selection": battle_info.get("deck_selection"),
        "game_mode_name": battle_info.get("game_mode_name"),
        "arena_name": battle_info.get("arena_name"),
        "tournament_timing": tournament_timing or {},
    }


def find_unwatched_clan_tournament(conn, *, now: datetime | None = None) -> dict | None:
    """A tournament several clan members are playing that nobody told us about.

    Elixir already ingests these: a tournament battle arrives in the battle log
    with `type: "tournament"` and a `tournamentTag`, and `battle_events` has
    carried a `tournament_tag` column all along. On 2026-07-24 five current
    members -- Shafith Nihal, Vijay, canavar, AHMO, MONICA -- played
    #2JVLYQR9 across 100 minutes, Elixir observed the first battle within ~20
    minutes, and nothing happened, because watching required a human to type
    `/tournament watch <tag>`.

    Returns the tag + participants, or None. Membership is checked live so a
    tournament full of ex-members does not trigger a watch.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=TOURNAMENT_AUTOWATCH_LOOKBACK_MINUTES)).strftime(
        "%Y%m%dT%H%M%S"
    )
    rows = conn.execute(
        """SELECT be.tournament_tag AS tag,
                  COUNT(DISTINCT be.player_tag) AS members,
                  MAX(be.battle_time) AS latest
             FROM battle_events be
            WHERE be.tournament_tag IS NOT NULL
              AND be.battle_time >= ?
              AND EXISTS (SELECT 1 FROM clan_memberships cm
                           WHERE cm.player_tag = be.player_tag AND cm.left_at IS NULL)
              AND NOT EXISTS (SELECT 1 FROM tournaments t
                               WHERE t.tournament_tag = be.tournament_tag)
            GROUP BY be.tournament_tag
           HAVING members >= ?
            ORDER BY members DESC, latest DESC
            LIMIT 1""",
        (cutoff, TOURNAMENT_AUTOWATCH_MIN_MEMBERS),
    ).fetchone()
    if row := rows:
        names = [
            r[0]
            for r in conn.execute(
                """SELECT DISTINCT COALESCE(p.display_name, p.current_name)
                     FROM battle_events be LEFT JOIN players p ON p.player_tag = be.player_tag
                    WHERE be.tournament_tag = ? AND be.battle_time >= ?""",
                (row["tag"], cutoff),
            ).fetchall()
        ]
        return {"tournament_tag": row["tag"], "members": row["members"], "names": names}
    return None


def _emit_tournament_finished(tournament_tag: str, tournament: dict, api_data: dict) -> None:
    """Land a finished tournament on the clan stream for the awareness loop.

    Carries the facts the deterministic close-post used to format itself —
    final podium, participant count, deck format — so the brain narrates from
    the same evidence rather than re-deriving it from the tournament tables.
    Best-effort: a tournament that ends must still close cleanly even if the
    emit fails, so this never raises into the watch tick.
    """
    from engine.db import connect
    from engine.emitters import insert_stream_event
    from engine.tick import HOME_CLAN

    members = api_data.get("membersList") or []
    podium = [
        {
            "rank": m.get("rank"),
            "name": m.get("name"),
            "tag": m.get("tag"),
            "score": m.get("score"),
        }
        for m in sorted(members, key=lambda m: m.get("rank") or 999)[:3]
    ]
    name = tournament.get("name") or api_data.get("name") or tournament_tag
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = None
    try:
        conn = connect()
        insert_stream_event(
            conn,
            "clan_events",
            dedup_key=f"tournament_finished:{tournament_tag}",
            event_type="tournament_finished",
            subject_cols={"clan_tag": HOME_CLAN, "subject_tag": None},
            observed_at=observed_at,
            window_start=None,
            payload={
                "name": name,
                "tournament_tag": tournament_tag,
                "participants": len(members),
                "podium": podium,
                "deck_selection": api_data.get("deckSelection"),
                "game_mode": tournament.get("game_mode_name"),
            },
            timing="exact",
        )
        conn.commit()
        log.info("tournament_finished emitted for %s (%s players)", tournament_tag, len(members))
    except Exception:
        log.warning("tournament_finished emit failed for %s", tournament_tag, exc_info=True)
    finally:
        if conn is not None:
            conn.close()


def _raise_tournament_clan_chat_relay(
    tournament_tag: str, tournament: dict, api_data: dict
) -> None:
    """Post-tournament commentary for the whole clan, in game.

    In-game clan chat is the only surface that reaches every member -- Discord
    is an opted-in subset -- so a tournament the clan played together belongs
    there, not only in #announcements. Elixir cannot type into the game, so this
    rides the existing HITL path: an `in_game_relay` card a leader pastes.

    Copy runs through the same guardrails as every other clan-chat message
    (200-char cap, sentence-aware clip, the Supercell censor-filter guard, and
    the `- E` signature). Best-effort: a tournament must close cleanly even if
    the card cannot be raised.
    """
    from runtime.clan_chat_copy import signed_valid_messages
    from runtime.leader_action_ui import CLASH_COPY_MAX_LENGTH
    from storage.leader_actions import create_leader_action_recommendation

    members = api_data.get("membersList") or []
    podium = sorted(members, key=lambda m: m.get("rank") or 999)[:3]
    name = tournament.get("name") or api_data.get("name") or "the tournament"

    if podium:
        lead = podium[0]
        bits = [f"{name} wrapped. {lead.get('name')} took it with {lead.get('score')} wins"]
        if len(podium) > 1:
            bits.append(", then " + " and ".join(str(m.get("name")) for m in podium[1:]))
        bits.append(f". {len(members)} played. good games all")
        draft = "".join(bits)
    else:
        draft = f"{name} wrapped with {len(members)} players. good games all"

    copies = signed_valid_messages(draft, max_chars=CLASH_COPY_MAX_LENGTH)
    if not copies:
        log.warning("tournament clan-chat copy rejected by guardrails: %r", draft)
        return
    copy_text = copies[0]
    try:
        create_leader_action_recommendation(
            action_type="in_game_relay",
            objective=f"Tournament recap: {name}",
            prompt_text=f"Paste this clan-chat note: {copy_text}",
            rationale=f"{len(members)} played {name} ({tournament_tag}); in-game chat reaches everyone.",
            target_channel_key="arena-relay",
            source_signal_key=f"tournament_finished:{tournament_tag}",
            source_signal_type="tournament_recap",
            copy_original_text=copy_text,
            copy_current_text=copy_text,
            action_key=f"tournament_relay:{tournament_tag}",
        )
        log.info("tournament clan-chat relay card raised for %s", tournament_tag)
    except Exception:
        log.warning("tournament clan-chat relay failed for %s", tournament_tag, exc_info=True)


async def _tournament_watch_tick():
    """Poll the active tournament for standings and capture participant battle logs."""
    tournament = await asyncio.to_thread(db.get_active_tournament)
    if not tournament:
        stop_tournament_watch()
        return

    tag = tournament["tournament_tag"]
    tournament_id = tournament["tournament_id"]
    runtime_status.mark_job_start("tournament_watch")

    try:
        api_data = await asyncio.to_thread(cr_api.get_tournament, tag)
        if api_data is None:
            log.warning("Tournament watch: API returned None for %s", tag)
            runtime_status.mark_job_failure("tournament_watch", f"API returned None for {tag}")
            return

        poll_result = await asyncio.to_thread(db.poll_tournament, tag, api_data)
        participants = poll_result.get("participants") or []
        live_signals = poll_result.get("live_signals") or []

        # Capture battle logs when tournament is active or just ended
        api_status = api_data.get("status") or ""
        battles_captured = 0
        tournament_name = api_data.get("name") or tag

        # Tournament-own timing, so battle-played signals can cite the
        # tournament's clock instead of the war/river-race clock.
        from storage.tournament import _compute_ends_time

        _duration_s = api_data.get("duration")
        _started_time = api_data.get("startedTime") or tournament.get("started_time")
        tournament_timing = {
            "duration_seconds": _duration_s,
            "duration_minutes": (_duration_s // 60) if isinstance(_duration_s, int) else None,
            "started_time": _started_time,
            "ends_time": _compute_ends_time(_started_time, _duration_s),
        }

        if api_status in ("inProgress", "ended"):
            tournament_tag_with_hash = f"#{tag.lstrip('#')}"
            for p in participants:
                p_tag = p["player_tag"]
                try:
                    battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, p_tag)
                    admission = observations.admit("player_battlelog", p_tag, battle_log)
                    if not admission.accepted:
                        if not admission.transport_failure:
                            log.error(
                                "runtime.tournament_battlelog: CR observation rejected by "
                                "admission boundary: entity_key=%s errors=%s",
                                admission.entity_key,
                                admission.errors,
                            )
                        else:
                            log.warning(
                                "Tournament watch: rejected battle log for %s: %s",
                                p_tag,
                                admission.errors,
                            )
                    elif battle_log:
                        # Store tournament battles in dedicated table
                        for battle in battle_log:
                            if battle.get("tournamentTag") == tournament_tag_with_hash:
                                battle_info = await asyncio.to_thread(
                                    db.store_tournament_battle, tournament_id, battle
                                )
                                if battle_info:
                                    battles_captured += 1
                                    signal = _build_battle_played_signal(
                                        tag,
                                        tournament_name,
                                        battle_info,
                                        tournament_timing=tournament_timing,
                                    )
                                    live_signals.append(signal)
                        # Also feed through existing battle log pipeline
                        await asyncio.to_thread(db.snapshot_player_battlelog, p_tag, battle_log)
                except Exception as e:
                    log.warning("Tournament watch: battle log failed for %s: %s", p_tag, e)
                await asyncio.sleep(TOURNAMENT_BATTLE_LOG_SPACING_SECONDS)

        # Handle tournament end
        if api_status == "ended" and tournament["status"] != "ended":
            await asyncio.to_thread(db.finalize_tournament, tag, api_data)
            log.info("Tournament %s ended — emitting tournament_finished", tag)
            # A finished tournament is a bounded clan moment, so it lands on the
            # clan stream and the awareness loop narrates it — the same path
            # season_closed and member_joined take (#210). The bespoke
            # close-post + deferred `tournament_recap` LLM workflow that used to
            # run here made tournaments a second proactive owner, composing and
            # posting in parallel with the brain.
            live_signals = [s for s in live_signals if s.get("type") != "tournament_ended"]
            await asyncio.to_thread(_emit_tournament_finished, tag, tournament, api_data)
            await asyncio.to_thread(_raise_tournament_clan_chat_relay, tag, tournament, api_data)
            stop_tournament_watch()

        # Post live tournament signals directly to #elixir (v5-style),
        # replacing the v4 awareness pipeline (item 7).
        if live_signals:
            try:
                channel_id = _get_singleton_channel_id("elixir")
                channel = _bot().get_channel(channel_id) if channel_id else None
                if channel is None:
                    log.error("Tournament watch: #elixir channel not found")
                else:
                    ok = await compose_and_post(
                        channel,
                        lane="elixir",
                        context=_tournament_signal_context(live_signals),
                    )
                    if not ok:
                        log.warning("Tournament watch: live signal post failed")
            except Exception as e:
                log.warning("Tournament watch: signal delivery failed: %s", e)

        summary = f"poll #{tournament['poll_count'] + 1}, {len(participants)} participants, {battles_captured} new battles"
        if api_status == "ended":
            summary += " [ENDED]"
        runtime_status.mark_job_success("tournament_watch", summary)

    except Exception as exc:
        log.error("Tournament watch failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("tournament_watch", str(exc))


def _tournament_signal_context(signals: list[dict]) -> str:
    """Facts for the agent to compose a live tournament update for #elixir."""
    import json

    return (
        "Live tournament update(s) for the clan. Write a short #elixir post in "
        "your own voice about what's happening (e.g. a tournament started, a lead "
        "change). Use only these facts; do not invent details.\n\n"
        f"```json\n{json.dumps(signals, indent=2, default=str)}\n```"
    )


def _format_tournament_close_post(tournament_name: str, api_data: dict, *, top_n: int = 10) -> str:
    """Deterministic close-out post for a tournament.

    Facts only — final leaderboard, deck format, total participants. The
    narrative recap fires separately a couple minutes later.
    """
    members = api_data.get("membersList") or []
    top = sorted(members, key=lambda m: m.get("rank") or 999)[:top_n]
    deck_label = {
        "draftCompetitive": "Triple Draft",
        "collection": "Bring Your Own Deck",
        "draft": "Draft",
    }.get(api_data.get("deckSelection") or "", api_data.get("deckSelection") or "")
    header_bits = [f"{len(members)} players"]
    if deck_label:
        header_bits.append(deck_label)
    lines = [
        f"**Tournament Complete | {tournament_name}**",
        " · ".join(header_bits),
        "",
        "Final leaderboard:",
    ]
    for m in top:
        rank = m.get("rank", "?")
        name = m.get("name", "?")
        score = m.get("score", 0)
        lines.append(f"{rank}. **{name}** — {score} wins")
    if len(members) > top_n:
        lines.append(f"…and {len(members) - top_n} more")
    return "\n".join(lines)


async def _post_tournament_close(tournament_tag: str, api_data: dict) -> None:
    """Post the deterministic close-out (facts + leaderboard) to #elixir."""
    try:
        tournament = await asyncio.to_thread(db.get_tournament_by_tag, tournament_tag)
        tournament_name = (tournament or {}).get("name") or api_data.get("name") or tournament_tag
        text = _format_tournament_close_post(tournament_name, api_data)
        channel_id = _get_singleton_channel_id("elixir")
        if not channel_id:
            log.error("Tournament close: #elixir channel not configured")
            return
        channel = _bot().get_channel(channel_id)
        if not channel:
            log.error("Tournament close: could not resolve channel %s", channel_id)
            return
        await _post_to_elixir(channel, {"content": text})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            text,
            **_channel_msg_kwargs(channel),
            workflow="channel_update",
            event_type="tournament_complete",
        )
        log.info("Tournament close posted for %s", tournament_tag)
    except Exception as exc:
        log.error("Tournament close post failed: %s", exc, exc_info=True)


def _schedule_tournament_recap(tournament_tag: str, *, delay_seconds: int) -> None:
    """Defer the LLM recap so the close-out post lands first.

    Uses the bot event loop directly (no scheduler entry) so the delay
    survives the tick returning. If Elixir restarts during the delay
    window, the boot-time check in resume_pending_tournament_recaps()
    picks up the recap so it isn't lost.
    """

    def _kick():
        _bot().loop.create_task(_tournament_recap(tournament_tag))

    _bot().loop.call_soon_threadsafe(lambda: _bot().loop.call_later(max(0, delay_seconds), _kick))


async def resume_pending_tournament_recaps() -> None:
    """On boot, fire any recap that was deferred but never posted before
    a restart. Catches the gap between the deterministic close post and
    the delayed LLM recap.
    """
    try:
        rows = await asyncio.to_thread(db.list_pending_tournament_recaps)
    except Exception as exc:
        log.warning("Pending tournament recap check failed: %s", exc)
        return
    for row in rows or []:
        tag = row.get("tournament_tag")
        if not tag:
            continue
        log.info("Resuming pending tournament recap for %s", tag)
        await _tournament_recap(tag)


async def _tournament_recap(tournament_tag: str) -> bool:
    """Generate and post a tournament recap to #elixir."""
    try:
        context = await asyncio.to_thread(db.build_tournament_recap_context, tournament_tag)
        if not context:
            log.warning("Tournament recap: no context for %s", tournament_tag)
            return False

        recap_text = elixir_agent.generate_tournament_recap(context)
        if not recap_text:
            log.warning("Tournament recap: LLM returned empty for %s", tournament_tag)
            return False

        tournament = await asyncio.to_thread(db.get_tournament_by_tag, tournament_tag)
        tournament_name = (tournament or {}).get("name") or tournament_tag
        title = f"**Tournament Recap | {tournament_name}**"
        full_post = f"{title}\n\n{recap_text}"

        channel_id = _get_singleton_channel_id("elixir")
        if not channel_id:
            log.error("Tournament recap: #elixir channel not configured")
            return False
        channel = _bot().get_channel(channel_id)
        if not channel:
            log.error("Tournament recap: could not resolve channel %s", channel_id)
            return False

        await _post_to_elixir(channel, {"content": full_post})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            full_post,
            **_channel_msg_kwargs(channel),
            workflow="channel_update",
            event_type="tournament_recap",
        )

        # Update recap_posted_at. Use _canon_tag — tournaments are stored
        # WITH the leading "#", so a bare lstrip+upper on the input would
        # never match.
        from db import _canon_tag, _utcnow, get_connection

        canon_tag = _canon_tag(tournament_tag)

        def _mark_recap_posted():
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE tournaments SET recap_posted_at = ? WHERE tournament_tag = ?",
                    (_utcnow(), canon_tag),
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.to_thread(_mark_recap_posted)

        log.info("Tournament recap posted for %s", tournament_tag)
        return True
    except Exception:
        log.exception("tournament.recap failed: tournament_tag=%s", tournament_tag)
        return False


async def maybe_autowatch_tournament() -> dict | None:
    """Start watching a tournament the clan is already playing, unasked.

    Watching used to require a human to type `/tournament watch <tag>`, so a
    tournament nobody thought to register was invisible -- even though Elixir had
    already ingested every battle of it. Reuses the exact bootstrap the slash
    command uses: validate against the API, refuse an ended one, register, start
    the poller.

    Best-effort and quiet: returns None when there is nothing to do.
    """
    from engine.db import connect

    if await asyncio.to_thread(db.get_active_tournament):
        return None  # already watching one; never run two

    conn = None
    try:
        conn = connect()
        found = find_unwatched_clan_tournament(conn)
    except Exception:
        log.warning("tournament autowatch scan failed", exc_info=True)
        return None
    finally:
        if conn is not None:
            conn.close()
    if not found:
        return None

    tag = str(found["tournament_tag"]).lstrip("#")
    api_data = await asyncio.to_thread(cr_api.get_tournament, tag)
    if not api_data:
        log.info("tournament autowatch: %s not resolvable via API", tag)
        return None
    if (api_data.get("status") or "") == "ended":
        # Detection lagged past the finish. Register it anyway so the same
        # tournament is not re-detected every tick forever.
        await asyncio.to_thread(db.register_tournament, tag, api_data)
        log.info("tournament autowatch: %s already ended; recorded, not watched", tag)
        return {"tournament_tag": tag, "watched": False}

    await asyncio.to_thread(db.register_tournament, tag, api_data)
    start_tournament_watch()
    log.info(
        "tournament autowatch: watching %s (%s) — detected from %s member(s): %s",
        api_data.get("name") or tag,
        tag,
        found["members"],
        ", ".join(str(n) for n in found["names"][:6]),
    )
    return {
        "tournament_tag": tag,
        "watched": True,
        "members": found["members"],
        "names": found["names"],
    }


def start_tournament_watch():
    """Add the tournament watch job to the scheduler."""
    try:
        _runtime_app().scheduler.remove_job(_TOURNAMENT_JOB_ID)
    except JobLookupError:
        pass  # job may not exist yet

    # Register the coroutine directly so max_instances/coalesce actually guard
    # the tick. The old call_soon_threadsafe shim returned instantly, so
    # APScheduler's overlap guard only ever saw the no-op shim (see the
    # scheduler setup in runtime/app.py).
    _runtime_app().scheduler.add_job(
        _tournament_watch_tick,
        "interval",
        id=_TOURNAMENT_JOB_ID,
        name="tournament-watch",
        minutes=TOURNAMENT_POLL_MINUTES,
        max_instances=1,
        coalesce=True,
    )
    log.info("Tournament watch started (every %d minutes)", TOURNAMENT_POLL_MINUTES)


def stop_tournament_watch():
    """Remove the tournament watch job from the scheduler."""
    try:
        _runtime_app().scheduler.remove_job(_TOURNAMENT_JOB_ID)
        log.info("Tournament watch stopped")
    except JobLookupError:
        pass  # job may not exist
