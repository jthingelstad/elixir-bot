import asyncio
import json
import os
import re
from datetime import datetime, timezone

import discord
import cr_api
import db
import elixir_agent
import heartbeat
import prompts
from integrations.poap_kings import site as poap_kings_site
from storage.contextual_memory import upsert_war_recap_memory, upsert_weekly_summary_memory
from runtime import app as _app
from runtime.app import (
    CHICAGO,
    bot,
    log,
)
from runtime.helpers import _channel_scope, _get_singleton_channel_id, _with_leader_ping
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals

_WEEKLY_RECAP_HEADER_RE = re.compile(r"^\s*[*#_`\s]*weekly recap\b", re.IGNORECASE)
_PROMOTION_DISCORD_REQUIRED_TEXT = "Required Trophies: [2000]"
_PROMOTION_REDDIT_REQUIRED_TOKEN = "[2000]"


async def _post_to_elixir(*args, **kwargs):
    return await _app._post_to_elixir(*args, **kwargs)


async def _load_live_clan_context(*args, **kwargs):
    return await _app._load_live_clan_context(*args, **kwargs)


def _build_weekly_clanops_review(*args, **kwargs):
    return _app._build_weekly_clanops_review(*args, **kwargs)


def _build_weekly_clan_recap_context(*args, **kwargs):
    return _app._build_weekly_clan_recap_context(*args, **kwargs)


def _strip_weekly_recap_header(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    lines = body.splitlines()
    if lines and _WEEKLY_RECAP_HEADER_RE.match(lines[0] or ""):
        lines = lines[1:]
        while lines and not (lines[0] or "").strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


def _format_weekly_recap_post(recap_text: str, *, now: datetime | None = None) -> str:
    body = _strip_weekly_recap_header(recap_text)
    current = (now or datetime.now(timezone.utc)).astimezone(CHICAGO)
    title = f"**Weekly Recap | {current.strftime('%B')} {current.day}, {current.year}**"
    if not body:
        return title
    return f"{title}\n\n{body}"


def _relayworthy_war_signals(signals):
    relay_signal_types = {
        "war_battle_day_started",
        "war_battle_day_live_update",
        "war_battle_rank_change",
        "war_battle_day_final_hours",
        "war_battle_day_complete",
        "war_final_practice_day",
        "war_final_battle_day",
        "war_practice_day_started",
        "war_practice_day_complete",
        "war_week_rollover",
        "war_season_rollover",
        "war_week_complete",
        "war_season_complete",
    }
    relayworthy = []
    for signal in signals or []:
        signal_type = signal.get("type")
        if signal_type in relay_signal_types:
            relayworthy.append(signal)
            continue
        if signal_type == "war_completed" and (signal.get("won") or signal.get("our_rank") == 1):
            relayworthy.append(signal)
    return relayworthy


def _observation_signal_batches(signals):
    if not signals:
        return []
    grouped = []
    completion_batch = []
    batches = []
    completion_signal_types = {
        "war_completed",
        "war_week_complete",
        "war_champ_standings",
    }
    for signal in signals:
        signal_type = signal.get("type") or ""
        if signal_type.startswith("war_"):
            if signal_type in completion_signal_types:
                completion_batch.append(signal)
                continue
            batches.append([signal])
        else:
            grouped.append(signal)
    if grouped:
        batches.insert(0, grouped)
    if completion_batch:
        batches.append(completion_batch)
    return batches


def _system_signal_updates(signals):
    return [signal for signal in (signals or []) if signal.get("signal_key")]


def _store_recap_memories_for_signal_batch(signal_batch, posts, channel_id):
    body = "\n\n".join((post or "").strip() for post in (posts or []) if (post or "").strip())
    if not body:
        return None
    return upsert_war_recap_memory(
        signals=signal_batch,
        body=body,
        channel_id=channel_id,
        workflow="observation",
    )


def _build_system_signal_context(signal, channel_name):
    payload = signal.get("payload") or {}
    details = payload.get("details") or []
    lines = [
        "This is a standalone clan-wide system update about a new Elixir capability.",
        f"Post it for {channel_name}.",
        "Write exactly one Discord message. Do not split it into parts or a series.",
        "Write the full final Discord message yourself, including the subject line.",
        "The first line MUST be a bolded subject line.",
        "Include an Elixir custom emoji in that subject line using :emoji_name: shortcode syntax.",
        "Do not restate the subject line or title again immediately after the first line.",
        "Do not mention hidden system mechanics or call it a system signal.",
        "Make it feel like a self-contained clan update from Elixir.",
        "",
        f"signal_type: {signal.get('type') or 'unknown'}",
        f"signal_key: {signal.get('signal_key') or 'unknown'}",
        f"title: {payload.get('title') or signal.get('title') or ''}",
        f"message: {payload.get('message') or signal.get('message') or ''}",
        f"audience: {payload.get('audience') or 'clan'}",
        f"capability_area: {payload.get('capability_area') or 'general'}",
    ]
    if details:
        lines.append("details:")
        lines.extend(f"- {detail}" for detail in details)
    return "\n".join(lines)


async def _post_system_signal_updates(signals, clan, war):
    system_signals = _system_signal_updates(signals)
    if not system_signals:
        return

    channel_id = _get_singleton_channel_id("weekly_digest")
    channel = bot.get_channel(channel_id)
    if not channel:
        raise RuntimeError("weekly digest channel not found for system signal updates")

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages, channel_id, 10, "assistant",
    )

    for signal in system_signals:
        context = _build_system_signal_context(signal, f"#{getattr(channel, 'name', 'announcements')}")
        message = await asyncio.to_thread(
            elixir_agent.generate_message,
            "system_signal_broadcast",
            context,
            recent_posts,
        )
        message = (message or "").strip()
        if not message:
            continue
        await _post_to_elixir(channel, {"content": message})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel), "assistant", message,
            summary=(signal.get("payload") or {}).get("title") or signal.get("title"),
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="observation",
            event_type=signal.get("type") or "system_signal",
        )
        if signal.get("signal_key"):
            await asyncio.to_thread(
                db.mark_system_signal_announced,
                signal["signal_key"],
            )
        recent_posts = [*recent_posts, {"content": message}][-10:]


