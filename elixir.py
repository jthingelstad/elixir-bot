"""Elixir - POAP KINGS Discord bot (LLM-powered with heartbeat)."""

import asyncio
import atexit
import os
import signal
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api
import db
import site_content
import elixir_agent
import heartbeat
import prompts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))
CLANOPS_PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("CLANOPS_PROACTIVE_COOLDOWN_SECONDS", "900"))
CHANNEL_CONVERSATION_LIMIT = 20

# Active hours for the heartbeat (Chicago time). Outside this window, heartbeat is skipped.
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "7"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "22"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _post_to_elixir(channel, entry: dict):
    """Post an entry's content to #elixir channel."""
    content = entry.get("content", entry.get("summary", ""))
    if not content:
        return
    if len(content) > 2000:
        for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
            await channel.send(chunk)
    else:
        await channel.send(content)


def _match_clan_member(nickname):
    """Match a Discord nickname to a clan member. Returns (tag, name) or None.

    Uses the V2 member resolver but only accepts high-confidence exact matches.
    """
    normalized = (nickname or "").lower().strip()
    if not normalized:
        return None

    try:
        matches = db.resolve_member(nickname, limit=2)
        if matches:
            best = matches[0]
            if best.get("match_source") in {"player_tag_exact", "current_name_exact", "alias_exact"}:
                if len(matches) == 1 or matches[0].get("match_score") != matches[1].get("match_score"):
                    return (best["player_tag"], best.get("current_name") or best.get("member_name"))
            return None
    except Exception:
        pass

    try:
        snapshot = db.get_active_roster_map()
        for tag, name in snapshot.items():
            if name.lower().strip() == normalized:
                return (tag, name)
    except Exception:
        return None
    return None


def _channel_scope(channel) -> str:
    behavior = _get_channel_behavior(channel.id)
    if behavior and behavior.get("role") == "announcements":
        return "channel:elixir"
    return f"channel:{channel.id}"


def _strip_bot_mentions(text: str) -> str:
    if bot.user is None:
        return (text or "").strip()
    return (
        (text or "")
        .replace(f"<@{bot.user.id}>", "")
        .replace(f"<@!{bot.user.id}>", "")
        .replace(f"<@&{BOT_ROLE_ID}>", "")
        .strip()
    )


def _is_bot_mentioned(message) -> bool:
    return bot.user in message.mentions or any(r.id == BOT_ROLE_ID for r in message.role_mentions)


def _get_channel_behavior(channel_id):
    return prompts.discord_channels_by_id().get(channel_id)


def _get_singleton_channel(role):
    return prompts.discord_singleton_channel(role)


def _get_singleton_channel_id(role):
    return _get_singleton_channel(role)["id"]


def _channel_reply_target_name(channel_config):
    return channel_config.get("name") or f"channel:{channel_config['id']}"


