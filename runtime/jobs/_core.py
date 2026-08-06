"""Shared utilities and remaining job executors."""

__all__ = [
    "WEEKLY_RECAP_DAY",
    "WEEKLY_RECAP_HOUR",
    "_build_weekly_clan_recap_context",
    "_query_or_default",
    "_summarize_member_rows",
    "_build_ask_elixir_daily_insight_context",
    "_ask_elixir_daily_insight",
    "_weekly_clan_recap",
    "_weekly_discord_invite_relay",
]

import asyncio
import logging
import os
from datetime import datetime

import discord
import pytz

import db
import elixir_agent
from runtime import email_dedup
from runtime import status as runtime_status
from runtime.clan_chat_copy import generate_clan_chat_copy
from runtime.helpers import (
    _channel_config_by_key,
    _channel_msg_kwargs,
    _channel_scope,
    _format_weekly_recap_post,
    _get_singleton_channel_id,
    _strip_weekly_recap_header,
    format_weekly_recap_email,
)
from runtime.helpers._common import _load_live_clan_context, _post_to_elixir
from runtime.leader_action_policy import can_post_leader_action
from runtime.leader_action_ui import (
    CLASH_COPY_MAX_LENGTH,
    LEADER_ACTION_UI_VERSION,
    post_leader_action_card,
)
from storage.contextual_memory import upsert_weekly_summary_memory

CHICAGO = pytz.timezone("America/Chicago")
log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


KICK_RECOMMENDATION_FRESH_JOIN_GRACE_DAYS = int(
    os.getenv("KICK_RECOMMENDATION_FRESH_JOIN_GRACE_DAYS", "7")
)
# How long a leader's decline suppresses re-proposing the same role action
# for the same member. Role situations change on roster timescales, so the
# default is 30 days — much longer than the 7-day unanswered-card dedup.
WEEKLY_DISCORD_INVITE_RELAY_DAY = os.getenv("WEEKLY_DISCORD_INVITE_RELAY_DAY", "sat")
WEEKLY_DISCORD_INVITE_RELAY_HOUR = int(os.getenv("WEEKLY_DISCORD_INVITE_RELAY_HOUR", "11"))
WEEKLY_RECAP_DAY = os.getenv("WEEKLY_RECAP_DAY", "mon")
WEEKLY_RECAP_HOUR = int(os.getenv("WEEKLY_RECAP_HOUR", "9"))
WEEKLY_MEMBER_REPORT_DAY = os.getenv("WEEKLY_MEMBER_REPORT_DAY", "mon")
WEEKLY_MEMBER_REPORT_HOUR = int(os.getenv("WEEKLY_MEMBER_REPORT_HOUR", "10"))
KICK_RECOMMENDATION_POLICY_CONTEXT = {
    "primary_signal": "inactivity_or_absence",
    "supporting_signals": ["donations", "war_participation"],
    "faq_alignment": [
        "Wars are encouraged, not required.",
        "Real life comes first when members communicate.",
        "Removal recommendations are for ghosting or inactivity without a heads-up.",
    ],
}
_AVAILABILITY_MEMORY_TERMS = (
    "away",
    "camping",
    "limited signal",
    "vacation",
    "travel",
    "travelling",
    "traveling",
    "unavailable",
    "offline",
    "break",
    "heads-up",
    "headsup",
    "real life",
)
_RETURN_MEMORY_TERMS = (
    "returned",
    "return from",
    "came back",
    "is back",
    "ready to participate",
    "active participation",
)


def _build_weekly_clan_recap_context(*args, **kwargs):
    return _runtime_app()._build_weekly_clan_recap_context(*args, **kwargs)


