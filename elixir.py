"""Elixir - POAP KINGS Discord bot."""
import os
import json
import logging
import asyncio
from datetime import datetime
import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
import requests

import cr_api
import announcements

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("DISCORD_ANNOUNCEMENTS_CHANNEL", "0"))
LEADERSHIP_CHANNEL_ID = int(os.getenv("DISCORD_LEADERSHIP_CHANNEL", "0"))
SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "member_snapshot.json")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _last_seen_days(member):
    from datetime import timezone
    ls = member.get("lastSeen", "")
    if not ls:
        return 999
    try:
        dt = datetime.strptime(ls, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 999


def _format_roster(members):
    lines = []
    for m in sorted(members, key=lambda x: x.get("clanRank", 99)):
        role = m.get("role", "member").replace("coLeader", "Co-Leader").replace("leader", "Leader").replace("elder", "Elder").replace("member", "Member")
        lines.append(f"• **{m['name']}** ({role}) — {m.get('trophies',0):,} 🏆 | {m.get('donations',0)} 🎁")
    return "\n".join(lines)


def _promotion_candidates(members):
    return [m for m in members if m.get("donations", 0) >= 100 and m.get("trophies", 0) >= 4000 and m.get("role", "member") == "member"]


def _inactive_members(members):
    return [m for m in members if m.get("donations", 0) < 20 and _last_seen_days(m) >= 3]


# ── Member change detection ───────────────────────────────────────────────────

async def _check_member_changes():
    """Hourly check for new/departed members."""
    try:
        clan = cr_api.get_clan()
        current = {m["tag"]: m["name"] for m in clan.get("memberList", [])}
        known = json.load(open(SNAPSHOT_PATH)) if os.path.exists(SNAPSHOT_PATH) else {}
        channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)

        for tag, name in current.items():
            if tag not in known and channel:
                await channel.send(
                    f"\U0001f451 **Welcome to POAP KINGS, {name}!** \U0001f451\n\n"
                    f"Glad to have you! Donate cards, battle hard, climb together. "
                    f"Questions? Leadership is in #leader-lounge. Let\u2019s go! \U0001f9ea"
                )
                log.info("Welcomed new member: %s (%s)", name, tag)

        for tag, name in known.items():
            if tag not in current and channel:
                await channel.send(
                    f"\U0001f44b **{name} has left POAP KINGS.**\n\n"
                    f"We wish them well on their journey. Onwards, kings! \U0001f9ea"
                )
                log.info("Departure noticed: %s (%s)", name, tag)

        json.dump(current, open(SNAPSHOT_PATH, "w"), indent=2)

    except Exception as e:
        log.error("_check_member_changes error: %s", e)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info("Elixir online as %s 🧪", bot.user)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("morning")), "cron", hour=7, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("noon")), "cron", hour=12, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("afternoon")), "cron", hour=17, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("evening")), "cron", hour=21, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_check_member_changes()), "interval", hours=1)
    scheduler.start()
    log.info("Scheduler started — posts at 7am, 12pm, 5pm, 9pm + hourly member check")