async def _publish_pending_system_signal_updates(*, seed_startup_signals: bool = False) -> int:
    if seed_startup_signals:
        await asyncio.to_thread(queue_startup_system_signals)
    pending = await asyncio.to_thread(db.list_pending_system_signals)
    if not pending:
        return 0
    await _post_system_signal_updates(pending, {}, {})
    return len(pending)


async def _maybe_post_arena_relay(signals, clan, war):
    relayworthy = _relayworthy_war_signals(signals)
    if not relayworthy:
        return

    try:
        arena_relay_channel_id = _get_singleton_channel_id("arena_relay")
    except Exception as exc:
        log.warning("Arena relay post skipped: %s", exc)
        return

    channel = bot.get_channel(arena_relay_channel_id)
    if not channel:
        log.warning("Arena relay channel %s not found", arena_relay_channel_id)
        return

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages, arena_relay_channel_id, 10, "assistant",
    )
    relay_context = (
        "Write a short relay-ready message for #arena-relay.\n"
        "A clan leader may copy this into in-game Clan Chat.\n"
        "Keep it short and punchy: 1-3 short sentences.\n"
        "Focus on exactly what the clan should know or do right now.\n"
        "Do not mention Discord, channels, reactions, or private leadership context.\n"
        "Identify yourself naturally as Elixir.\n\n"
        f"Relevant signals:\n{json.dumps(relayworthy, indent=2, default=str)}\n\n"
        f"Current war data:\n{json.dumps(war or {}, indent=2, default=str)}\n\n"
        f"Current clan data:\n{json.dumps({'name': clan.get('name'), 'tag': clan.get('tag')}, indent=2, default=str)}"
    )

    message = await asyncio.to_thread(
        elixir_agent.generate_message,
        "arena_relay_auto",
        relay_context,
        recent_posts,
    )
    if not message:
        return

    relay_post = _with_leader_ping(message)
    await _post_to_elixir(channel, {"content": relay_post})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        relay_post,
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="observation",
        event_type="arena_relay_auto",
    )


def _promotion_channel_posts(promote):
    posts = []
    discord_body = (((promote or {}).get("discord") or {}).get("body") or "").strip()
    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()

    if discord_body:
        posts.append(
            "**Discord recruiting copy**\n"
            f"```text\n{discord_body}\n```"
        )
    if reddit_title or reddit_body:
        reddit_lines = ["**Reddit recruiting copy**"]
        if reddit_title:
            reddit_lines.append(f"Title: `{reddit_title}`")
        if reddit_body:
            reddit_lines.append(f"```text\n{reddit_body}\n```")
        posts.append("\n".join(reddit_lines))
    return posts