def _query_or_default(label: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        log.warning("ask-elixir insight data unavailable for %s: %s", label, exc)
        return default


def _summarize_member_rows(rows, *, name_key="name", value_builder=None, limit=5):
    summary = []
    for row in (rows or [])[:limit]:
        name = (
            row.get(name_key) or row.get("current_name") or row.get("member_ref") or row.get("tag")
        )
        if not name:
            continue
        value = value_builder(row) if value_builder else None
        summary.append(f"{name} ({value})" if value else str(name))
    return summary


def _build_ask_elixir_daily_insight_context(clan, war):
    hot_streaks = _query_or_default(
        "hot_streaks",
        lambda: db.get_members_on_hot_streak(min_streak=4) or [],
        [],
    )
    favourite_cards = _query_or_default(
        "favourite_cards",
        lambda: db.get_clan_favourite_card_counts(limit=10) or [],
        [],
    )
    overlooked = _query_or_default(
        "overlooked_cards",
        lambda: (
            db.get_clan_overlooked_cards(min_owners=3, min_level=14, battle_days=14, limit=10) or []
        ),
        [],
    )
    played_cards = _query_or_default(
        "played_cards",
        lambda: db.get_clan_recently_played_cards(days=14, limit=20) or [],
        [],
    )

    def _recent_stream_events(days: int = 7, limit: int = 8) -> list[dict]:
        """v5.1: recent public moments from the streams (was event_facades)."""
        import json as _json

        from engine import db as engine_db

        conn = engine_db.connect()
        try:
            rows = conn.execute(
                """SELECT event_type, observed_at, payload_json FROM (
                       SELECT event_type, observed_at, payload_json FROM player_events
                       WHERE scope = 'public' AND observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
                       UNION ALL
                       SELECT event_type, observed_at, payload_json FROM clan_events
                       WHERE scope = 'public' AND observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)
                   ) ORDER BY observed_at DESC LIMIT ?""",
                (f"-{days} days", f"-{days} days", limit),
            ).fetchall()
            out = []
            for r in rows:
                try:
                    payload = _json.loads(r["payload_json"] or "{}")
                except TypeError, ValueError:
                    payload = {}
                out.append(
                    {
                        "event_type": r["event_type"],
                        "observed_at": r["observed_at"],
                        **payload,
                    }
                )
            return out
        finally:
            conn.close()

    recent_events = _query_or_default(
        "public_recent_events",
        _recent_stream_events,
        [],
    )
    event_windows = {"7d_recent_public_events": len(recent_events)}

    lines = [
        "Write one short daily fun fact for #ask-elixir that teaches members something about a Clash Royale card.",
        "Pick a card from the lists below and teach something useful: a matchup, an elixir trade, a counter, a synergy, a mechanic, or a hidden interaction.",
        "The card lists are just hooks to pick from — do not mention levels, collections, or who owns what.",
        "Focus on gameplay: what the card does well, what beats it, what combos with it, or a non-obvious trick.",
        "Vary your picks — sometimes from popular clan cards, sometimes from overlooked ones, sometimes from cards the clan plays a lot.",
        "Use a playful opener like 'Did you know?', 'Fun fact', or 'Elixir noticed something...'.",
        "Do NOT write about clan wars, River Race, fame, or war participation.",
        "Do NOT mention card levels, who has a card maxed, or collection stats.",
        "Keep it to 1-3 short sentences.",
        "Do not turn it into a recap, reminder, call to action, leadership note, or war order.",
        "If today's data does not support a genuinely interesting insight, return null.",
    ]
    if event_windows or recent_events:
        lines.extend(
            [
                "",
                "=== RECENT PUBLIC EVENT PULSE (variety guardrail, not recap material) ===",
                "Use this only to avoid repeating yesterday's clan topic. Do not mention these events directly.",
            ]
        )
        seven_day = (event_windows.get("7d") or {}) if isinstance(event_windows, dict) else {}
        by_type = seven_day.get("by_type") or {}
        if by_type:
            top_types = sorted(by_type.items(), key=lambda item: (-item[1], item[0]))[:5]
            lines.append(
                "7d event types: "
                + ", ".join(f"{event_type}={count}" for event_type, count in top_types)
            )
        if recent_events:
            lines.append(
                "recent events: "
                + "; ".join(
                    f"{event.get('event_type')}:{event.get('subject_key') or event.get('source_signal_key')}"
                    for event in recent_events[:5]
                )
            )
    if played_cards:
        lines.extend(
            [
                "",
                "=== CARDS THE CLAN IS PLAYING RIGHT NOW ===",
                ", ".join(row["card_name"] for row in played_cards),
            ]
        )
    if favourite_cards:
        lines.extend(
            [
                "",
                "=== CARDS CLAN MEMBERS LOVE (FAVOURITES) ===",
                ", ".join(row["card_name"] for row in favourite_cards),
            ]
        )
    if overlooked:
        lines.extend(
            [
                "",
                "=== CARDS NOBODY IN THE CLAN IS PLAYING ===",
                ", ".join(row["card_name"] for row in overlooked),
            ]
        )
    if hot_streaks:
        lines.extend(
            [
                "",
                "=== MEMBERS ON HOT STREAKS ===",
                "\n".join(
                    f"- {item}"
                    for item in _summarize_member_rows(
                        hot_streaks,
                        value_builder=lambda row: f"{row.get('current_streak') or 0} straight wins",
                    )
                ),
            ]
        )
    return "\n".join(lines)


def _recent_ask_elixir_topics(channel_id, limit: int = 10) -> list[str]:
    """The capability areas the last ~10 daily posts covered, so today can pick a
    DIFFERENT one (anti-repetition — the fix for war-every-day). Reads the saved
    `topic` slug; a content-preview fallback tags an old untopic'd post 'war' when
    it obviously was one."""
    import json as _json

    from engine import db as engine_db

    conn = engine_db.connect()
    try:
        rows = conn.execute(
            "SELECT content, raw_json FROM messages "
            "WHERE channel_id = ? AND author_type = 'assistant' "
            "AND event_type = 'daily_clan_insight' "
            "ORDER BY created_at DESC LIMIT ?",
            (str(channel_id), limit),
        ).fetchall()
    finally:
        conn.close()
    topics: list[str] = []
    for row in rows:
        topic = None
        try:
            topic = (_json.loads(row["raw_json"] or "{}") or {}).get("topic")
        except TypeError, ValueError:
            topic = None
        if not topic:
            preview = (row["content"] or "").lower()
            if any(w in preview for w in ("war", "fame", "river race", "battle day", "boat")):
                topic = "war (inferred)"
        if topic:
            topics.append(str(topic))
    return topics


async def _ask_elixir_daily_insight():
    """Daily #ask-elixir post — feature discovery that ROTATES through Elixir's
    capabilities (decks, cards, stats, donations, awards, the Elder track,
    milestones, modes…), NOT the same war hook every day (reworked 2026-07-12).
    The brain reads the clan + the recent topics to avoid, spotlights a fresh
    capability area with a real hook, and invites members with answerable
    questions. Fail-open to silence: a failed/empty compose posts nothing."""
    runtime_status.mark_job_start("daily_clan_insight")
    try:
        channel_id = _get_singleton_channel_id("ask-elixir")
    except Exception as exc:
        runtime_status.mark_job_failure(
            "daily_clan_insight", f"ask-elixir channel config error: {exc}"
        )
        return

    channel = _bot().get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("daily_clan_insight", "ask-elixir channel not found")
        return

    recent_topics = await asyncio.to_thread(_recent_ask_elixir_topics, channel_id)

    def _compose():
        """Build the clan read and compose the daily post. Runs in a thread;
        returns {"post", "topic"}, None for a genuine no-hook day, or
        {"_error": ...} when composition failed. Never raises."""
        try:
            from runtime.awareness import read as awareness_read

            read = awareness_read.build_read()
            return elixir_agent.generate_ask_elixir_daily(read, recent_topics=recent_topics)
        except Exception as exc:
            log.error("Ask Elixir daily compose failed", exc_info=True)
            return {"_error": {"kind": "exception", "detail": str(exc)}}

    composed = await asyncio.to_thread(_compose)
    # A failed compose is NOT a quiet day. Conflating them is why this post went
    # missing from 2026-07-26 to 2026-08-06 while the job recorded a success and
    # "no hook today — skipped" every morning (the composer was truncating on its
    # first tool round). Same lesson as 674b21a6 for the weekly report: silence
    # that was chosen and silence that was forced must not share a code path.
    if isinstance(composed, dict) and composed.get("_error"):
        err = composed["_error"]
        runtime_status.mark_job_failure(
            "daily_clan_insight",
            f"compose failed: {err.get('kind')}: {err.get('detail')}",
        )
        return
    if not composed or not composed.get("post"):
        runtime_status.mark_job_success("daily_clan_insight", "no hook today — skipped")
        return

    final_text = composed["post"]
    topic = composed.get("topic") or "daily"
    result = {
        "event_type": "channel_update",
        "summary": f"ask-elixir daily: {topic}",
        "content": final_text,
    }

    await _post_to_elixir(channel, result)
    ch = _channel_msg_kwargs(channel)
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        final_text,
        summary=result["summary"],
        **ch,
        workflow="ask-elixir",
        event_type="daily_clan_insight",
        raw_json={"result": result, "context_kind": "ask_elixir_daily", "topic": topic},
    )
    runtime_status.mark_job_success("daily_clan_insight", f"posted topic={topic}")


