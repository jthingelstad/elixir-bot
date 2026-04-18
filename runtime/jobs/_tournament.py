"""Tournament watch."""

__all__ = [
    "TOURNAMENT_POLL_MINUTES", "TOURNAMENT_BATTLE_LOG_SPACING_SECONDS",
    "_TOURNAMENT_JOB_ID", "_tournament_watch_tick", "_tournament_recap",
    "start_tournament_watch", "stop_tournament_watch",
]

import asyncio
import os

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
_TOURNAMENT_JOB_ID = "tournament-watch"


def _build_battle_played_signal(tournament_tag: str, tournament_name: str, battle_info: dict) -> dict:
    """Shape a tournament_battle_played signal payload for awareness delivery.

    The audience framing (supportive clan commentary vs. neutral observation)
    is encoded as an explicit payload flag so the prompt can pick tone
    without re-deriving it from member metadata. Dedup is already guaranteed
    by store_tournament_battle's INSERT OR IGNORE on the canonicalized
    (p1_tag, p2_tag, battle_time) triple, so the signal fires exactly once
    per match even though we see it in both players' battle logs.
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
    if winner_tag and winner_tag == p1_tag:
        winner_name = battle_info.get("player1_name")
        loser_name = battle_info.get("player2_name")
    elif winner_tag and winner_tag == p2_tag:
        winner_name = battle_info.get("player2_name")
        loser_name = battle_info.get("player1_name")
    else:
        winner_name = None
        loser_name = None

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
        },
        "player2": {
            "tag": p2_tag,
            "name": battle_info.get("player2_name"),
            "is_clan_member": p2_is_member,
            "crowns": battle_info.get("player2_crowns"),
            "deck": battle_info.get("player2_deck") or [],
        },
        "winner_tag": winner_tag,
        "winner_name": winner_name,
        "loser_name": loser_name,
        "deck_selection": battle_info.get("deck_selection"),
        "game_mode_name": battle_info.get("game_mode_name"),
        "arena_name": battle_info.get("arena_name"),
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
                                        tag, tournament_name, battle_info
                                    ))
                        # Also feed through existing battle log pipeline
                        await asyncio.to_thread(db.snapshot_player_battlelog, p_tag, battle_log)
                except Exception as e:
                    log.warning("Tournament watch: battle log failed for %s: %s", p_tag, e)
                await asyncio.sleep(TOURNAMENT_BATTLE_LOG_SPACING_SECONDS)

        # Handle tournament end
        if api_status == "ended" and tournament["status"] != "ended":
            await asyncio.to_thread(db.finalize_tournament, tag, api_data)
            log.info("Tournament %s ended — generating recap", tag)
            await _tournament_recap(tag)
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

        # Update recap_posted_at
        from db import get_connection, _utcnow
        def _mark_recap_posted():
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE tournaments SET recap_posted_at = ? WHERE tournament_tag = ?",
                    (_utcnow(), tournament_tag.lstrip("#").upper()),
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