def _unwrap_outer_bold(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    return stripped


def _validate_promote_content_or_raise(promote) -> None:
    discord = (promote or {}).get("discord") or {}
    discord_body = (discord.get("body") or "").strip()
    if discord_body:
        first_line = next((line.strip() for line in discord_body.splitlines() if line.strip()), "")
        first_line = _unwrap_outer_bold(first_line)
        if _PROMOTION_DISCORD_REQUIRED_TEXT not in first_line:
            raise ValueError(
                f"discord.body first line must include exact text `{_PROMOTION_DISCORD_REQUIRED_TEXT}`"
            )
        if not first_line.endswith(_PROMOTION_DISCORD_REQUIRED_TEXT):
            raise ValueError(
                f"discord.body first line must end with exact text `{_PROMOTION_DISCORD_REQUIRED_TEXT}`"
            )

    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()
    if (reddit_title or reddit_body) and _PROMOTION_REDDIT_REQUIRED_TOKEN not in reddit_title:
        raise ValueError(
            f"reddit.title must include exact token `{_PROMOTION_REDDIT_REQUIRED_TOKEN}`"
        )


def _write_site_content_or_raise(content_type: str, data) -> None:
    if not poap_kings_site.write_content(content_type, data):
        raise RuntimeError(f"{content_type} content write failed")


def _commit_site_content_or_raise(message: str) -> None:
    if not poap_kings_site.commit_and_push(message):
        raise RuntimeError("site publish failed")


def _publish_poap_kings_site_or_raise(payloads: dict[str, object], message: str) -> bool:
    return poap_kings_site.publish_site_content(payloads, message)


def _mark_delivered_signals(signals, *, today: str | None = None):
    signal_date = today or db.chicago_today()
    for signal in signals or []:
        if signal.get("signal_key"):
            continue
        signal_type = signal.get("signal_log_type") or signal.get("type")
        if signal_type:
            db.mark_signal_sent(signal_type, signal_date)
        if signal.get("type") == "clan_birthday":
            db.mark_announcement_sent(signal_date, "clan_birthday", None)
        elif signal.get("type") == "join_anniversary":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "join_anniversary", tag)
        elif signal.get("type") == "member_birthday":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "birthday", tag)