CLAN_CHAT_ACTION_COPY_LIMIT = CLASH_COPY_MAX_LENGTH


LEADER_ACTION_CASE_ORDER = (
    "inactivity_review",
    "demotion_review",
    "promotion_review",
)


async def _weekly_discord_invite_relay():
    """Evergreen housekeeping-nudge emitter (Jamie, 2026-07-06).

    Keeps its original job name for stable scheduling, but no longer posts a
    fixed weekly Discord reminder. It now rotates through the evergreen_nudges
    inventory (Discord, FAQ, website) and — ONLY during a quiet period and within
    a strict rate cap — offers ONE as an in-game-relay leader-action card in
    #actions for a leader to paste. Runs daily; self-gates so it emits
    rarely and fills lulls instead of adding noise. The Discord invite is now
    just inventory item #1.
    """
    from engine import db as engine_db
    from storage import evergreen_nudges as nudges

    runtime_status.mark_job_start("weekly_discord_invite_relay")

    def _pick():
        conn = engine_db.connect()
        try:
            if not nudges.is_quiet_period(conn):
                return None, "not a quiet period"
            item = nudges.due_nudge(conn)
            return (item, None) if item else (None, "no nudge due (cap/cooldown/pending)")
        finally:
            conn.close()

    try:
        item, skip = await asyncio.to_thread(_pick)
        if item is None:
            runtime_status.mark_job_success("weekly_discord_invite_relay", f"skipped: {skip}")
            return
        ok = await _emit_evergreen_nudge_card(item)
        if not ok:
            runtime_status.mark_job_failure("weekly_discord_invite_relay", "nudge card emit failed")
            return
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_discord_invite_relay", str(exc))
        log.warning("evergreen nudge failed: %s", exc, exc_info=True)
        return
    runtime_status.mark_job_success(
        "weekly_discord_invite_relay",
        f"posted nudge: {item['nudge_key']}",
    )


