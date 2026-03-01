"""Elixir - POAP KINGS Discord bot (LLM-powered)."""
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
import journal
import elixir_agent

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("DISCORD_ANNOUNCEMENTS_CHANNEL", "0"))
LEADERSHIP_CHANNEL_ID = int(os.getenv("DISCORD_LEADERSHIP_CHANNEL", "0"))
SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "member_snapshot.json")
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_snapshot():
    if os.path.exists(SNAPSHOT_PATH):
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
    return {}


def _save_snapshot(data):
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(data, f, indent=2)


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


# ── Scheduled observations ────────────────────────────────────────────────────

async def _scheduled_observe():
    """Called 4x/day — ask Elixir agent if anything is worth posting."""
    channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
    if not channel:
        log.error("Announcements channel %s not found", ANNOUNCEMENTS_CHANNEL_ID)
        return
    try:
        clan = cr_api.get_clan()
        try:
            war = cr_api.get_current_war()
        except Exception:
            war = {}
        entries = journal.load_entries(POAPKINGS_REPO)
        recent = journal.recent_entries(entries, 20)
        result = elixir_agent.observe_and_post(clan, war, recent)
        if result is None:
            log.info("Elixir agent: nothing worth posting right now")
            return
        entry = await _write_and_push(result, f"Elixir observation [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]")
        await _post_to_elixir(channel, entry)
        log.info("Posted observation: %s", result.get("summary"))
    except Exception as e:
        log.error("_scheduled_observe error: %s", e)


# ── Member change detection ───────────────────────────────────────────────────

async def _check_member_changes():
    """Hourly — detect joins and departures, log to journal, post to #elixir."""
    try:
        clan = cr_api.get_clan()
        current = {m["tag"]: m["name"] for m in clan.get("memberList", [])}
        known = _load_snapshot()
        channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)

        for tag, name in current.items():
            if tag not in known:
                log.info("New member: %s (%s)", name, tag)
                entry = {
                    "event_type": "member_join",
                    "member_tags": [tag],
                    "member_names": [name],
                    "summary": f"{name} joined POAP KINGS.",
                    "content": (
                        f"👑 **Welcome to POAP KINGS, {name}!** 👑\n\n"
                        f"Glad to have you with us. Donate cards, battle hard, and climb together. "
                        f"Questions? Leadership is in #leader-lounge. Let's go! 🧪"
                    ),
                    "metadata": {"tag": tag}
                }
                saved = await _write_and_push(entry, f"Elixir: {name} joined the clan")
                if channel:
                    await _post_to_elixir(channel, saved)

        for tag, name in known.items():
            if tag not in current:
                log.info("Member left: %s (%s)", name, tag)
                entry = {
                    "event_type": "member_leave",
                    "member_tags": [tag],
                    "member_names": [name],
                    "summary": f"{name} left POAP KINGS.",
                    "content": (
                        f"👋 **{name} has left POAP KINGS.**\n\n"
                        f"We wish them well on their journey. Onwards, kings! 🧪"
                    ),
                    "metadata": {"tag": tag}
                }
                saved = await _write_and_push(entry, f"Elixir: {name} left the clan")
                if channel:
                    await _post_to_elixir(channel, saved)

        _save_snapshot(current)
    except Exception as e:
        log.error("_check_member_changes error: %s", e)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Elixir online as %s 🧪", bot.user)
    scheduler.add_job(lambda: asyncio.ensure_future(_scheduled_observe()), "cron", hour=7, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_scheduled_observe()), "cron", hour=12, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_scheduled_observe()), "cron", hour=17, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_scheduled_observe()), "cron", hour=21, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_check_member_changes()), "interval", hours=1)
    scheduler.start()
    log.info("Scheduler started — observations at 7am, 12pm, 5pm, 9pm + hourly member check")


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
                result = elixir_agent.respond_to_leader(
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    war_data=war,
                    recent_entries=recent
                )
                if result is None:
                    await message.reply("Something went wrong on my end — try again? 🧪")
                    return
                # NOTE: leader-lounge responses are NOT written to elixir.json (private)
                content = result.get("content", result.get("summary", ""))
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
