"""Tournament watch."""

__all__ = [
    "TOURNAMENT_POLL_MINUTES", "TOURNAMENT_BATTLE_LOG_SPACING_SECONDS",
    "_TOURNAMENT_JOB_ID", "_tournament_watch_tick", "_tournament_recap",
    "start_tournament_watch", "stop_tournament_watch",
]

import asyncio
import os
from typing import Optional

import cr_api
import db
import elixir_agent
from runtime import app as _app
from runtime.app import bot, log
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.jobs._signals import _deliver_signal_group, _post_to_elixir


TOURNAMENT_POLL_MINUTES = int(os.getenv("TOURNAMENT_POLL_MINUTES", "5"))
TOURNAMENT_BATTLE_LOG_SPACING_SECONDS = 0.5
TOURNAMENT_RECAP_DELAY_SECONDS = int(os.getenv("TOURNAMENT_RECAP_DELAY_SECONDS", "120"))
_TOURNAMENT_JOB_ID = "tournament-watch"


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
                    if battle_log:
                        # Store tournament battles in dedicated table
                        for battle in battle_log:
                            if battle.get("tournamentTag") == tournament_tag_with_hash:
                                battle_info = await asyncio.to_thread(
                                    db.store_tournament_battle, tournament_id, battle
                                )
                                if battle_info:
                                    battles_captured += 1
                                    live_signals.append(_build_battle_played_signal(
                                        tag, tournament_name, battle_info,
                                        tournament_timing=tournament_timing,
                                    ))
                        # Also feed through existing battle log pipeline
                        await asyncio.to_thread(db.snapshot_player_battlelog, p_tag, battle_log)
                except Exception as e:
                    log.warning("Tournament watch: battle log failed for %s: %s", p_tag, e)
                await asyncio.sleep(TOURNAMENT_BATTLE_LOG_SPACING_SECONDS)

        # Handle tournament end
        if api_status == "ended" and tournament["status"] != "ended":
            await asyncio.to_thread(db.finalize_tournament, tag, api_data)
            log.info("Tournament %s ended — posting close, deferring recap %ds", tag, TOURNAMENT_RECAP_DELAY_SECONDS)
            # The "tournament_ended" signal in live_signals would otherwise
            # generate a chatty narrative post via tournament_update.
            # Replace it with a deterministic facts + leaderboard post and
            # defer the LLM recap so the two don't land on top of each other.
            live_signals = [s for s in live_signals if s.get("type") != "tournament_ended"]
            await _post_tournament_close(tag, api_data)
            _schedule_tournament_recap(tag, delay_seconds=TOURNAMENT_RECAP_DELAY_SECONDS)
            stop_tournament_watch()

        # Deliver live signals
        for signal in live_signals:
            try:
                await _deliver_signal_group([signal], {}, {})
            except Exception as e:
                log.warning("Tournament watch: signal delivery failed: %s", e)

        summary = f"poll #{tournament['poll_count'] + 1}, {len(participants)} participants, {battles_captured} new battles"
        if api_status == "ended":
            summary += " [ENDED]"
        runtime_status.mark_job_success("tournament_watch", summary)

    except Exception as exc:
        log.error("Tournament watch failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("tournament_watch", str(exc))


def _format_tournament_close_post(tournament_name: str, api_data: dict, *, top_n: int = 10) -> str:
    """Deterministic close-out post for a tournament.

    Facts only — final leaderboard, deck format, total participants. The
    narrative recap fires separately a couple minutes later.
    """
    members = api_data.get("membersList") or []
    top = sorted(members, key=lambda m: m.get("rank") or 999)[:top_n]
    deck_label = (
        {
            "draftCompetitive": "Triple Draft",
            "collection": "Bring Your Own Deck",
            "draft": "Draft",
        }.get(api_data.get("deckSelection") or "", api_data.get("deckSelection") or "")
    )
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
    """Post the deterministic close-out (facts + leaderboard) to #clan-events."""
    try:
        tournament = await asyncio.to_thread(db.get_tournament_by_tag, tournament_tag)
        tournament_name = (tournament or {}).get("name") or api_data.get("name") or tournament_tag
        text = _format_tournament_close_post(tournament_name, api_data)
        channel_id = _get_singleton_channel_id("clan-events")
        if not channel_id:
            log.error("Tournament close: #clan-events channel not configured")
            return
        channel = bot.get_channel(channel_id)
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
        bot.loop.create_task(_tournament_recap(tournament_tag))

    bot.loop.call_soon_threadsafe(
        lambda: bot.loop.call_later(max(0, delay_seconds), _kick)
    )


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


async def _tournament_recap(tournament_tag: str):
    """Generate and post a tournament recap to #clan-events."""
    try:
        context = await asyncio.to_thread(db.build_tournament_recap_context, tournament_tag)
        if not context:
            log.warning("Tournament recap: no context for %s", tournament_tag)
            return

        recap_text = elixir_agent.generate_tournament_recap(context)
        if not recap_text:
            log.warning("Tournament recap: LLM returned empty for %s", tournament_tag)
            return

        tournament = await asyncio.to_thread(db.get_tournament_by_tag, tournament_tag)
        tournament_name = (tournament or {}).get("name") or tournament_tag
        title = f"**Tournament Recap | {tournament_name}**"
        full_post = f"{title}\n\n{recap_text}"

        channel_id = _get_singleton_channel_id("clan-events")
        if not channel_id:
            log.error("Tournament recap: #clan-events channel not configured")
            return
        channel = bot.get_channel(channel_id)
        if not channel:
            log.error("Tournament recap: could not resolve channel %s", channel_id)
            return

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
        from db import get_connection, _utcnow, _canon_tag
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
    except Exception as exc:
        log.error("Tournament recap generation failed: %s", exc, exc_info=True)


def start_tournament_watch():
    """Add the tournament watch job to the scheduler."""
    try:
        _app.scheduler.remove_job(_TOURNAMENT_JOB_ID)
    except Exception:
        pass  # job may not exist yet

    def _job_runner():
        bot.loop.call_soon_threadsafe(
            lambda: bot.loop.create_task(_tournament_watch_tick())
        )

    _app.scheduler.add_job(
        _job_runner,
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
        _app.scheduler.remove_job(_TOURNAMENT_JOB_ID)
        log.info("Tournament watch stopped")
    except Exception:
        pass  # job may not exist