async def _emit_evergreen_nudge_card(item: dict) -> bool:
    """Compose one clan-chat nudge from an inventory item and offer it as an
    in-game-relay leader-action card (mirrors the weekly story relay)."""
    from engine import db as engine_db
    from storage import evergreen_nudges as nudges

    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        log.info("evergreen nudge skipped: actions unavailable")
        return False
    relay_channel = _bot().get_channel(channel_config["id"])
    if not relay_channel:
        log.info("evergreen nudge skipped: actions channel not found")
        return False
    allowed, reason = await asyncio.to_thread(can_post_leader_action, action_type="in_game_relay")
    if not allowed:
        log.info("evergreen nudge skipped by policy: %s", reason)
        return False

    context = (
        "Evergreen clan-chat nudge task:\n"
        "Write ONE short Clash Royale clan-chat message from the note below.\n"
        f"- Plain text only: no markdown, no links, no Discord emoji shortcodes, under {CLAN_CHAT_ACTION_COPY_LIMIT} characters.\n"
        "- Warm and natural, the way a leader would actually say it in clan chat.\n"
        "=== NUDGE ===\n"
        f"{item['context']}"
    )
    forbidden = tuple(item.get("forbidden_terms") or ()) or (
        "http://",
        "https://",
        "www.",
    )
    generated = await generate_clan_chat_copy(
        intent="evergreen_nudge",
        context=context,
        max_messages=1,
        max_chars=CLAN_CHAT_ACTION_COPY_LIMIT,
        forbidden_terms=forbidden,
        metadata={"channel": channel_config["name"], "nudge": item["nudge_key"]},
    )
    copy = generated.messages[0] if generated and generated.messages else ""
    if not copy:
        log.info("evergreen nudge skipped: no usable clan-chat copy")
        return False

    day_key = datetime.now(CHICAGO).strftime("%G-%m-%d")
    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type="in_game_relay",
        target_player_tag=None,
    )
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type="in_game_relay",
        objective="clan_nudge",
        prompt_text=f"Relay into clan chat ({item['topic']}): {copy}",
        rationale=f"Evergreen {item['topic']} nudge, surfaced during a quiet stretch so it fills a lull.",
        target_channel_key="actions",
        target_channel_id=channel_config["id"],
        source_signal_key=f"evergreen_nudge:{item['nudge_key']}:{day_key}",
        source_signal_type="evergreen_nudge",
        copy_original_text=copy,
        copy_current_text=copy,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline=baseline,
    )
    if not action or action.get("source_message_id"):
        return False

    # Mark the item sent NOW (on proposal): the rate cap + rotation count from
    # when it was offered, so we never re-nag even if the leader ignores it.
    def _mark():
        conn = engine_db.connect()
        try:
            nudges.mark_nudge_sent(conn, item["nudge_key"])
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_mark)

    sent_messages = await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    first_message = sent_messages[0] if isinstance(sent_messages, list) and sent_messages else None
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(relay_channel),
        "assistant",
        copy,
        summary=f"Leader action R{action.get('action_id')}: evergreen {item['nudge_key']}",
        channel_id=channel_config["id"],
        channel_name=getattr(relay_channel, "name", "actions"),
        channel_kind=str(getattr(relay_channel, "type", "text")),
        workflow="actions",
        event_type="evergreen_nudge",
        discord_message_id=getattr(first_message, "id", None),
    )
    return True


