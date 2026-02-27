"""Elixir 🧪 — POAP KINGS Discord bot."""
import os
import logging
import asyncio
from datetime import datetime
import json
import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api
import announcements

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("DISCORD_ANNOUNCEMENTS_CHANNEL", "0"))
LEADERSHIP_CHANNEL_ID = int(os.getenv("DISCORD_LEADERSHIP_CHANNEL", "0"))

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
        delta = datetime.now(timezone.utc) - dt
        return delta.days
    except Exception:
        return 999


def _format_roster(members):
    lines = []
    for m in sorted(members, key=lambda x: x.get("clanRank", 99)):
        role = m.get("role", "member").replace("coLeader", "Co-Leader").replace("leader", "Leader").replace("elder", "Elder").replace("member", "Member")
        lines.append(f"• **{m['name']}** ({role}) — {m.get('trophies',0):,} 🏆 | {m.get('donations',0)} 🎁")
    return "\n".join(lines)


def _promotion_candidates(members):
    return [
        m for m in members
        if m.get("donations", 0) >= 100
        and m.get("trophies", 0) >= 4000
        and m.get("role", "member") == "member"
    ]


def _inactive_members(members):
    return [
        m for m in members
        if m.get("donations", 0) < 20 and _last_seen_days(m) >= 3
    ]


# ── Bot events ────────────────────────────────────────────────────────────────


async def _check_new_members():
    """Poll for new clan members and post welcome messages."""
    snapshot_path = os.path.join(os.path.dirname(__file__), "member_snapshot.json")
    try:
        clan = cr_api.get_clan()
        current = {m["tag"]: m["name"] for m in clan.get("memberList", [])}

        if os.path.exists(snapshot_path):
            known = json.load(open(snapshot_path))
        else:
            known = {}

        new_members = {tag: name for tag, name in current.items() if tag not in known}

        if new_members:
            channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
            if channel:
                for tag, name in new_members.items():
                    msg = (
                        f"👑 **Welcome to POAP KINGS, {name}!** 👑\n\n"
                        f"Glad to have you in the clan! Donate cards, battle hard, and let's climb together. "
                        f"If you have questions, leadership is in #leader-lounge. Let's go! 🧪"
                    )
                    await channel.send(msg)
                    log.info(f"Welcomed new member: {name} ({tag})")

        # Update snapshot
        json.dump(current, open(snapshot_path, "w"), indent=2)

    except Exception as e:
        log.error(f"_check_new_members error: {e}")

@bot.event
async def on_ready():
    log.info(f"Elixir online as {bot.user} 🧪")
    # Schedule announcements
    ann_channel_id = ANNOUNCEMENTS_CHANNEL_ID
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("morning")), "cron", hour=7, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("noon")), "cron", hour=12, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("afternoon")), "cron", hour=17, minute=0)
    scheduler.add_job(lambda: asyncio.ensure_future(_run_post("evening")), "cron", hour=21, minute=0)
    scheduler.start()
    log.info("Scheduler started — posts at 7am, 12pm, 5pm, 9pm Chicago time")
    scheduler.add_job(lambda: asyncio.ensure_future(_check_new_members()), "interval", minutes=30)


async def _run_post(kind):
    channel = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
    if not channel:
        log.error(f"Announcements channel {ANNOUNCEMENTS_CHANNEL_ID} not found")
        return
    if kind == "morning":
        await announcements.morning_post(channel)
    elif kind == "noon":
        await announcements.noon_post(channel)
    elif kind == "afternoon":
        await announcements.afternoon_post(channel)
    elif kind == "evening":
        await announcements.evening_post(channel)


