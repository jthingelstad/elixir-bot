from __future__ import annotations

import asyncio
import logging

import db
from runtime.leader_action_feedback import queue_leader_action_feedback_refresh
from runtime.leader_action_ui import refresh_leader_action_card

log = logging.getLogger("elixir")

THUMBS_UP = "\N{THUMBS UP SIGN}"
THUMBS_DOWN = "\N{THUMBS DOWN SIGN}"
WHITE_CHECK_MARK = "\N{WHITE HEAVY CHECK MARK}"
BALLOT_BOX_WITH_CHECK = "\N{BALLOT BOX WITH CHECK}"
CROSS_MARK = "\N{CROSS MARK}"


def feedback_value_for_emoji(emoji) -> str | None:
    value = str(emoji or "").strip()
    if value == THUMBS_UP:
        return "up"
    if value == THUMBS_DOWN:
        return "down"
    return None


def leader_action_value_for_emoji(emoji) -> str | None:
    value = str(emoji or "").strip()
    if value in {
        WHITE_CHECK_MARK,
        BALLOT_BOX_WITH_CHECK,
        f"{BALLOT_BOX_WITH_CHECK}\ufe0f",
    }:
        return db.ACTION_DONE
    if value == CROSS_MARK:
        return db.ACTION_REJECTED
    return None


def _assistant_message_lookup(payload) -> tuple[dict | None, dict | None]:
    import runtime.app as app

    channel_config = app._get_channel_behavior(payload.channel_id)
    if not channel_config:
        return None, None
    if channel_config.get("lane") != "ask-elixir":
        return channel_config, None
    assistant = db.get_message_by_discord_message_id(payload.message_id)
    return channel_config, assistant


async def _fetch_channel_and_message(payload):
    import runtime.app as app

    channel = app.bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await app.bot.fetch_channel(payload.channel_id)
        except Exception:
            app.log.warning(
                "prompt_feedback channel fetch failed channel_id=%s",
                payload.channel_id,
                exc_info=True,
            )
            return None, None
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        app.log.warning(
            "prompt_feedback message fetch failed channel_id=%s message_id=%s",
            payload.channel_id,
            payload.message_id,
            exc_info=True,
        )
        return channel, None
    return channel, message


async def _acknowledge_feedback(payload):
    _channel, message = await _fetch_channel_and_message(payload)
    if message is None:
        return None
    try:
        await message.add_reaction(WHITE_CHECK_MARK)
    except Exception:
        import runtime.app as app

        app.log.warning("Failed to add ask-elixir feedback acknowledgement reaction", exc_info=True)
    return message


async def _post_retry_invitation(payload, *, prompt_feedback_id: int | None, message=None) -> None:
    if message is None:
        _channel, message = await _fetch_channel_and_message(payload)
    if message is None:
        return
    content = (
        f"<@{payload.user_id}> if that missed, ask me again or tell me what felt off "
        "and I'll take another shot."
    )
    try:
        import runtime.app as app

        sent = await app._safe_reply(message, content)
        if prompt_feedback_id:
            await asyncio.to_thread(
                db.mark_prompt_feedback_retry_invited,
                prompt_feedback_id,
                retry_message_id=getattr(sent, "id", None),
            )
    except Exception:
        import runtime.app as app

        app.log.warning(
            "Failed to send ask-elixir retry invitation after thumbs-down",
            exc_info=True,
        )


def reflection_enabled() -> bool:
    """Phase 4 ships ON for capture and OFF for the nightly pass by default.

    Recording evidence is safe and reversible — the rows are inert until a
    reflection reads them, and a lane that turns out to be poisoned empties with
    one delete. Writing lessons that reach every chassis turn is the part that
    earns a flag.
    """
    import os

    return os.getenv("ELIXIR_REFLECTION", "0").strip().lower() not in ("0", "false", "no", "off")