async def post_release_relay_card(clanchat_text: str, *, tag: str) -> bool:
    """Surface a new release's short clan-chat blurb as an in_game_relay leader-action
    card in #actions, for a leader to paste into the in-game clan chat. Called by
    /clanops release after cut_release.py generates the three tiers; the copy is already
    written (no LLM call here) — we validate length, sign, and post the card."""
    from runtime.clan_chat_copy import sign_clan_chat_text

    text = " ".join((clanchat_text or "").split())
    if not text:
        return False
    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        log.info("release relay skipped: actions unavailable")
        return False
    relay_channel = _bot().get_channel(channel_config["id"])
    if not relay_channel:
        log.info("release relay skipped: actions channel not found")
        return False

    copy = sign_clan_chat_text(text, limit=CLAN_CHAT_ACTION_COPY_LIMIT)
    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type="in_game_relay",
        target_player_tag=None,
    )
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type="in_game_relay",
        objective="release_relay",
        prompt_text=f"Relay the new release into clan chat: {copy}",
        rationale="New Elixir release — short blurb for the in-game clan chat.",
        target_channel_key="actions",
        target_channel_id=channel_config["id"],
        source_signal_key=f"release_relay:{tag}",
        source_signal_type="release_relay",
        copy_original_text=copy,
        copy_current_text=copy,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline=baseline,
    )
    if not action or action.get("source_message_id"):
        return False
    await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    return True