async def _promotion_content_cycle():
    runtime_status.mark_job_start("promotion_content_cycle")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("promotion_content_cycle", "POAP KINGS site integration disabled")
        return
    try:
        promotion_channel_id = _get_singleton_channel_id("promotion")
    except Exception as exc:
        runtime_status.mark_job_failure("promotion_content_cycle", f"promotion channel config error: {exc}")
        return

    channel = bot.get_channel(promotion_channel_id)
    if not channel:
        runtime_status.mark_job_failure("promotion_content_cycle", "promotion channel not found")
        return

    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.error("Promotion content refresh failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"refresh failed: {exc}")
        return

    if not clan.get("memberList"):
        runtime_status.mark_job_success("promotion_content_cycle", "no member data")
        return

    roster_data = await asyncio.to_thread(poap_kings_site.build_roster_data, clan, True)
    promote = await asyncio.to_thread(
        elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=roster_data,
    )
    if not promote:
        runtime_status.mark_job_success("promotion_content_cycle", "no promotion content")
        return
    try:
        _validate_promote_content_or_raise(promote)
    except Exception as exc:
        log.error("Promotion content validation failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"invalid promotion content: {exc}")
        return

    try:
        await asyncio.to_thread(
            _publish_poap_kings_site_or_raise,
            {"promote": promote},
            "Elixir POAP KINGS promotion content update",
        )
    except Exception as exc:
        log.error("Promotion content publish error: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"site publish failed: {exc}")
        return

    channel_posts = _promotion_channel_posts(promote)
    if not channel_posts:
        runtime_status.mark_job_success("promotion_content_cycle", "website updated, no promotion channel copy")
        return

    await _post_to_elixir(channel, {"content": channel_posts})
    for index, post in enumerate(channel_posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            post,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="promotion",
            event_type="promotion_content_cycle" if index == 0 else "promotion_content_cycle_part",
        )
    runtime_status.mark_job_success("promotion_content_cycle", "website and Discord promotion content published")

async def _heartbeat_tick():
    """Scheduled heartbeat — fetch data, detect signals, post if interesting."""
    runtime_status.mark_job_start("heartbeat")
    # Check active hours
    now_chicago = datetime.now(CHICAGO)
    if not (_app.HEARTBEAT_START_HOUR <= now_chicago.hour < _app.HEARTBEAT_END_HOUR):
        log.info("Heartbeat: outside active hours (%d:%02d), skipping",
                 now_chicago.hour, now_chicago.minute)
        runtime_status.mark_job_success("heartbeat", "skipped outside active hours")
        return

    try:
        await asyncio.to_thread(queue_startup_system_signals)

        # Run the heartbeat tick — fetches data, snapshots, detects signals
        tick_result = heartbeat.tick(include_war=False)
        if tick_result.clan.get("memberList"):
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            await _app._maybe_alert_cr_api_failure("heartbeat")
        signals = tick_result.signals

        if not signals:
            log.info("Heartbeat: no signals, nothing to post")
            runtime_status.mark_job_success("heartbeat", "no signals")
            return

        log.info("Heartbeat: %d signals detected, consulting LLM", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        await _post_system_signal_updates(signals, clan, war)
        signals = [signal for signal in signals if not signal.get("signal_key")]

        if not signals:
            runtime_status.mark_job_success("heartbeat", "only system signals processed")
            return

        announcements_channel_id = _get_singleton_channel_id("announcements")
        channel = bot.get_channel(announcements_channel_id)
        if not channel:
            log.error("Announcements channel %s not found", announcements_channel_id)
            runtime_status.mark_job_failure("heartbeat", "announcements channel not found")
            return

        # Fetch recent announcements-channel post history to avoid repetition
        recent_posts = await asyncio.to_thread(
            db.list_channel_messages, announcements_channel_id, 20, "assistant",
        )
        channel_memory = await asyncio.to_thread(
            db.build_memory_context,
            channel_id=announcements_channel_id,
        )

        # Handle join/leave signals via LLM
        other_signals = []
        for sig in signals:
            if sig["type"] == "member_join":
                msg = await asyncio.to_thread(
                    elixir_agent.generate_message,
                    "member_join_broadcast",
                    f"New member '{sig['name']}' (tag: {sig['tag']}) just joined the clan. "
                    f"Write a welcome announcement for the broadcast channel.",
                    recent_posts,
                )
                if msg:
                    await _post_to_elixir(channel, {"content": msg})
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_join_broadcast",
                    )
                    await asyncio.to_thread(_mark_delivered_signals, [sig])
            elif sig["type"] == "member_leave":
                msg = await asyncio.to_thread(
                    elixir_agent.generate_message,
                    "member_leave_broadcast",
                    f"Member '{sig['name']}' (tag: {sig['tag']}) has left the clan. "
                    f"Write a brief farewell for the broadcast channel.",
                    recent_posts,
                )
                if msg:
                    await _post_to_elixir(channel, {"content": msg})
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_leave_broadcast",
                    )
                    await asyncio.to_thread(_mark_delivered_signals, [sig])
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft posts
        if other_signals:
            for signal_batch in _observation_signal_batches(other_signals):
                result = await asyncio.to_thread(
                    elixir_agent.observe_and_post, clan, war,
                    signal_batch, recent_posts, channel_memory,
                )
                if result is None:
                    log.info("Heartbeat: LLM decided signal batch not worth posting")
                    continue
                result = await _app._apply_member_refs_to_result(result)
                await _post_to_elixir(channel, result)
                posts = _app._entry_posts(result)
                if posts:
                    summary = result.get("summary")
                    event_type = result.get("event_type")
                    for index, post in enumerate(posts):
                        post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
                        post_event_type = event_type if index == 0 else f"{event_type}_part" if event_type else None
                        await asyncio.to_thread(
                            db.save_message,
                            _channel_scope(channel), "assistant", post,
                            summary=post_summary,
                            channel_id=channel.id,
                            channel_name=getattr(channel, "name", None),
                            channel_kind=str(channel.type),
                            workflow="observation",
                            event_type=post_event_type,
                        )
                    await asyncio.to_thread(_mark_delivered_signals, signal_batch)
                    await asyncio.to_thread(
                        _store_recap_memories_for_signal_batch,
                        signal_batch,
                        posts,
                        channel.id,
                    )
                    recent_posts = [*recent_posts, *({"content": post} for post in posts)][-20:]
                log.info("Posted observation: %s", result.get("summary"))

                await _maybe_post_arena_relay(signal_batch, clan, war)

        runtime_status.mark_job_success("heartbeat", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("heartbeat", str(e))


async def _war_awareness_tick():
    """Dedicated 24/7 war observer so day rollovers and recaps are not tied to Chicago daytime."""
    runtime_status.mark_job_start("war_awareness")
    try:
        tick_result = heartbeat.tick(include_nonwar=False, include_war=True)
        if tick_result.clan.get("memberList"):
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            await _app._maybe_alert_cr_api_failure("war awareness")
        signals = tick_result.signals

        if not signals:
            runtime_status.mark_job_success("war_awareness", "no war signals")
            return

        clan = tick_result.clan
        war = tick_result.war

        announcements_channel_id = _get_singleton_channel_id("announcements")
        channel = bot.get_channel(announcements_channel_id)
        if not channel:
            runtime_status.mark_job_failure("war_awareness", "announcements channel not found")
            return

        recent_posts = await asyncio.to_thread(
            db.list_channel_messages, announcements_channel_id, 20, "assistant",
        )
        channel_memory = await asyncio.to_thread(
            db.build_memory_context,
            channel_id=announcements_channel_id,
        )

        for signal_batch in _observation_signal_batches(signals):
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post, clan, war,
                signal_batch, recent_posts, channel_memory,
            )
            if result is None:
                log.info("War awareness: LLM decided signal batch not worth posting")
                continue
            result = await _app._apply_member_refs_to_result(result)
            await _post_to_elixir(channel, result)
            posts = _app._entry_posts(result)
            if posts:
                summary = result.get("summary")
                event_type = result.get("event_type")
                for index, post in enumerate(posts):
                    post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
                    post_event_type = event_type if index == 0 else f"{event_type}_part" if event_type else None
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", post,
                        summary=post_summary,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type=post_event_type,
                    )
                await asyncio.to_thread(_mark_delivered_signals, signal_batch)
                await asyncio.to_thread(
                    _store_recap_memories_for_signal_batch,
                    signal_batch,
                    posts,
                    channel.id,
                )
                recent_posts = [*recent_posts, *({"content": post} for post in posts)][-20:]

            await _maybe_post_arena_relay(signal_batch, clan, war)

        runtime_status.mark_job_success("war_awareness", f"{len(signals)} war signal(s) processed")
    except Exception as e:
        log.error("War awareness error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("war_awareness", str(e))


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "18"))       # 6pm Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "18"))  # 6pm Chicago


