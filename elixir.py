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
ANNOUNCEMENTS_CHANNEL_ID = _dc.get("announcements_channel", 0)
LEADERSHIP_CHANNEL_ID = _dc.get("leadership_channel", 0)
RECEPTION_CHANNEL_ID = _dc.get("reception_channel", 0)
MEMBER_ROLE_ID = _dc.get("member_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))

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

    Uses the latest SQLite roster snapshot. Case-insensitive.
    """
    snapshot = db.get_known_roster()
    normalized = nickname.lower().strip()
    for tag, name in snapshot.items():
        if name.lower().strip() == normalized:
            return (tag, name)
    return None


# ── Heartbeat ────────────────────────────────────────────────────────────────

async def _heartbeat_tick():
    """Hourly heartbeat — fetch data, detect signals, post if interesting."""
    # Check active hours
    now_chicago = datetime.now(CHICAGO)
    if not (HEARTBEAT_START_HOUR <= now_chicago.hour < HEARTBEAT_END_HOUR):
        log.info("Heartbeat: outside active hours (%d:%02d), skipping",
                 now_chicago.hour, now_chicago.minute)
        return

    channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
    if not channel:
        log.error("Announcements channel %s not found", ANNOUNCEMENTS_CHANNEL_ID)
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
            db.get_conversation_history, "channel:elixir", 20,
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
                        db.save_conversation_turn,
                        "channel:elixir", "assistant", msg,
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
                        db.save_conversation_turn,
                        "channel:elixir", "assistant", msg,
                    )
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft a post
        if other_signals:
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post, clan, war,
                other_signals, recent_posts,
            )
            if result is None:
                log.info("Heartbeat: LLM decided signals not worth posting")
                return
            await _post_to_elixir(channel, result)
            content = result.get("content", result.get("summary", ""))
            if content:
                await asyncio.to_thread(
                    db.save_conversation_turn,
                    "channel:elixir", "assistant", content,
                )
            log.info("Posted observation: %s", result.get("summary"))

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "8"))       # 8am Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "20"))  # 8pm Chicago


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
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago), "
                 "site data refresh at %dam, content cycle at %dpm",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 SITE_DATA_HOUR, SITE_CONTENT_HOUR)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    channel = bot.get_channel(RECEPTION_CHANNEL_ID)
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

    # Only act if they don't already have the member role
    if not MEMBER_ROLE_ID:
        return
    member_role = after.guild.get_role(MEMBER_ROLE_ID)
    if not member_role or member_role in after.roles:
        return

    match = await asyncio.to_thread(_match_clan_member, after.nick)
    channel = bot.get_channel(RECEPTION_CHANNEL_ID)

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
    mentioned = (bot.user in message.mentions or
                 any(r.id == BOT_ROLE_ID for r in message.role_mentions))
    if not mentioned:
        return

    # #elixir channel — broadcast only, Elixir does not respond here
    if message.channel.id == ANNOUNCEMENTS_CHANNEL_ID:
        return

    # #reception — onboarding help
    if message.channel.id == RECEPTION_CHANNEL_ID:
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@&{BOT_ROLE_ID}>", "").strip()
                result = await asyncio.to_thread(
                    elixir_agent.respond_in_reception,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
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
            except Exception as e:
                log.error("reception error: %s", e)
                await message.reply("Hit an error — try again in a moment. 🧪")
        return

    # #leader-lounge — relay everything to the agent
    if message.channel.id == LEADERSHIP_CHANNEL_ID:
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                try:
                    war = await asyncio.to_thread(cr_api.get_current_war)
                except Exception:
                    war = {}
                # Strip the @Elixir mention from the question
                question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@&{BOT_ROLE_ID}>", "").strip()

                # Load conversation history for this leader
                leader_scope = f"leader:{message.author.id}"
                conversation_history = await asyncio.to_thread(
                    db.get_conversation_history, leader_scope,
                )

                # Save the leader's question
                await asyncio.to_thread(
                    db.save_conversation_turn,
                    leader_scope, "user", question,
                    message.author.display_name,
                )

                result = await asyncio.to_thread(
                    elixir_agent.respond_to_leader,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                )
                if result is None:
                    await message.reply("Something went wrong on my end — try again? 🧪")
                    return

                content = result.get("content", result.get("summary", ""))

                # If the leader asked to share something with the clan, post to #elixir
                if result.get("event_type") == "leader_share":
                    share_content = result.get("share_content", "")
                    if share_content:
                        elixir_channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
                        if elixir_channel:
                            await _post_to_elixir(elixir_channel, {
                                "content": share_content,
                            })
                            await asyncio.to_thread(
                                db.save_conversation_turn,
                                "channel:elixir", "assistant", share_content,
                            )

                # Save Elixir's response to conversation memory
                await asyncio.to_thread(
                    db.save_conversation_turn,
                    leader_scope, "assistant", content,
                    message.author.display_name,
                )

                if len(content) > 2000:
                    for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
                        await message.reply(chunk)
                else:
                    await message.reply(content)
            except Exception as e:
                log.error("leader-lounge error: %s", e)
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