async def _weekly_clan_recap():
    runtime_status.mark_job_start("weekly_clan_recap")
    try:
        recap_channel_id = _get_singleton_channel_id("weekly_recap")
    except Exception as exc:
        runtime_status.mark_job_failure(
            "weekly_clan_recap", f"weekly digest channel config error: {exc}"
        )
        return

    channel = _bot().get_channel(recap_channel_id)
    if not channel:
        runtime_status.mark_job_failure("weekly_clan_recap", "weekly digest channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("Weekly clan recap refresh failed: %s", exc)

    recap_context = await asyncio.to_thread(_build_weekly_clan_recap_context, clan, war)
    recent_posts = await asyncio.to_thread(
        db.list_channel_messages, recap_channel_id, 5, "assistant"
    )
    previous_message = _strip_weekly_recap_header(
        recent_posts[-1]["content"] if recent_posts else ""
    )

    def _compose():
        """Build the awareness read and compose the recap with the brain (rebuilt
        2026-07-11 — the recap is now brain-written, in voice and aware of what it
        posted this week). Runs in a thread; returns recap text or None."""
        try:
            from runtime.awareness import read as awareness_read

            read = awareness_read.build_read()
            return elixir_agent.generate_weekly_recap(read, recap_context, previous_message)
        except Exception:
            log.error("Weekly recap compose failed", exc_info=True)
            return None

    recap_text = await asyncio.to_thread(_compose)
    if not recap_text:
        # FAILURE, not success. The weekly clan report is a standing Monday
        # deliverable, so "the composer returned nothing" is a broken run, not a
        # quiet no-op. Recording it as success is how 2026-08-03 passed unnoticed
        # until Jamie asked where his report was: the job was green, the LLM call
        # was ok=1, and the only evidence was completion_chars=0 in the log.
        # Contrast the legitimate no-ops elsewhere in this file ("no members with
        # a verified email"), where there was genuinely nothing to send.
        runtime_status.mark_job_failure(
            "weekly_clan_recap", "composer returned no recap (nothing was sent)"
        )
        return
    recap_post = _format_weekly_recap_post(recap_text)

    try:
        await _post_to_elixir(channel, {"content": recap_post})
    except discord.Forbidden as exc:
        detail = f"missing Discord permissions in #{getattr(channel, 'name', 'unknown')}"
        runtime_status.mark_job_failure("weekly_clan_recap", detail)
        raise RuntimeError(f"weekly recap post failed: {detail}") from exc
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        recap_post,
        **_channel_msg_kwargs(channel),
        workflow="announcements",
        event_type="weekly_clan_recap",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clan_recap",
        title="Weekly Clan Recap",
        body=recap_post,
        scope="public",
        tags=["weekly", "recap", "clan-history"],
        metadata={"channel_id": channel.id, "workflow": "announcements"},
    )
    # Email the recap to clan members with a verified email. A mail failure must
    # not abort the job — the Discord post already landed — but it must not be
    # INVISIBLE either. Swallowing it here and still calling mark_job_success at
    # the end is how the 2026-08-03 report went missing with a green job status.
    emailed = 0
    email_error: str | None = None
    try:
        emailed = await _email_weekly_recap(recap_text, recap_context)
    except Exception as exc:  # noqa: BLE001 - reported below, not swallowed
        email_error = f"{type(exc).__name__}: {exc}"
        log.warning("weekly recap email failed", exc_info=True)
    # (POAP KINGS website weekly-recap sync + blog post removed 2026-06-21 — the
    # site has its own update script now. The Discord #announcements recap above
    # and the story relay below are unchanged.)
    try:
        await _weekly_story_relay_card(recap_text)
    except Exception:
        log.warning("weekly story relay card failed", exc_info=True)
    if email_error:
        # mark_job_failure raises a deduped #elixir-log alert, which is the whole
        # point: the email is the half of this deliverable nobody sees fail.
        runtime_status.mark_job_failure(
            "weekly_clan_recap",
            f"Discord recap posted, but the email did NOT go out ({email_error})",
        )
        return
    runtime_status.mark_job_success("weekly_clan_recap", f"weekly recap posted; emailed {emailed}")


async def _weekly_elder_standing():
    """Weekly public Elder Standing — the transparent Elder-track report to
    #announcements (holding / rising / stepping-down, from the live band).
    Standalone + grounding-guarded: warms via the LLM, falls back to the exact
    deterministic render if the LLM output ever names someone not in the data."""
    runtime_status.mark_job_start("weekly_elder_standing")
    try:
        channel_id = _get_singleton_channel_id("weekly_recap")  # = #announcements
    except Exception as exc:
        runtime_status.mark_job_failure(
            "weekly_elder_standing", f"announcements channel config error: {exc}"
        )
        return
    channel = _bot().get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("weekly_elder_standing", "announcements channel not found")
        return

    date_str = datetime.now(pytz.timezone("America/Chicago")).strftime("%A, %B %-d")

    def _compose():
        from runtime.elder_standing import compose_elder_standing_report

        try:
            return compose_elder_standing_report(date=date_str)
        except Exception:
            log.error("Elder standing compose failed", exc_info=True)
            return "", "error"

    text, source = await asyncio.to_thread(_compose)
    if not text:
        # Same class as weekly_clan_recap: the composer was asked for a report
        # and produced none. A standing weekly post that silently does not post
        # is a failure, and a green job status is what hides it.
        runtime_status.mark_job_failure(
            "weekly_elder_standing", f"composer returned no report (source={source})"
        )
        return
    try:
        await _post_to_elixir(channel, {"content": text})
    except discord.Forbidden as exc:
        detail = f"missing Discord permissions in #{getattr(channel, 'name', 'unknown')}"
        runtime_status.mark_job_failure("weekly_elder_standing", detail)
        raise RuntimeError(f"elder standing post failed: {detail}") from exc
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        text,
        **_channel_msg_kwargs(channel),
        workflow="elder_standing",
        event_type="weekly_elder_standing",
    )
    runtime_status.mark_job_success(
        "weekly_elder_standing", f"elder standing posted (source={source})"
    )


