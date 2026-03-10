import asyncio
import json
import os
from datetime import datetime, timezone

import cr_api
import db
import elixir_agent
import heartbeat
import prompts
import site_content
from runtime import app as _app
from runtime.app import (
    CHICAGO,
    bot,
    log,
)
from runtime.helpers import _channel_scope, _get_singleton_channel_id, _with_leader_ping
from runtime import status as runtime_status


async def _post_to_elixir(*args, **kwargs):
    return await _app._post_to_elixir(*args, **kwargs)


async def _load_live_clan_context(*args, **kwargs):
    return await _app._load_live_clan_context(*args, **kwargs)


def _build_weekly_clanops_review(*args, **kwargs):
    return _app._build_weekly_clanops_review(*args, **kwargs)


def _relayworthy_war_signals(signals):
    relay_signal_types = {
        "war_final_practice_day",
        "war_final_battle_day",
        "war_week_rollover",
        "war_season_rollover",
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


async def _promotion_content_cycle():
    runtime_status.mark_job_start("promotion_content_cycle")
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

    roster_data = await asyncio.to_thread(site_content.build_roster_data, clan, True)
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
        await asyncio.to_thread(site_content.write_content, "promote", promote)
        await asyncio.to_thread(site_content.commit_and_push, "Elixir promotion content update")
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

    announcements_channel_id = _get_singleton_channel_id("announcements")
    channel = bot.get_channel(announcements_channel_id)
    if not channel:
        log.error("Announcements channel %s not found", announcements_channel_id)
        runtime_status.mark_job_failure("heartbeat", "announcements channel not found")
        return

    try:
        # Run the heartbeat tick — fetches data, snapshots, detects signals
        tick_result = heartbeat.tick()
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
                    await channel.send(msg)
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_join_broadcast",
                    )
            elif sig["type"] == "member_leave":
                msg = await asyncio.to_thread(
                    elixir_agent.generate_message,
                    "member_leave_broadcast",
                    f"Member '{sig['name']}' (tag: {sig['tag']}) has left the clan. "
                    f"Write a brief farewell for the broadcast channel.",
                    recent_posts,
                )
                if msg:
                    await channel.send(msg)
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_leave_broadcast",
                    )
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft a post
        if other_signals:
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post, clan, war,
                other_signals, recent_posts, channel_memory,
            )
            if result is None:
                log.info("Heartbeat: LLM decided signals not worth posting")
            else:
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
                for sig in other_signals:
                    if sig.get("signal_key"):
                        await asyncio.to_thread(
                            db.mark_system_signal_announced,
                            sig["signal_key"],
                        )
                log.info("Posted observation: %s", result.get("summary"))

            await _maybe_post_arena_relay(other_signals, clan, war)

        runtime_status.mark_job_success("heartbeat", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("heartbeat", str(e))


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "18"))       # 6pm Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "18"))  # 6pm Chicago
PLAYER_INTEL_REFRESH_HOURS = int(os.getenv("PLAYER_INTEL_REFRESH_HOURS", "6"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "12"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "6"))
PLAYER_INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("PLAYER_INTEL_REQUEST_SPACING_SECONDS", "2.0"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))


async def _site_data_refresh():
    """On-demand site data refresh — refresh clan data and roster on poapkings.com."""
    runtime_status.mark_job_start("site_data_refresh")
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

        roster_data = site_content.build_roster_data(clan)
        site_content.write_content("roster", roster_data)

        clan_stats = site_content.build_clan_data(clan)
        site_content.write_content("clan", clan_stats)

        site_content.commit_and_push("Elixir data refresh")
        log.info("Site data refresh complete: %d members", len(roster_data.get("members", [])))
        runtime_status.mark_job_success("site_data_refresh", f"{len(roster_data.get('members', []))} members")
    except Exception as e:
        log.error("Site data refresh error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_data_refresh", str(e))


async def _site_content_cycle():
    """Daily site publish — refresh data, generate content, and push updates."""
    runtime_status.mark_job_start("site_content_cycle")
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
        if clan.get("memberList"):
            roster_data = site_content.build_roster_data(clan, include_cards=True)
            clan_stats = site_content.build_clan_data(clan)

            # Generate roster bios and merge
            try:
                bios = elixir_agent.generate_roster_bios(clan, war, roster_data=roster_data)
                if bios:
                    roster_data["intro"] = bios.get("intro", "")
                    member_bios = bios.get("members", {})
                    db.upsert_member_generated_profiles(member_bios)
                    for m in roster_data["members"]:
                        mc = member_bios.get(m["tag"], {}) or member_bios.get("#" + m["tag"], {})
                        if mc:
                            m["bio"] = mc.get("bio", "")
                            m["highlight"] = mc.get("highlight", "general")
            except Exception as e:
                log.error("Roster bio generation error: %s", e)

            site_content.write_content("roster", roster_data)
            site_content.write_content("clan", clan_stats)

        # Generate home message
        try:
            prev_home = site_content.load_current("home")
            prev_msg = prev_home.get("message", "") if prev_home else ""
            home_text = elixir_agent.generate_home_message(clan, war, prev_msg, roster_data=roster_data)
            if home_text:
                site_content.write_content("home", {
                    "message": home_text,
                    "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
        except Exception as e:
            log.error("Home message error: %s", e)

        # Generate members message
        try:
            prev_members = site_content.load_current("members")
            prev_msg = prev_members.get("message", "") if prev_members else ""
            members_text = elixir_agent.generate_members_message(clan, war, prev_msg, roster_data=roster_data)
            if members_text:
                site_content.write_content("members", {
                    "message": members_text,
                    "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
        except Exception as e:
            log.error("Members message error: %s", e)

        site_content.commit_and_push("Elixir content update")
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
                if profile_signals:
                    progression_signals.extend(profile_signals)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log:
                await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
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
    runtime_status.mark_job_success("clanops_weekly_review", "weekly review posted")


# ── Bot events ────────────────────────────────────────────────────────────────

__all__ = [
    name for name in globals()
    if not name.startswith("__") and name not in {"_post_to_elixir", "_load_live_clan_context", "_build_weekly_clanops_review"}
]