def _player_intel_refresh_minutes() -> int:
    minutes = os.getenv("PLAYER_INTEL_REFRESH_MINUTES")
    if minutes:
        return max(1, int(minutes))
    legacy_hours = os.getenv("PLAYER_INTEL_REFRESH_HOURS")
    if legacy_hours:
        return max(1, int(float(legacy_hours) * 60))
    return 30


PLAYER_INTEL_REFRESH_MINUTES = _player_intel_refresh_minutes()
PLAYER_INTEL_REFRESH_HOURS = PLAYER_INTEL_REFRESH_MINUTES / 60
WAR_AWARENESS_INTERVAL_MINUTES = int(os.getenv("WAR_AWARENESS_INTERVAL_MINUTES", "15"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "5"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "1"))
PLAYER_INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("PLAYER_INTEL_REQUEST_SPACING_SECONDS", "2.0"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))
WEEKLY_RECAP_DAY = os.getenv("WEEKLY_RECAP_DAY", "mon")
WEEKLY_RECAP_HOUR = int(os.getenv("WEEKLY_RECAP_HOUR", "9"))


async def _site_data_refresh():
    """On-demand site data refresh — refresh clan data and roster on poapkings.com."""
    runtime_status.mark_job_start("site_data_refresh")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("site_data_refresh", "POAP KINGS site integration disabled")
        return
    try:
        try:
            clan = cr_api.get_clan()
            _app._clear_cr_api_failure_alert_if_recovered()
        except Exception:
            log.error("Site data refresh: CR API failed")
            await _app._maybe_alert_cr_api_failure("site data refresh")
            clan = {}

        if not clan.get("memberList"):
            log.info("Site data refresh: no member data, skipping")
            runtime_status.mark_job_success("site_data_refresh", "no member data")
            return

        roster_data = poap_kings_site.build_roster_data(clan)
        clan_stats = poap_kings_site.build_clan_data(clan)
        await asyncio.to_thread(
            _publish_poap_kings_site_or_raise,
            {"roster": roster_data, "clan": clan_stats},
            "Elixir POAP KINGS site data refresh",
        )
        log.info("Site data refresh complete: %d members", len(roster_data.get("members", [])))
        runtime_status.mark_job_success("site_data_refresh", f"{len(roster_data.get('members', []))} members")
    except Exception as e:
        log.error("Site data refresh error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_data_refresh", str(e))