async def _email_weekly_recap(recap_text: str, email_context: str | None = None) -> int:
    """BCC the weekly recap to clan members with a verified email (BCC so nobody
    sees anyone else's address).

    Takes the RAW recap text, not the Discord post: the email body is rendered as
    email markdown (real headings) rather than a Discord post with its emoji filed
    off. See helpers.format_weekly_recap_email. Returns the recipient count and
    lets send errors propagate — the caller decides what a failure means."""
    from agent.mail import outbound

    if not outbound.enabled():
        return 0
    recipients = await asyncio.to_thread(lambda: [m["email"] for m in db.list_member_emails()])
    if not recipients:
        return 0
    body = None
    if email_context is not None:
        # The email is its OWN composition — expansive, with headings and tables
        # — not the Discord post reformatted. See generate_weekly_recap_email.
        def _compose_email():
            try:
                from runtime.awareness import read as awareness_read

                return elixir_agent.generate_weekly_recap_email(
                    awareness_read.build_read(), email_context, recap_text
                )
            except Exception:
                log.error("Weekly recap email compose failed", exc_info=True)
                return None

        body = await asyncio.to_thread(_compose_email)
    if not body:
        # Fall back to the reformatted Discord post rather than sending nothing.
        # A plainer email still beats a missing one.
        log.warning("weekly recap email: composer returned nothing, using the Discord recap")
        body = format_weekly_recap_email(recap_text)
    email_addr = os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")
    now_ct = datetime.now(CHICAGO)
    subject = f"POAP KINGS — Weekly Clan Recap ({now_ct.strftime('%b %d, %Y')})"
    # One recap per ISO week. A manual re-trigger while debugging used to
    # re-broadcast to everyone; it happened twice on 2026-08-03.
    week_key = "{}-W{:02d}".format(*now_ct.isocalendar()[:2])
    if await asyncio.to_thread(email_dedup.already_sent, "weekly_recap", week_key):
        log.info("weekly recap email already sent for %s; skipping", week_key)
        return 0
    await asyncio.to_thread(
        outbound.send, to=email_addr, bcc=recipients, subject=subject, body=body
    )
    log.info("weekly recap emailed to %d member(s)", len(recipients))
    if not await asyncio.to_thread(
        email_dedup.record_sent,
        "weekly_recap",
        week_key,
        detail=f"{len(recipients)} recipient(s)",
    ):
        # Say it out loud: the next run will re-send rather than skip.
        log.error("weekly recap: sent but NOT recorded for %s — a re-run will duplicate", week_key)
    return len(recipients)


async def _weekly_member_report_cycle():
    """Arena Dispatch — the personalized weekly Clash Royale email, one per member
    with a verified address. Each member gets their OWN report (built from their
    week's battles/badges/cards/profile), narrated in Elixir's voice, and sent
    individually (To:, never BCC — it's about them, and no address leaks).

    Best-effort and isolated: one member's build/LLM/mail failure is logged and
    skipped so it can't sink the batch. Skips cleanly when mail is unconfigured
    or nobody has a verified email."""
    runtime_status.mark_job_start("weekly_member_report")

    from agent.mail import outbound
    from agent.workflows import generate_member_report
    from runtime import member_report

    if not outbound.enabled():
        runtime_status.mark_job_success("weekly_member_report", "skipped: mail not configured")
        return {"sent": 0, "total": 0}

    recipients = await asyncio.to_thread(db.list_member_emails)
    if not recipients:
        runtime_status.mark_job_success("weekly_member_report", "no members with a verified email")
        return {"sent": 0, "total": 0}

    # Per member, per ISO week. Without this a partial re-run re-sends to every
    # member who already succeeded — the isolation that makes one failure
    # survivable also makes a retry duplicate everyone else.
    week_key = "{}-W{:02d}".format(*datetime.now(CHICAGO).isocalendar()[:2])

    def _build_and_send(rec: dict) -> bool:
        tag = rec["player_tag"]
        dedup_key = f"{tag}:{week_key}"
        if email_dedup.already_sent("member_report", dedup_key):
            return False
        name = rec.get("member_name") or tag
        ctx = member_report.build_member_report_context(tag, name)
        narrative = generate_member_report(member_report.facts_for_model(ctx))
        subject, body = member_report.render_member_report(ctx, narrative)
        outbound.send(to=rec["email"], subject=subject, body=body)
        if not email_dedup.record_sent("member_report", dedup_key):
            log.error("arena dispatch: sent to %s but NOT recorded; a re-run will duplicate", tag)
        return True

    sent = 0
    skipped = 0
    for rec in recipients:
        try:
            if await asyncio.to_thread(_build_and_send, rec):
                sent += 1
            else:
                skipped += 1
        except Exception as exc:  # one member's failure never sinks the batch
            log.warning("arena dispatch failed for %s: %s", rec.get("player_tag"), exc)

    total = len(recipients)
    if sent == 0 and skipped == total and total:
        # Everyone was already mailed this week — a no-op re-run, not a failure.
        runtime_status.mark_job_success(
            "weekly_member_report", f"already sent this week ({total} member(s))"
        )
        return {"sent": 0, "total": total, "skipped": skipped}
    if sent == 0:
        runtime_status.mark_job_failure("weekly_member_report", f"0/{total} arena dispatches sent")
    else:
        runtime_status.mark_job_success(
            "weekly_member_report", f"{sent}/{total} arena dispatches sent"
        )
    log.info("arena dispatch: %d/%d member report(s) emailed", sent, total)
    return {"sent": sent, "total": total}


