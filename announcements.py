"""Scheduled announcement logic for Elixir bot."""
import logging
from datetime import datetime, timezone
import pytz

import cr_api

log = logging.getLogger(__name__)
CHICAGO = pytz.timezone("America/Chicago")


def _top_donors(members, n=3):
    return sorted(members, key=lambda m: m.get("donations", 0), reverse=True)[:n]


def _top_trophies(members, n=5):
    return sorted(members, key=lambda m: m.get("trophies", 0), reverse=True)[:n]


def _last_seen_days(member):
    ls = member.get("lastSeen", "")
    if not ls:
        return 999
    try:
        dt = datetime.strptime(ls, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.days
    except Exception:
        return 999


async def morning_post(channel):
    """7am: Daily clan overview."""
    try:
        clan = cr_api.get_clan()
        members = clan.get("memberList", [])
        count = clan.get("members", 0)
        score = clan.get("clanScore", 0)
        donations = clan.get("donationsPerWeek", 0)
        top3 = _top_donors(members, 3)
        trophy_leader = _top_trophies(members, 1)

        donor_str = ", ".join(f"**{m['name']}** ({m.get('donations',0)} 🎁)" for m in top3)
        leader_str = f"**{trophy_leader[0]['name']}** ({trophy_leader[0].get('trophies',0)} 🏆)" if trophy_leader else "nobody yet"

        war = cr_api.get_current_war()
        war_str = ""
        if war and war.get("state") not in (None, "notInWar"):
            war_str = f"\n⚔️ **War is active** — state: {war.get('state', 'unknown')}. Let's go!"

        msg = (
            f"☀️ **Good morning, POAP KINGS!** Let's see where we stand.\n\n"
            f"👥 Members: **{count}/50** | 🏆 Clan Score: **{score:,}** | 🎁 Donations/week: **{donations:,}**\n\n"
            f"🔥 Top donors today: {donor_str}\n"
            f"👑 Trophy leader: {leader_str}"
            f"{war_str}\n\n"
            f"Keep the elixir flowing, kings! 🧪"
        )
        await channel.send(msg)
    except Exception as e:
        log.error(f"morning_post failed: {e}")


async def noon_post(channel):
    """12pm: Donation spotlight."""
    try:
        clan = cr_api.get_clan()
        members = clan.get("memberList", [])
        top = _top_donors(members, 1)
        if not top:
            return
        mvp = top[0]
        total = sum(m.get("donations", 0) for m in members)

        msg = (
            f"🎁 **Midday Donation Report** — POAP KINGS checking in!\n\n"
            f"Big shoutout to **{mvp['name']}** leading the charge with **{mvp.get('donations',0)} cards** donated this week! 🙌\n"
            f"The whole clan has moved **{total:,} cards** — that's how we support each other.\n\n"
            f"Keep donating and keep climbing! 🧪"
        )
        await channel.send(msg)
    except Exception as e:
        log.error(f"noon_post failed: {e}")


async def afternoon_post(channel):
    """5pm: Trophy leaderboard."""
    try:
        clan = cr_api.get_clan()
        members = clan.get("memberList", [])
        top5 = _top_trophies(members, 5)

        lines = "\n".join(
            f"{i+1}. **{m['name']}** — {m.get('trophies',0):,} 🏆 ({m.get('role','Member')})"
            for i, m in enumerate(top5)
        )

        war = cr_api.get_current_war()
        war_str = ""
        if war and war.get("state") not in (None, "notInWar"):
            participants = war.get("clan", {}).get("participants", [])
            fame = war.get("clan", {}).get("fame", 0)
            unused = [p["name"] for p in participants if p.get("decksUsedToday", 0) == 0]
            used = len(participants) - len(unused)
            war_str = f"\n\n⚔️ **River Race:** {fame:,} fame | {used}/{len(participants)} battled today"
            if unused:
                nudge = ", ".join(unused[:3])
                war_str += f" — still need to battle: **{nudge}**{'...' if len(unused) > 3 else ''}"

        msg = (
            f"🏆 **Afternoon Trophy Report** — POAP KINGS leaderboard!\n\n"
            f"{lines}"
            f"{war_str}\n\n"
            f"Push those trophies! 🧪"
        )
        await channel.send(msg)
    except Exception as e:
        log.error(f"afternoon_post failed: {e}")


async def evening_post(channel):
    """9pm: War wrap + clan health."""
    try:
        clan = cr_api.get_clan()
        members = clan.get("memberList", [])

        # Find inactive members
        inactive = [m for m in members if m.get("donations", 0) < 20 and _last_seen_days(m) >= 3]

        war = cr_api.get_current_war()
        war_str = "No active war right now — rest up and recharge your elixir. ⚗️"
        if war and war.get("state") not in (None, "notInWar"):
            war_str = f"⚔️ War is **{war.get('state','active')}** — make sure you've used your battle deck!"

        health_str = ""
        if inactive:
            names = ", ".join(m["name"] for m in inactive[:3])
            health_str = f"\n\n👀 Heads up leadership — **{names}** have been quiet lately. Worth a check-in."

        msg = (
            f"🌙 **Evening Wrap — POAP KINGS** 👑\n\n"
            f"{war_str}"
            f"{health_str}\n\n"
            f"Great effort today, everyone. See you on the arena! 🧪"
        )
        await channel.send(msg)
    except Exception as e:
        log.error(f"evening_post failed: {e}")