async def _record_editorial_reaction(payload, *, removed: bool) -> None:
    """Attribute a leader's reaction to the Elixir post it landed on.

    Never raises into the reaction handler: feedback capture failing must not
    stop a thumbs-up from being acknowledged or a leader action from resolving.
    """
    import runtime.app as app

    if not app._has_leader_role(getattr(payload, "member", None)):
        return

    def _write():
        import db
        from engine import editor

        conn = db.get_connection()
        try:
            return editor.record_post_reaction(
                conn,
                discord_message_id=str(payload.message_id),
                emoji=str(getattr(payload, "emoji", "")),
                reactor_id=str(payload.user_id),
                removed=removed,
            )
        finally:
            conn.close()

    try:
        memory_id = await asyncio.to_thread(_write)
    except Exception:
        log.warning("editorial reaction capture failed", exc_info=True)
        return
    if memory_id:
        log.info(
            "editorial_reaction %s emoji=%s message_id=%s reactor=%s memory=%s",
            "removed" if removed else "added",
            getattr(payload, "emoji", ""),
            payload.message_id,
            payload.user_id,
            memory_id,
        )


async def handle_raw_reaction_add(payload) -> None:
    import runtime.app as app

    if not payload or not payload.channel_id or not payload.message_id or not payload.user_id:
        return
    if app.bot.user and int(payload.user_id) == int(app.bot.user.id):
        return
    if getattr(getattr(payload, "member", None), "bot", False):
        return
    action_status = leader_action_value_for_emoji(getattr(payload, "emoji", None))
    if action_status:
        channel_config = app._get_channel_behavior(payload.channel_id)
        if channel_config and channel_config.get("lane") == "actions":
            if not app._has_leader_role(getattr(payload, "member", None)):
                return
            action = await asyncio.to_thread(
                db.decide_leader_action_by_message,
                payload.message_id,
                status=action_status,
                discord_user_id=payload.user_id,
                emoji=str(getattr(payload, "emoji", "")),
            )
            if action:
                log.info(
                    "leader_action_decision action_id=%s type=%s status=%s message_id=%s reactor=%s",
                    action.get("action_id"),
                    action.get("action_type"),
                    action.get("status"),
                    payload.message_id,
                    payload.user_id,
                )
                queue_leader_action_feedback_refresh(action.get("action_type"))
                await refresh_leader_action_card(app.bot, action)
                if action.get("action_type") == "member_outreach":
                    try:
                        await app._member_outreach_decision(action, action_status)
                    except Exception:
                        log.exception("member outreach decision handling failed")
            else:
                # None now covers three cases, and two of them mean the card on
                # screen is stale: it was already decided (the open-card guard
                # refused), or it is a classification card that resolves only
                # via its own buttons. Reactions have no interaction token, so
                # the only correction available is to re-render the card from
                # the current row — otherwise the reaction just sits there and
                # the leader believes it landed.
                stale = await asyncio.to_thread(db.get_leader_action_by_message, payload.message_id)
                if stale:
                    log.info(
                        "leader_action_reaction_ignored action_id=%s status=%s "
                        "decided_by=%s message_id=%s reactor=%s",
                        stale.get("action_id"),
                        stale.get("status"),
                        stale.get("decided_by_discord_user_id"),
                        payload.message_id,
                        payload.user_id,
                    )
                    await refresh_leader_action_card(app.bot, stale)
                else:
                    log.info(
                        "leader_action_reaction_unmatched message_id=%s reactor=%s",
                        payload.message_id,
                        payload.user_id,
                    )
            return
    # Phase 4: any leadership reaction on something Elixir POSTED is editorial
    # evidence, whatever the emoji. This is deliberately not an emoji vocabulary
    # — the join is the delivery intent, so a reaction on any other message in
    # the channel is not feedback about Elixir's writing and never lands here.
    await _record_editorial_reaction(payload, removed=False)

    feedback_value = feedback_value_for_emoji(getattr(payload, "emoji", None))
    if not feedback_value:
        return

    channel_config, assistant = await asyncio.to_thread(_assistant_message_lookup, payload)
    if not channel_config or not assistant:
        return
    if assistant.get("author_type") != "assistant":
        return
    if (assistant.get("workflow") or "").strip().lower() != "interactive":
        return
    if (assistant.get("discord_user_id") or "") != str(payload.user_id):
        return

    feedback = await asyncio.to_thread(
        db.upsert_prompt_feedback,
        assistant_discord_message_id=payload.message_id,
        discord_user_id=payload.user_id,
        original_asker_discord_user_id=assistant.get("discord_user_id"),
        workflow=assistant.get("workflow"),
        channel_id=assistant.get("channel_id"),
        channel_name=channel_config.get("name"),
        feedback_value=feedback_value,
    )
    became_active_down = feedback_value == "down" and feedback.get("became_active_down")
    # Surface every feedback event in elixir-v5.log so log-triage can see it.
    # Thumbs-down is a quality signal we want to triage promptly, so it goes
    # WARNING; thumbs-up is informational. Only the first thumbs-down per
    # message+user gets WARNING (became_active_down=True) — toggle-and-back
    # is downgraded to INFO so we don't spam triage with re-reactions.
    log_level = log.warning if became_active_down else log.info
    log_level(
        "prompt_feedback emoji=%s channel=%s workflow=%s message_id=%s reactor=%s asker=%s",
        f"thumbs_{feedback_value}",
        channel_config.get("name"),
        assistant.get("workflow"),
        payload.message_id,
        payload.user_id,
        assistant.get("discord_user_id"),
    )
    message = await _acknowledge_feedback(payload)
    if became_active_down:
        await _post_retry_invitation(
            payload,
            prompt_feedback_id=feedback.get("prompt_feedback_id"),
            message=message,
        )