async def _weekly_story_relay_card(recap_text: str) -> bool:
    """Offer the recap's best beat as a clan-chat relay card in #actions.

    Most of the clan never reads Discord — the recap's strongest member
    story reaches them only if a leader pastes it into game chat. One card
    per week, leader-decided; earned frequency learns if these are unwanted.
    """
    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        log.info("weekly story relay skipped: actions unavailable")
        return False
    relay_channel = _bot().get_channel(channel_config["id"])
    if not relay_channel:
        log.info("weekly story relay skipped: actions channel not found")
        return False
    allowed, reason = await asyncio.to_thread(can_post_leader_action, action_type="in_game_relay")
    if not allowed:
        log.info("weekly story relay skipped by policy: %s", reason)
        return False

    context = (
        "Weekly story relay task:\n"
        "Compress the weekly recap below into ONE Clash Royale clan-chat message.\n"
        f"- Plain text only: no markdown, no links, no Discord emoji shortcodes, under {CLAN_CHAT_ACTION_COPY_LIMIT} characters.\n"
        "- Pick the single strongest member story and name the member(s) — recognition is the point.\n"
        "- Write it as something a leader would naturally say in clan chat, not as a broadcast.\n"
        "=== THIS WEEK'S RECAP ===\n"
        f"{recap_text}"
    )
    generated = await generate_clan_chat_copy(
        intent="weekly_story_relay",
        context=context,
        max_messages=1,
        max_chars=CLAN_CHAT_ACTION_COPY_LIMIT,
        forbidden_terms=("http://", "https://", "www.", "Discord"),
        metadata={
            "channel": channel_config["name"],
            "lane": channel_config.get("lane_key") or "actions",
        },
    )
    copy = generated.messages[0] if generated and generated.messages else ""
    if not copy:
        log.info("weekly story relay skipped: no usable clan-chat copy")
        return False

    week_key = datetime.now(CHICAGO).strftime("%G-W%V")
    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type="in_game_relay",
        target_player_tag=None,
    )
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type="in_game_relay",
        objective="clan_story",
        prompt_text=f"Relay this week's story into clan chat: {copy}",
        rationale="Most members never read Discord; the recap's best story reaches them through game chat.",
        target_channel_key="actions",
        target_channel_id=channel_config["id"],
        source_signal_key=f"weekly_story_relay:{week_key}",
        source_signal_type="weekly_story_relay",
        copy_original_text=copy,
        copy_current_text=copy,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline=baseline,
    )
    if not action or action.get("source_message_id"):
        return False
    sent_messages = await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    if not isinstance(sent_messages, list):
        sent_messages = []
    first_message = sent_messages[0] if sent_messages else None
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(relay_channel),
        "assistant",
        copy,
        summary=f"Leader action R{action.get('action_id')}: weekly story relay",
        channel_id=channel_config["id"],
        channel_name=getattr(relay_channel, "name", "actions"),
        channel_kind=str(getattr(relay_channel, "type", "text")),
        workflow="actions",
        event_type="weekly_story_relay",
        discord_message_id=getattr(first_message, "id", None),
        raw_json={"leader_action": action},
    )
    return True
