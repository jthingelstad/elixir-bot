"""Elixir - POAP KINGS Discord bot (LLM-powered with heartbeat)."""

import os
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api
import db
import journal
import elixir_agent
import heartbeat

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("DISCORD_ANNOUNCEMENTS_CHANNEL", "0"))
LEADERSHIP_CHANNEL_ID = int(os.getenv("DISCORD_LEADERSHIP_CHANNEL", "0"))
RECEPTION_CHANNEL_ID = int(os.getenv("DISCORD_RECEPTION_CHANNEL", "0"))
MEMBER_ROLE_ID = int(os.getenv("DISCORD_MEMBER_ROLE", "0"))
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

    Uses member_snapshot.json for the current roster. Case-insensitive.
    """
    snapshot = heartbeat._load_snapshot()
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
        signals = heartbeat.tick()

        if not signals:
            log.info("Heartbeat: no signals, nothing to post")
            return

        log.info("Heartbeat: %d signals detected, consulting LLM", len(signals))

        # Get clan + war data for context (heartbeat.tick already fetched it,
        # but we need it for the LLM context too)
        try:
            clan = cr_api.get_clan()
        except Exception:
            clan = {}
        try:
            war = cr_api.get_current_war()
        except Exception:
            war = {}

        # Handle join/leave signals directly (these get specific formatted posts)
        other_signals = []
        for sig in signals:
            if sig["type"] == "member_join":
                await channel.send(
                    f"👑 **Welcome to POAP KINGS, {sig['name']}!** 👑\n\n"
                    f"Glad to have you with us. Donate cards, battle hard, and climb together. "
                    f"Questions? Leadership is in #leader-lounge. Let's go! 🧪"
                )
            elif sig["type"] == "member_leave":
                await channel.send(
                    f"👋 **{sig['name']} has left POAP KINGS.**\n\n"
                    f"We wish them well on their journey. Onwards, kings! 🧪"
                )
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft a post
        if other_signals:
            result = elixir_agent.observe_and_post(clan, war, signals=other_signals)
            if result is None:
                log.info("Heartbeat: LLM decided signals not worth posting")
                return
            await _post_to_elixir(channel, result)
            log.info("Posted observation: %s", result.get("summary"))

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)


# ── Daily editorial for poapkings.com ────────────────────────────────────────

EDITORIAL_HOUR = int(os.getenv("EDITORIAL_HOUR", "20"))  # 8pm Chicago


async def _daily_editorial():
    """Evening job — write a short editorial for the poapkings.com website."""
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            clan = {}
        try:
            war = cr_api.get_current_war()
        except Exception:
            war = {}

        previous = journal.load_messages(POAPKINGS_REPO)

        text = elixir_agent.write_editorial(clan, war, previous)
        if not text:
            log.info("Editorial: LLM returned nothing, skipping")
            return

        journal.save_message(POAPKINGS_REPO, text)
        journal.commit_and_push(POAPKINGS_REPO)
        log.info("Editorial published: %s", text[:80])
    except Exception as e:
        log.error("Editorial error: %s", e, exc_info=True)


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
        # Daily editorial for poapkings.com website
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_daily_editorial())
            ),
            "cron",
            hour=EDITORIAL_HOUR,
            minute=0,
            id="editorial",
        )
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago), "
                 "daily editorial at %dpm",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR, EDITORIAL_HOUR)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    channel = bot.get_channel(RECEPTION_CHANNEL_ID)
    if not channel:
        return
    await channel.send(
        f"👋 Welcome to the **POAP KINGS** Discord, {member.mention}!\n\n"
        f"To get full access, please update your **server nickname** to match "
        f"your **Clash Royale in-game name** exactly.\n\n"
        f"**How to change your nickname:**\n"
        f"• **Desktop** — Right-click your name in the member list → "
        f"*Edit Server Profile* → change nickname\n"
        f"• **Mobile** — Tap the server name at the top → "
        f"*Edit Server Profile* → change nickname\n\n"
        f"Once I see a match with our clan roster, I'll unlock the rest of the "
        f"server for you. Need help? Just tag me! 🧪"
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

    match = _match_clan_member(after.nick)
    channel = bot.get_channel(RECEPTION_CHANNEL_ID)

    if not match:
        if channel:
            await channel.send(
                f"Hmm {after.mention}, I don't see **{after.nick}** in our clan roster. "
                f"Make sure your server nickname matches your Clash Royale name exactly. "
                f"Tag me if you need help! 🧪"
            )
        return

    tag, cr_name = match
    try:
        await after.add_roles(member_role, reason=f"Matched clan member: {cr_name} ({tag})")
    except discord.Forbidden:
        log.error("Cannot assign member role — check bot permissions and role hierarchy")
        if channel:
            await channel.send(
                f"I matched **{cr_name}** but couldn't assign the role — "
                f"a leader will need to help. 🧪"
            )
        return

    if channel:
        await channel.send(
            f"✅ **Welcome aboard, {cr_name}!** {after.mention}\n\n"
            f"Matched you to your Clash Royale account (`{tag}`). "
            f"You now have full access — head to the other channels and say hi! 🧪"
        )


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if bot.user not in message.mentions:
        return

    # #elixir channel — broadcast only, Elixir does not respond here
    if message.channel.id == ANNOUNCEMENTS_CHANNEL_ID:
        return

    # #reception — onboarding help
    if message.channel.id == RECEPTION_CHANNEL_ID:
        async with message.channel.typing():
            try:
                clan = cr_api.get_clan()
                question = message.content.replace(f"<@{bot.user.id}>", "").strip()
                result = elixir_agent.respond_in_reception(
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
                clan = cr_api.get_clan()
                try:
                    war = cr_api.get_current_war()
                except Exception:
                    war = {}
                # Strip the @Elixir mention from the question
                question = message.content.replace(f"<@{bot.user.id}>", "").strip()

                # Load conversation history for this leader
                author_id = str(message.author.id)
                conversation_history = db.get_conversation_history(author_id)

                # Save the leader's question
                db.save_conversation_turn(
                    author_id, message.author.display_name,
                    "user", question,
                )

                result = elixir_agent.respond_to_leader(
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

                # Save Elixir's response to conversation memory
                db.save_conversation_turn(
                    author_id, message.author.display_name,
                    "assistant", content,
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


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    bot.run(TOKEN)