async def _run_post(kind):
    channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
    if not channel:
        log.error("Announcements channel %s not found", ANNOUNCEMENTS_CHANNEL_ID)
        return
    fns = {"morning": announcements.morning_post, "noon": announcements.noon_post,
           "afternoon": announcements.afternoon_post, "evening": announcements.evening_post}
    if kind in fns:
        await fns[kind](channel)


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if bot.user not in message.mentions:
        return

    log.info("Message from %s in channel %s", message.author, message.channel.id)
    content_lower = message.content.lower()

    # ── #elixir / announcements channel ──────────────────────────────────────
    if message.channel.id == ANNOUNCEMENTS_CHANNEL_ID:
        async with message.channel.typing():
            if any(w in content_lower for w in ["war", "battle", "river race"]):
                try:
                    war = cr_api.get_current_war()
                    if not war or war.get("state") in (None, "notInWar"):
                        await message.channel.send("No active River Race right now. Rest up! \U0001f9ea")
                    else:
                        parts = war.get("clan", {}).get("participants", [])
                        fame = war.get("clan", {}).get("fame", 0)
                        state = war.get("state", "active")
                        used = [p for p in parts if p.get("decksUsedToday", 0) > 0]
                        unused = [p for p in parts if p.get("decksUsedToday", 0) == 0]
                        used_names = ", ".join("**" + p["name"] + "**" for p in used[:5])
                        unused_names = ", ".join("**" + p["name"] + "**" for p in unused[:5])
                        extra = "..." if len(unused) > 5 else ""
                        msg = (
                            f"\u2694\ufe0f **River Race Report \u2014 POAP KINGS**\n\n"
                            f"State: **{state}** | Fame: **{fame:,}** \u26a1\n"
                            f"Decks used today: **{len(used)}/{len(parts)}**\n\n"
                        )
                        if used_names:
                            msg += f"\U0001f525 Battled: {used_names}\n"
                        if unused_names:
                            msg += f"\U0001f634 Still need to battle: {unused_names}{extra}\n"
                        msg += "\n\U0001f9ea Let's finish strong, kings!"
                        await message.channel.send(msg)
                except Exception as e:
                    log.error("war post error: %s", e)
                    await message.channel.send("Couldn't fetch war data right now. Try again! \U0001f9ea")

            elif any(w in content_lower for w in ["update", "post", "report", "status"]):
                now = datetime.now(CHICAGO).hour
                if now < 10:
                    await announcements.morning_post(message.channel)
                elif now < 14:
                    await announcements.noon_post(message.channel)
                elif now < 19:
                    await announcements.afternoon_post(message.channel)
                else:
                    await announcements.evening_post(message.channel)
            else:
                await message.channel.send("Try: `@Elixir update`, `@Elixir war`, or `@Elixir report` \U0001f9ea")
        return

    # ── #leader-lounge ────────────────────────────────────────────────────────
    if message.channel.id != LEADERSHIP_CHANNEL_ID:
        return

    async with message.channel.typing():
        try:
            clan = cr_api.get_clan()
            members = clan.get("memberList", [])

            if any(w in content_lower for w in ["promote", "promotion", "elder"]):
                candidates = _promotion_candidates(members)
                if not candidates:
                    reply = "No clear Elder candidates right now — need 100+ donations and 4000+ trophies. Keep pushing! \U0001f9ea"
                else:
                    lines = "\n".join(f"• **{m['name']}** — {m.get('donations',0)} donations, {m.get('trophies',0):,} trophies" for m in candidates)
                    reply = f"\u2b06\ufe0f **Elder promotion candidates** ({len(candidates)} found):\n\n{lines}\n\nThese members are pulling their weight. I'd promote them. \U0001f9ea"

            elif any(w in content_lower for w in ["inactive", "quiet", "missing", "absent"]):
                inactive = _inactive_members(members)
                if not inactive:
                    reply = "\u2705 Clan looks active! Everyone's donating and showing up. \U0001f9ea"
                else:
                    lines = "\n".join(f"• **{m['name']}** — {m.get('donations',0)} donations, last seen {_last_seen_days(m)} days ago" for m in inactive)
                    reply = f"\U0001f634 **Inactive members** ({len(inactive)} flagged):\n\n{lines}\n\nWorth a nudge — or a boot if it's a pattern. Your call. \U0001f9ea"

            elif any(w in content_lower for w in ["kick", "remove", "boot"]):
                match = next((m for m in members if m["name"].lower() in content_lower), None)
                if match:
                    m = match
                    days = _last_seen_days(m)
                    reply = f"\U0001f914 **Should we kick {m['name']}?**\n\n• Role: {m.get('role','member')}\n• Trophies: {m.get('trophies',0):,}\n• Donations: {m.get('donations',0)}\n• Last seen: {days} days ago\n\n"
                    if m.get("donations", 0) < 20 and days >= 5:
                        reply += "\U0001f4ca My take: Yeah, probably time. Low donations and hasn't been around. \U0001f9ea"
                    elif m.get("donations", 0) >= 50:
                        reply += "\U0001f4ca My take: Hold off — they're donating well. Maybe just a check-in? \U0001f9ea"
                    else:
                        reply += "\U0001f4ca My take: On the fence. Give them a week and revisit. \U0001f9ea"
                else:
                    reply = "Who are you thinking of kicking? Name someone and I'll pull their stats. \U0001f9ea"

            elif any(w in content_lower for w in ["roster", "members", "list"]):
                reply = f"\U0001f4cb **POAP KINGS Roster** ({clan.get('members',0)}/50)\n\n{_format_roster(members)}"

            elif any(w in content_lower for w in ["stats", "status", "how are we", "overview"]):
                score = clan.get("clanScore", 0)
                war_trophies = clan.get("clanWarTrophies", 0)
                donations = clan.get("donationsPerWeek", 0)
                top_donor = sorted(members, key=lambda m: m.get("donations", 0), reverse=True)
                top_trophy = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)
                war = cr_api.get_current_war()
                war_str = "No active war" if not war or war.get("state") in (None, "notInWar") else f"River Race — {war.get('state')}"
                health_msg = "Looking strong! Keep it up." if donations > 500 else "We can do better on donations — keep pushing!"
                reply = (
                    f"\U0001f4ca **POAP KINGS \u2014 Clan Status**\n\n"
                    f"\U0001f465 Members: **{clan.get('members',0)}/50**\n"
                    f"\U0001f3c6 Clan Score: **{score:,}** | War Trophies: **{war_trophies:,}**\n"
                    f"\U0001f381 Donations/week: **{donations:,}**\n"
                    f"\u2694\ufe0f War: **{war_str}**\n\n"
                    f"\U0001f947 Top donor: **{top_donor[0]['name']}** ({top_donor[0].get('donations',0)})\n"
                    f"\U0001f947 Trophy leader: **{top_trophy[0]['name']}** ({top_trophy[0].get('trophies',0):,})\n\n"
                    f"\U0001f9ea {health_msg}"
                )

            elif any(w in content_lower for w in ["war", "battle"]):
                war = cr_api.get_current_war()
                if not war or war.get("state") in (None, "notInWar"):
                    reply = "\u2694\ufe0f No active war right now. Prep your decks! \U0001f9ea"
                else:
                    parts = war.get("clan", {}).get("participants", [])
                    fame = war.get("clan", {}).get("fame", 0)
                    unused = [p for p in parts if p.get("decksUsedToday", 0) == 0]
                    reply = f"\u2694\ufe0f River Race is **{war.get('state')}** | {fame:,} fame | {len(parts)-len(unused)}/{len(parts)} battled today. \U0001f9ea"

            else:
                reply = (
                    "Hey! I'm **Elixir** \U0001f9ea, your POAP KINGS assistant.\n\n"
                    "Ask me:\n"
                    "• *who should we promote?*\n"
                    "• *who is inactive?*\n"
                    "• *should we kick [name]?*\n"
                    "• *clan stats*\n"
                    "• *roster*\n"
                    "• *war status*"
                )

        except requests.exceptions.HTTPError as e:
            reply = f"\u26a0\ufe0f API error: {e} \U0001f9ea"
        except Exception as e:
            log.error("on_message error: %s", e)
            reply = "\u26a0\ufe0f Something went wrong. Try again in a moment. \U0001f9ea"

    if len(reply) > 2000:
        for chunk in [reply[i:i+1990] for i in range(0, len(reply), 1990)]:
            await message.reply(chunk)
    else:
        await message.reply(reply)

    await bot.process_commands(message)


if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    bot.run(TOKEN)