async def handle_raw_reaction_remove(payload) -> None:
    import runtime.app as app

    if not payload or not payload.channel_id or not payload.message_id or not payload.user_id:
        return
    if app.bot.user and int(payload.user_id) == int(app.bot.user.id):
        return
    action_status = leader_action_value_for_emoji(getattr(payload, "emoji", None))
    if action_status:
        channel_config = app._get_channel_behavior(payload.channel_id)
        if channel_config and channel_config.get("lane") == "actions":
            action = await asyncio.to_thread(
                db.clear_leader_action_decision_by_message,
                payload.message_id,
                discord_user_id=payload.user_id,
                emoji=str(getattr(payload, "emoji", "")),
            )
            if action:
                log.info(
                    "leader_action_decision_cleared action_id=%s type=%s status=%s message_id=%s reactor=%s",
                    action.get("action_id"),
                    action.get("action_type"),
                    action.get("status"),
                    payload.message_id,
                    payload.user_id,
                )
                await refresh_leader_action_card(app.bot, action)
            return
    # Taking a reaction back is itself a signal — a leader who removes a
    # thumbs-up has changed their mind, and the reflection should see both.
    await _record_editorial_reaction(payload, removed=True)

    feedback_value = feedback_value_for_emoji(getattr(payload, "emoji", None))
    if not feedback_value:
        return

    channel_config, assistant = await asyncio.to_thread(_assistant_message_lookup, payload)
    if not channel_config or not assistant:
        return
    if assistant.get("author_type") != "assistant":
        return
    if (assistant.get("workflow") or "").strip().lower() != "interactive":
        return
    if (assistant.get("discord_user_id") or "") != str(payload.user_id):
        return

    await asyncio.to_thread(
        db.clear_prompt_feedback,
        assistant_discord_message_id=payload.message_id,
        discord_user_id=payload.user_id,
        feedback_value=feedback_value,
    )


__all__ = [
    "THUMBS_DOWN",
    "THUMBS_UP",
    "WHITE_CHECK_MARK",
    "BALLOT_BOX_WITH_CHECK",
    "CROSS_MARK",
    "feedback_value_for_emoji",
    "handle_raw_reaction_add",
    "handle_raw_reaction_remove",
    "leader_action_value_for_emoji",
]