async def _site_content_cycle():
    """Daily site publish — refresh data, generate content, and push updates."""
    runtime_status.mark_job_start("site_content_cycle")
    if not poap_kings_site.site_enabled():
        runtime_status.mark_job_success("site_content_cycle", "POAP KINGS site integration disabled")
        return
    try:
        try:
            clan = cr_api.get_clan()
            _app._clear_cr_api_failure_alert_if_recovered()
        except Exception:
            await _app._maybe_alert_cr_api_failure("site content cycle")
            clan = {}
        try:
            war = cr_api.get_current_war()
        except Exception:
            await _app._maybe_alert_cr_api_failure("site content war refresh")
            war = {}

        # Build and write data (second daily refresh)
        roster_data = None
        payloads = {}
        if clan.get("memberList"):
            roster_data = poap_kings_site.build_roster_data(clan, include_cards=True)
            clan_stats = poap_kings_site.build_clan_data(clan)
            payloads["roster"] = roster_data
            payloads["clan"] = clan_stats

        # Generate home message
        try:
            prev_home = poap_kings_site.load_published("home") or poap_kings_site.load_current("home")
            prev_msg = prev_home.get("message", "") if prev_home else ""
            home_text = elixir_agent.generate_home_message(clan, war, prev_msg, roster_data=roster_data)
        except Exception as e:
            log.error("Home message error: %s", e)
            home_text = None
        if home_text:
            payloads["home"] = {
                "message": home_text,
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        if payloads:
            await asyncio.to_thread(
                _publish_poap_kings_site_or_raise,
                payloads,
                "Elixir POAP KINGS daily site sync",
            )
        log.info("Site content cycle complete")
        runtime_status.mark_job_success("site_content_cycle", "content updated")
    except Exception as e:
        log.error("Site content cycle error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_content_cycle", str(e))


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
        _app._clear_cr_api_failure_alert_if_recovered()
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        await _app._maybe_alert_cr_api_failure("player intel refresh")
        runtime_status.mark_job_failure("player_intel_refresh", f"clan fetch failed: {e}")
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        runtime_status.mark_job_success("player_intel_refresh", "no member data")
        return

    await asyncio.to_thread(db.snapshot_members, members)
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
        if war:
            await asyncio.to_thread(db.upsert_war_current_state, war)
    except Exception:
        await _app._maybe_alert_cr_api_failure("player intel war refresh")
        war = {}

    targets = await asyncio.to_thread(
        db.get_player_intel_refresh_targets,
        PLAYER_INTEL_BATCH_SIZE,
        PLAYER_INTEL_STALE_HOURS,
    )
    if not targets:
        log.info("Player intel refresh: no stale targets")
        runtime_status.mark_job_success("player_intel_refresh", "no stale targets")
        return

    refreshed = 0
    progression_signals = []
    for target in targets:
        tag = target["tag"]
        try:
            profile = await asyncio.to_thread(cr_api.get_player, tag)
            if profile:
                profile_signals = await asyncio.to_thread(db.snapshot_player_profile, profile)
                if isinstance(profile_signals, list) and profile_signals:
                    progression_signals.extend(profile_signals)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log:
                battle_signals = await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
                if isinstance(battle_signals, list) and battle_signals:
                    progression_signals.extend(battle_signals)
            refreshed += 1
            await asyncio.sleep(PLAYER_INTEL_REQUEST_SPACING_SECONDS)
        except Exception as e:
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    if progression_signals:
        announcements_channel_id = _get_singleton_channel_id("announcements")
        channel = bot.get_channel(announcements_channel_id)
        if channel:
            recent_posts = await asyncio.to_thread(
                db.list_channel_messages, announcements_channel_id, 20, "assistant",
            )
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post,
                clan,
                war,
                progression_signals,
                recent_posts,
                await asyncio.to_thread(
                    db.build_memory_context,
                    channel_id=announcements_channel_id,
                ),
            )
            if result:
                result = await _app._apply_member_refs_to_result(result)
                await _post_to_elixir(channel, result)
                posts = _app._entry_posts(result)
                if posts:
                    summary = result.get("summary")
                    event_type = result.get("event_type")
                    for index, post in enumerate(posts):
                        post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
                        post_event_type = event_type if index == 0 else f"{event_type}_part" if event_type else None
                        await asyncio.to_thread(
                            db.save_message,
                            _channel_scope(channel), "assistant", post,
                            summary=post_summary,
                            channel_id=channel.id,
                            channel_name=getattr(channel, "name", None),
                            channel_kind=str(channel.type),
                            workflow="observation",
                            event_type=post_event_type,
                        )

    log.info("Player intel refresh complete: refreshed %d members", refreshed)
    runtime_status.mark_job_success("player_intel_refresh", f"refreshed {refreshed} member(s)")