def _clanops_cooldown_elapsed(channel_id):
    state = db.get_channel_state(channel_id)
    if not state or not state.get("last_elixir_post_at"):
        return True
    try:
        last_post = datetime.strptime(state["last_elixir_post_at"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - last_post).total_seconds() >= CLANOPS_PROACTIVE_COOLDOWN_SECONDS


async def _load_live_clan_context():
    clan = await asyncio.to_thread(cr_api.get_clan)
    if clan.get("memberList"):
        await asyncio.to_thread(db.snapshot_members, clan.get("memberList", []))
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
    except Exception:
        war = {}
    if war:
        await asyncio.to_thread(db.upsert_war_current_state, war)
    return clan, war


async def _share_channel_result(result, workflow):
    if result.get("event_type") not in {"channel_share", "leader_share"}:
        return
    share_content = result.get("share_content", "")
    if not share_content:
        return
    target_ref = result.get("share_channel") or "#elixir"
    target = prompts.resolve_channel_reference(target_ref)
    if not target:
        log.warning("Unknown share target channel: %s", target_ref)
        return
    target_channel = bot.get_channel(target["id"])
    if not target_channel:
        return
    await _post_to_elixir(target_channel, {"content": share_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(target_channel),
        "assistant",
        share_content,
        channel_id=target_channel.id,
        channel_name=getattr(target_channel, "name", None),
        channel_kind=str(target_channel.type),
        workflow=workflow,
        event_type=result.get("event_type"),
    )


# ── Heartbeat ────────────────────────────────────────────────────────────────

async def _heartbeat_tick():
    """Hourly heartbeat — fetch data, detect signals, post if interesting."""
    # Check active hours
    now_chicago = datetime.now(CHICAGO)
    if not (HEARTBEAT_START_HOUR <= now_chicago.hour < HEARTBEAT_END_HOUR):
        log.info("Heartbeat: outside active hours (%d:%02d), skipping",
                 now_chicago.hour, now_chicago.minute)
        return

    announcements_channel_id = _get_singleton_channel_id("announcements")
    channel = bot.get_channel(announcements_channel_id)
    if not channel:
        log.error("Announcements channel %s not found", announcements_channel_id)
        return

    try:
        # Run the heartbeat tick — fetches data, snapshots, detects signals
        tick_result = heartbeat.tick()
        signals = tick_result.signals

        if not signals:
            log.info("Heartbeat: no signals, nothing to post")
            return

        log.info("Heartbeat: %d signals detected, consulting LLM", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        # Fetch recent #elixir post history to avoid repetition
        recent_posts = await asyncio.to_thread(
            db.list_thread_messages, "channel:elixir", 20,
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
                        "channel:elixir", "assistant", msg,
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
                        "channel:elixir", "assistant", msg,
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
                return
            await _post_to_elixir(channel, result)
            content = result.get("content", result.get("summary", ""))
            if content:
                await asyncio.to_thread(
                    db.save_message,
                    "channel:elixir", "assistant", content,
                    channel_id=channel.id,
                    channel_name=getattr(channel, "name", None),
                    channel_kind=str(channel.type),
                    workflow="observation",
                    event_type=result.get("event_type"),
                )
            log.info("Posted observation: %s", result.get("summary"))

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "8"))       # 8am Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "20"))  # 8pm Chicago
PLAYER_INTEL_REFRESH_HOURS = int(os.getenv("PLAYER_INTEL_REFRESH_HOURS", "6"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "12"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "6"))


async def _site_data_refresh():
    """Morning job — refresh clan data and roster on poapkings.com."""
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            log.error("Site data refresh: CR API failed")
            clan = {}

        if not clan.get("memberList"):
            log.info("Site data refresh: no member data, skipping")
            return

        roster_data = site_content.build_roster_data(clan)
        site_content.write_content("roster", roster_data)

        clan_stats = site_content.build_clan_data(clan)
        site_content.write_content("clan", clan_stats)

        site_content.commit_and_push("Elixir data refresh")
        log.info("Site data refresh complete: %d members", len(roster_data.get("members", [])))
    except Exception as e:
        log.error("Site data refresh error: %s", e, exc_info=True)


async def _site_content_cycle():
    """Evening job — generate all site content and refresh data."""
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            clan = {}
        try:
            war = cr_api.get_current_war()
        except Exception:
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

        # Generate promote content on Sundays
        now_chicago = datetime.now(CHICAGO)
        if now_chicago.weekday() == 6:  # Sunday
            try:
                promote = elixir_agent.generate_promote_content(clan, roster_data=roster_data)
                if promote:
                    site_content.write_content("promote", promote)
            except Exception as e:
                log.error("Promote content error: %s", e)

        site_content.commit_and_push("Elixir content update")
        log.info("Site content cycle complete")
    except Exception as e:
        log.error("Site content cycle error: %s", e, exc_info=True)


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        return

    await asyncio.to_thread(db.snapshot_members, members)
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
        if war:
            await asyncio.to_thread(db.upsert_war_current_state, war)
    except Exception:
        war = {}

    targets = await asyncio.to_thread(
        db.get_player_intel_refresh_targets,
        PLAYER_INTEL_BATCH_SIZE,
        PLAYER_INTEL_STALE_HOURS,
    )
    if not targets:
        log.info("Player intel refresh: no stale targets")
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
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    if progression_signals:
        announcements_channel_id = _get_singleton_channel_id("announcements")
        channel = bot.get_channel(announcements_channel_id)
        if channel:
            recent_posts = await asyncio.to_thread(
                db.list_thread_messages, "channel:elixir", 20,
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
                content = result.get("content", result.get("summary", ""))
                if content:
                    await asyncio.to_thread(
                        db.save_message,
                        "channel:elixir", "assistant", content,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type=result.get("event_type"),
                    )

    log.info("Player intel refresh complete: refreshed %d members", refreshed)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Elixir online as %s 🧪", bot.user)
    if not scheduler.running:
        # Single hourly heartbeat replaces both the 4x/day observations and hourly member check
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_heartbeat_tick())
            ),
            "interval",
            hours=1,
            id="heartbeat",
        )
        # Morning data refresh for poapkings.com
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_site_data_refresh())
            ),
            "cron",
            hour=SITE_DATA_HOUR,
            minute=0,
            id="site_data_refresh",
        )
        # Evening content cycle for poapkings.com
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_site_content_cycle())
            ),
            "cron",
            hour=SITE_CONTENT_HOUR,
            minute=0,
            id="site_content_cycle",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_player_intel_refresh())
            ),
            "interval",
            hours=PLAYER_INTEL_REFRESH_HOURS,
            id="player_intel_refresh",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago), "
                 "site data refresh at %dam, content cycle at %dpm, player intel refresh every %dh",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 SITE_DATA_HOUR, SITE_CONTENT_HOUR, PLAYER_INTEL_REFRESH_HOURS)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    await asyncio.to_thread(
        db.upsert_discord_user,
        member.id,
        username=member.name,
        global_name=getattr(member, "global_name", None),
        display_name=member.display_name,
    )
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))
    if not channel:
        return
    msg = await asyncio.to_thread(
        elixir_agent.generate_message,
        "discord_member_join",
        f"A new user '{member.display_name}' ({member.mention}) just joined the Discord server. "
        f"Welcome them in #reception and explain how to set their server nickname "
        f"to match their Clash Royale in-game name to get verified.",
    )
    if msg:
        await channel.send(msg)
    else:
        await channel.send(
            f"Welcome to the server, {member.mention}! Set your server nickname "
            f"to your Clash Royale name and I'll get you verified."
        )


