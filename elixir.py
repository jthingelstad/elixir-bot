"""Elixir - POAP KINGS Discord bot (LLM-powered with heartbeat)."""

import os
import json
import logging
import asyncio
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
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))

# Active hours for the heartbeat (Chicago time). Outside this window, heartbeat is skipped.
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "7"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "22"))

intents = discord.Intents.default()
intents.message_content = True
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


async def _write_and_push(entry: dict, commit_msg: str):
    """Append to journal and push to poapkings.com."""
    saved = journal.append_entry(POAPKINGS_REPO, entry)
    journal.commit_and_push(POAPKINGS_REPO, commit_msg)
    return saved


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

        entries = journal.load_entries(POAPKINGS_REPO)
        recent = journal.recent_entries(entries, 20)

        # Handle join/leave signals directly (these get specific formatted posts)
        other_signals = []
        for sig in signals:
            if sig["type"] == "member_join":
                entry = {
                    "event_type": "member_join",
                    "member_tags": [sig["tag"]],
                    "member_names": [sig["name"]],
                    "summary": f"{sig['name']} joined POAP KINGS.",
                    "content": (
                        f"👑 **Welcome to POAP KINGS, {sig['name']}!** 👑\n\n"
                        f"Glad to have you with us. Donate cards, battle hard, and climb together. "
                        f"Questions? Leadership is in #leader-lounge. Let's go! 🧪"
                    ),
                    "metadata": {"tag": sig["tag"]},
                }
                saved = await _write_and_push(entry, f"Elixir: {sig['name']} joined the clan")
                await _post_to_elixir(channel, saved)
            elif sig["type"] == "member_leave":
                entry = {
                    "event_type": "member_leave",
                    "member_tags": [sig["tag"]],
                    "member_names": [sig["name"]],
                    "summary": f"{sig['name']} left POAP KINGS.",
                    "content": (
                        f"👋 **{sig['name']} has left POAP KINGS.**\n\n"
                        f"We wish them well on their journey. Onwards, kings! 🧪"
                    ),
                    "metadata": {"tag": sig["tag"]},
                }
                saved = await _write_and_push(entry, f"Elixir: {sig['name']} left the clan")
                await _post_to_elixir(channel, saved)
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft a post
        if other_signals:
            result = elixir_agent.observe_and_post(clan, war, recent, signals=other_signals)
            if result is None:
                log.info("Heartbeat: LLM decided signals not worth posting")
                return
            entry = await _write_and_push(
                result,
                f"Elixir observation [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]",
            )
            await _post_to_elixir(channel, entry)
            log.info("Posted observation: %s", result.get("summary"))

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)


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
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago)",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if bot.user not in message.mentions:
        return

    # #elixir channel — broadcast only, Elixir does not respond here
    if message.channel.id == ANNOUNCEMENTS_CHANNEL_ID:
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
                entries = journal.load_entries(POAPKINGS_REPO)
                recent = journal.recent_entries(entries, 20)
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
                    recent_entries=recent,
                    conversation_history=conversation_history,
                )
                if result is None:
                    await message.reply("Something went wrong on my end — try again? 🧪")
                    return
                # NOTE: leader-lounge responses are NOT written to elixir.json (private)
                content = result.get("content", result.get("summary", ""))

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