async def _clanops_weekly_review():
    runtime_status.mark_job_start("clanops_weekly_review")
    clanops_channels = prompts.discord_channels_by_role("clanops")
    if not clanops_channels:
        runtime_status.mark_job_failure("clanops_weekly_review", "no clanops channel configured")
        return

    target_config = clanops_channels[0]
    channel = bot.get_channel(target_config["id"])
    if not channel:
        runtime_status.mark_job_failure("clanops_weekly_review", "clanops channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("ClanOps weekly review refresh failed: %s", exc)

    review_content = await asyncio.to_thread(_build_weekly_clanops_review, clan, war)
    if not review_content:
        runtime_status.mark_job_success("clanops_weekly_review", "no review content")
        return

    await _post_to_elixir(channel, {"content": review_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        review_content,
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="clanops",
        event_type="weekly_clanops_review",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clanops_review",
        title="Weekly ClanOps Review",
        body=review_content,
        scope="leadership",
        tags=["weekly", "clanops", "review"],
        metadata={"channel_id": channel.id, "workflow": "clanops"},
    )
    runtime_status.mark_job_success("clanops_weekly_review", "weekly review posted")


async def _weekly_clan_recap():
    runtime_status.mark_job_start("weekly_clan_recap")
    try:
        recap_channel_id = _get_singleton_channel_id("weekly_digest")
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_clan_recap", f"weekly digest channel config error: {exc}")
        return

    channel = bot.get_channel(recap_channel_id)
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
    recent_posts = await asyncio.to_thread(db.list_channel_messages, recap_channel_id, 5, "assistant")
    previous_message = _strip_weekly_recap_header(recent_posts[-1]["content"] if recent_posts else "")
    recap_text = await asyncio.to_thread(
        elixir_agent.generate_weekly_digest,
        recap_context,
        previous_message,
    )
    if not recap_text:
        runtime_status.mark_job_success("weekly_clan_recap", "no recap generated")
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
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="observation",
        event_type="weekly_clan_recap",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clan_recap",
        title="Weekly Clan Recap",
        body=recap_post,
        scope="public",
        tags=["weekly", "recap", "clan-history"],
        metadata={"channel_id": channel.id, "workflow": "observation"},
    )
    if poap_kings_site.site_enabled():
        try:
            await asyncio.to_thread(
                _publish_poap_kings_site_or_raise,
                {
                    "members": {
                        "title": "Weekly Recap",
                        "message": recap_text,
                        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": "weekly_clan_recap",
                    }
                },
                "Elixir POAP KINGS weekly recap sync",
            )
        except Exception as exc:
            log.error("Weekly recap site sync failed: %s", exc, exc_info=True)
            runtime_status.mark_job_failure("weekly_clan_recap", f"site sync failed: {exc}")
            return
    runtime_status.mark_job_success("weekly_clan_recap", "weekly recap posted")


# ── Bot events ────────────────────────────────────────────────────────────────

__all__ = [
    name for name in globals()
    if not name.startswith("__") and name not in {"_post_to_elixir", "_load_live_clan_context", "_build_weekly_clanops_review", "_build_weekly_clan_recap_context"}
]