@bot.event
async def on_member_update(before, after):
    """Detect nickname changes and grant member role when name matches a clan member."""
    if before.nick == after.nick:
        return
    if not after.nick:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        after.id,
        username=after.name,
        global_name=getattr(after, "global_name", None),
        display_name=after.display_name,
    )

    # Only act if they don't already have the member role
    if not MEMBER_ROLE_ID:
        return
    member_role = after.guild.get_role(MEMBER_ROLE_ID)
    if not member_role or member_role in after.roles:
        return

    match = await asyncio.to_thread(_match_clan_member, after.nick)
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))

    if not match:
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "nickname_no_match",
                f"User {after.mention} set their nickname to '{after.nick}' but it doesn't "
                f"match anyone in the clan roster. Let them know and suggest they check "
                f"the spelling or join the clan first. Channel: #reception.",
            )
            await channel.send(msg or f"Hmm {after.mention}, I don't see **{after.nick}** in our roster.")
        return

    tag, cr_name = match
    await asyncio.to_thread(
        db.link_discord_user_to_member,
        after.id,
        tag,
        username=after.name,
        display_name=after.display_name,
        source="verified_nickname_match",
    )
    try:
        await after.add_roles(member_role, reason=f"Matched clan member: {cr_name} ({tag})")
    except discord.Forbidden:
        log.error("Cannot assign member role — check bot permissions and role hierarchy")
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "role_grant_failed",
                f"Matched user {after.mention} to clan member '{cr_name}' ({tag}) but "
                f"couldn't assign the member role due to permissions. Let them know "
                f"a leader will help. Channel: #reception.",
            )
            await channel.send(msg or f"I matched **{cr_name}** but couldn't assign the role.")
        return

    if channel:
        msg = await asyncio.to_thread(
            elixir_agent.generate_message,
            "nickname_matched",
            f"User {after.mention} set their nickname to '{cr_name}' which matches "
            f"clan member tag {tag}. They've been granted the member role. "
            f"Welcome them and let them know they have full access. Channel: #reception.",
        )
        await channel.send(msg or f"Welcome aboard, {cr_name}! You now have full access.")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        message.author.id,
        username=message.author.name,
        global_name=getattr(message.author, "global_name", None),
        display_name=message.author.display_name,
    )
    channel_config = _get_channel_behavior(message.channel.id)
    mentioned = _is_bot_mentioned(message)
    if not channel_config:
        await bot.process_commands(message)
        return

    role = channel_config.get("role")
    workflow = channel_config.get("workflow")
    scope = _channel_scope(message.channel)

    # Non-responsive singleton channels are outbound only.
    if not channel_config.get("respond_allowed", True):
        return

    if role == "onboarding" and not mentioned:
        return

    proactive = role == "clanops" and not mentioned
    if proactive and not _clanops_cooldown_elapsed(message.channel.id):
        await asyncio.to_thread(
            db.save_message,
            scope,
            "user",
            message.content.strip(),
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        return

    if not mentioned and not proactive:
        await bot.process_commands(message)
        return

    if role == "onboarding":
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                question = _strip_bot_mentions(message.content)
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow="reception",
                    discord_message_id=message.id,
                )
                result = await asyncio.to_thread(
                    elixir_agent.respond_in_reception,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    memory_context=memory_context,
                )
                if result is None:
                    await message.reply(
                        "Having a hiccup — try again in a sec! 🧪"
                    )
                    return
                content = result.get("content", result.get("summary", ""))
                if len(content) > 2000:
                    for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
                        await message.reply(chunk)
                else:
                    await message.reply(content)
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "assistant",
                    content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    workflow="reception",
                    event_type=result.get("event_type"),
                )
            except Exception as e:
                log.error("reception error: %s", e)
                await message.reply("Hit an error — try again in a moment. 🧪")
        return

    if workflow in {"interactive", "clanops"}:
        async with message.channel.typing():
            try:
                clan, war = await _load_live_clan_context()
                question = _strip_bot_mentions(message.content) if mentioned else message.content.strip()
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    scope,
                    CHANNEL_CONVERSATION_LIMIT,
                )
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )

                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow=workflow,
                    discord_message_id=message.id,
                )

                result = await asyncio.to_thread(
                    elixir_agent.respond_in_channel,
                    question=question,
                    author_name=message.author.display_name,
                    channel_name=_channel_reply_target_name(channel_config),
                    workflow=workflow,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                    proactive=proactive,
                )
                if result is None:
                    return

                content = result.get("content", result.get("summary", ""))
                await _share_channel_result(result, workflow)

                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "assistant",
                    content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow=workflow,
                    event_type=result.get("event_type"),
                )

                if len(content) > 2000:
                    for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
                        await message.reply(chunk)
                else:
                    await message.reply(content)
            except Exception as e:
                log.error("%s channel error: %s", workflow, e)
                if mentioned:
                    await message.reply("Hit an error — try again in a moment. 🧪")
        return

    await bot.process_commands(message)


PID_FILE = os.path.join(os.path.dirname(__file__), "elixir.pid")


def _acquire_pid_file():
    """Write current PID to file, killing any stale process first."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            log.info("Killed stale process %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # PID invalid, process gone, or not ours
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _cleanup_pid_file():
    """Remove PID file on clean shutdown."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    _acquire_pid_file()
    atexit.register(_cleanup_pid_file)
    bot.run(TOKEN)