@bot.event
async def on_message(message):
    log.info(f"Message received: channel={message.channel.id} author={message.author} mentions={[u.id for u in message.mentions]}")
    if message.author.bot:
        return
    if bot.user not in message.mentions:
        log.info(f"Ignoring — bot not mentioned")
        return
    # Allow manual trigger in announcements channel
    if message.channel.id == ANNOUNCEMENTS_CHANNEL_ID:
        content_lower = message.content.lower()
        if any(w in content_lower for w in ["war", "battle", "river race"]):
            async with message.channel.typing():
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
                        lines = ["\u2694\ufe0f **River Race Report \u2014 POAP KINGS**\n"]
                        lines.append("State: **" + state + "** | Fame: **" + f"{fame:,}" + "** \u26a1")
                        lines.append("Decks used today: **" + str(len(used)) + "/" + str(len(parts)) + "**\n")
                        if used_names:
                            lines.append("\U0001f525 Battled: " + used_names)
                        if unused_names:
                            extra = "..." if len(unused) > 5 else ""
                            lines.append("\U0001f634 Still need to battle: " + unused_names + extra)
                        lines.append("\n\U0001f9ea Let's finish strong, kings!")
                        await message.channel.send("\n".join(lines))
                except Exception as e:
                    log.error(f"war post error: {e}")
                    await message.channel.send("Could not fetch war data. Try again in a moment. \U0001f9ea")
        elif any(w in content_lower for w in ["update", "post", "report", "status"]):
            async with message.channel.typing():
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

    if message.channel.id != LEADERSHIP_CHANNEL_ID:
        log.info(f"Ignoring — wrong channel {message.channel.id} != {LEADERSHIP_CHANNEL_ID}")
        return

    content = message.content.lower()

    async with message.channel.typing():
        try:
            clan = cr_api.get_clan()
            members = clan.get("memberList", [])

            if any(w in content for w in ["promote", "promotion", "elder"]):
                candidates = _promotion_candidates(members)
                if not candidates:
                    reply = "👀 No clear Elder candidates right now. Everyone either doesn't have enough donations (need 100+/week) or trophies (need 4000+). Keep pushing! 🧪"
                else:
                    lines = "\n".join(
                        f"• **{m['name']}** — {m.get('donations',0)} donations, {m.get('trophies',0):,} trophies"
                        for m in candidates
                    )
                    reply = f"⬆️ **Elder promotion candidates** ({len(candidates)} found):\n\n{lines}\n\nThese members are pulling their weight — solid donors and strong in the arena. I'd promote them. 🧪"

            elif any(w in content for w in ["inactive", "quiet", "missing", "absent"]):
                inactive = _inactive_members(members)
                if not inactive:
                    reply = "✅ Clan looks active! Everyone's donating and showing up. Keep it up kings! 🧪"
                else:
                    lines = "\n".join(
                        f"• **{m['name']}** — {m.get('donations',0)} donations, last seen {_last_seen_days(m)} days ago"
                        for m in inactive
                    )
                    reply = f"😴 **Inactive members** ({len(inactive)} flagged):\n\n{lines}\n\nThese folks have been quiet. Worth a nudge — or a boot if it's a pattern. Your call. 🧪"

            elif any(w in content for w in ["kick", "remove", "boot"]):
                # Try to find a name in the message
                name_match = None
                for m in members:
                    if m["name"].lower() in content:
                        name_match = m
                        break
                if name_match:
                    m = name_match
                    days = _last_seen_days(m)
                    reply = (
                        f"🤔 **Should we kick {m['name']}?**\n\n"
                        f"• Role: {m.get('role','member')}\n"
                        f"• Trophies: {m.get('trophies',0):,}\n"
                        f"• Donations: {m.get('donations',0)}\n"
                        f"• Last seen: {days} days ago\n\n"
                    )
                    if m.get("donations", 0) < 20 and days >= 5:
                        reply += "📊 **My take:** Yeah, probably time. Low donations and hasn't been around. 🧪"
                    elif m.get("donations", 0) >= 50:
                        reply += "📊 **My take:** I'd hold off — they're donating well. Maybe just a check-in? 🧪"
                    else:
                        reply += "📊 **My take:** On the fence. Give them a week and revisit. 🧪"
                else:
                    reply = "Who are you thinking of kicking? Name me someone and I'll pull their stats. 🧪"

            elif any(w in content for w in ["roster", "members", "list"]):
                reply = f"📋 **POAP KINGS Roster** ({clan.get('members',0)}/50)\n\n{_format_roster(members)}"

            elif any(w in content for w in ["stats", "status", "how are we", "overview"]):
                score = clan.get("clanScore", 0)
                war_trophies = clan.get("clanWarTrophies", 0)
                donations = clan.get("donationsPerWeek", 0)
                top_donor = sorted(members, key=lambda m: m.get("donations", 0), reverse=True)
                top_trophy = sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)

                war = cr_api.get_current_war()
                war_str = "No active war" if not war or war.get("state") in (None, "notInWar") else f"War active — {war.get('state')}"

                health_msg = "Looking strong! Keep it up." if donations > 500 else "We can do better on donations — keep pushing!"
                reply = (
                    f"📊 **POAP KINGS — Clan Status**\n\n"
                    f"👥 Members: **{clan.get('members',0)}/50**\n"
                    f"🏆 Clan Score: **{score:,}** | War Trophies: **{war_trophies:,}**\n"
                    f"🎁 Donations/week: **{donations:,}**\n"
                    f"⚔️ War: **{war_str}**\n\n"
                    f"🥇 Top donor: **{top_donor[0]['name']}** ({top_donor[0].get('donations',0)})\n"
                    f"🥇 Trophy leader: **{top_trophy[0]['name']}** ({top_trophy[0].get('trophies',0):,})\n\n"
                    f"🧪 Overall: {health_msg}"
                )

            elif any(w in content for w in ["war", "battle"]):
                war = cr_api.get_current_war()
                if not war or war.get("state") in (None, "notInWar"):
                    reply = "⚔️ No active war right now. Prep your decks and stay ready! 🧪"
                else:
                    reply = f"⚔️ **War status: {war.get('state','unknown')}**\n\nMake sure everyone uses their battle decks! Check the war tab for details. 🧪"

            else:
                reply = (
                    f"Hey! I'm **Elixir** 🧪, your POAP KINGS clan assistant.\n\n"
                    f"Ask me things like:\n"
                    f"• *who should we promote?*\n"
                    f"• *who is inactive?*\n"
                    f"• *should we kick [name]?*\n"
                    f"• *clan stats*\n"
                    f"• *roster*\n"
                    f"• *war status*"
                )

        except requests.exceptions.HTTPError as e:
            if "invalidIp" in str(e):
                reply = "⚠️ CR API key IP whitelist issue — can't reach the API right now. Check developer.clashroyale.com. 🧪"
            else:
                reply = f"⚠️ API error: {e} 🧪"
        except Exception as e:
            log.error(f"on_message error: {e}")
            reply = "⚠️ Something went wrong pulling clan data. Try again in a moment. 🧪"

    # Split long messages
    if len(reply) > 2000:
        for chunk in [reply[i:i+1990] for i in range(0, len(reply), 1990)]:
            await message.reply(chunk)
    else:
        await message.reply(reply)

    await bot.process_commands(message)


import requests  # noqa: E402 (needed for error handling above)

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    bot.run(TOKEN)
