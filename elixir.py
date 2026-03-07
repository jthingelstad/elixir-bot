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
import runtime_status

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
LEADER_ROLE_ID = _dc.get("leader_role", 0)
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
    """Post an entry's content to a configured Discord channel."""
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
    return f"channel:{channel.id}"


def _channel_conversation_scope(channel, discord_user_id) -> str:
    return f"channel_user:{channel.id}:{discord_user_id}"


def _is_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "status",
        "!status",
        "/status",
        "elixir status",
        "@elixir status",
        "health",
        "health check",
    }


def _clan_status_mode(text: str) -> str | None:
    normalized = " ".join((text or "").strip().lower().split())
    if normalized in {
        "clan status",
        "!clan-status",
        "/clan-status",
        "clan health",
        "clan health check",
        "poap kings status",
    }:
        return "full"
    if normalized in {
        "clan status short",
        "clan status brief",
        "!clan-status-short",
        "/clan-status-short",
        "clan health short",
        "poap kings status short",
    }:
        return "short"
    return None


def _is_help_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "help",
        "!help",
        "/help",
        "elixir help",
        "@elixir help",
        "what can you do",
        "what do you do",
    }


def _fmt_iso_short(value):
    if not value:
        return "n/a"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(CHICAGO).strftime("%Y-%m-%d %I:%M %p CT")
    except ValueError:
        return str(value)


def _fmt_relative(value):
    if not value:
        return "n/a"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _fmt_bytes(size):
    if size is None:
        return "n/a"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _fmt_num(value, digits=0):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if digits:
            return f"{value:,.{digits}f}"
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return f"{value:,}"


def _member_label(member):
    return (
        member.get("member_ref")
        or member.get("member_reference")
        or member.get("name")
        or member.get("member_name")
        or member.get("tag")
        or "unknown"
    )


def _join_member_bits(members, formatter, limit=3):
    if not members:
        return "none"
    return ", ".join(formatter(member) for member in members[:limit])


def _leader_role_mention():
    return f"<@&{LEADER_ROLE_ID}>" if LEADER_ROLE_ID else ""


def _with_leader_ping(content):
    mention = _leader_role_mention()
    if not mention or not content or mention in content:
        return content
    return f"{mention}\n{content}"


def _job_next_runs():
    items = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time.astimezone(CHICAGO).strftime("%Y-%m-%d %I:%M %p CT") if job.next_run_time else "n/a"
        items.append({"id": job.id, "next_run": next_run})
    return sorted(items, key=lambda item: item["id"])


async def _reply_text(message, content):
    if len(content) > 2000:
        for chunk in [content[i:i + 1990] for i in range(0, len(content), 1990)]:
            await message.reply(chunk)
    else:
        await message.reply(content)


def _build_status_report():
    runtime = runtime_status.snapshot()
    data = db.get_system_status()
    api = runtime["api"]
    openai = runtime["openai"]
    roster = data.get("roster_summary") or {}
    freshness = data.get("freshness") or {}
    jobs = runtime.get("jobs") or {}
    endpoint_bits = []
    for item in (data.get("raw_payloads_by_endpoint") or [])[:4]:
        endpoint_bits.append(f"{item['endpoint']}={item['count']}")
    endpoint_summary = ", ".join(endpoint_bits) or "none"
    lines = [
        "**Elixir Status**",
        f"- Build: `{elixir_agent.BUILD_HASH}`",
        f"- Uptime: {_fmt_relative(runtime.get('started_at'))} (since {_fmt_iso_short(runtime.get('started_at'))})",
        f"- Scheduler: {'running' if scheduler.running else 'stopped'}",
        f"- DB: `{os.path.basename(data.get('db_path') or 'n/a')}` | schema v{data.get('schema_version')} | {_fmt_bytes(data.get('db_size_bytes'))} | active members {roster.get('active_members', 0)}/50",
        f"- Data freshness: roster {_fmt_relative(freshness.get('member_state_at'))}, profiles {_fmt_relative(freshness.get('player_profile_at'))}, battles {_fmt_relative(freshness.get('battle_fact_at'))}, war {_fmt_relative(freshness.get('war_state_at'))}",
        f"- Data counts: raw payloads {data.get('counts', {}).get('raw_payload_count', 0)}, battle facts {data.get('counts', {}).get('battle_fact_count', 0)}, messages {data.get('counts', {}).get('message_count', 0)}, discord links {data.get('counts', {}).get('discord_links', 0)}",
        f"- Raw ingest: latest {((data.get('latest_raw_payload') or {}).get('endpoint') or 'n/a')} @ {_fmt_relative((data.get('latest_raw_payload') or {}).get('fetched_at'))}; endpoints {endpoint_summary}",
        f"- Player intel backlog: {data.get('stale_player_intel_targets', 0)} stale target(s)",
        f"- CR API: last {(api.get('last_endpoint') or 'n/a')} ({api.get('last_entity_key') or '-'}) {_fmt_relative(api.get('last_call_at'))}; status {api.get('last_status_code') or 'n/a'}; {'ok' if api.get('last_ok') else 'error' if api.get('last_ok') is not None else 'n/a'}; {api.get('last_duration_ms') or 'n/a'}ms; total {api.get('call_count', 0)} calls / {api.get('error_count', 0)} errors",
        f"- OpenAI: last {(openai.get('last_workflow') or 'n/a')} via {(openai.get('last_model') or 'n/a')} {_fmt_relative(openai.get('last_call_at'))}; {'ok' if openai.get('last_ok') else 'error' if openai.get('last_ok') is not None else 'n/a'}; {openai.get('last_duration_ms') or 'n/a'}ms; tokens p/c/t {openai.get('last_prompt_tokens') or 'n/a'}/{openai.get('last_completion_tokens') or 'n/a'}/{openai.get('last_total_tokens') or 'n/a'}; total {openai.get('call_count', 0)} calls / {openai.get('error_count', 0)} errors",
        f"- Env: Discord {'ok' if runtime['env']['has_discord_token'] else 'missing'}, OpenAI {'ok' if runtime['env']['has_openai_api_key'] else 'missing'}, CR {'ok' if runtime['env']['has_cr_api_key'] else 'missing'}",
    ]
    if data.get("latest_signal"):
        lines.append(
            f"- Latest signal log: {data['latest_signal']['signal_type']} on {data['latest_signal']['signal_date']}"
        )
    if data.get("current_season_id") is not None:
        lines.append(f"- Current war season id: {data['current_season_id']}")
    if jobs:
        lines.append("- Jobs:")
        for name in ("heartbeat", "player_intel_refresh", "site_data_refresh", "site_content_cycle", "clanops_weekly_review"):
            state = jobs.get(name) or {}
            next_run = next((item["next_run"] for item in _job_next_runs() if item["id"] == name), "n/a")
            last = state.get("last_success_at") or state.get("last_failure_at") or state.get("last_started_at")
            summary = state.get("last_summary") or state.get("last_error") or "n/a"
            lines.append(
                f"  - `{name}` next {next_run} | last {_fmt_relative(last)} | {summary}"
            )
    return "\n".join(lines)


def _build_clan_status_report(clan=None, war=None):
    clan = clan or {}
    war = war or {}
    roster = db.get_clan_roster_summary()
    members = db.list_members()
    war_status = db.get_current_war_status()
    season_summary = db.get_war_season_summary(top_n=3)
    at_risk = db.get_members_at_risk(require_war_participation=True)
    slumping = db.get_members_on_losing_streak(min_streak=3)
    recent_joins = db.list_recent_joins(days=30)
    deck_status = db.get_war_deck_status_today()

    clan_name = clan.get("name") or (war_status.get("clan_name") if war_status else None)
    clan_name = clan_name or "Clan"
    member_count = clan.get("members") or clan.get("memberCount") or roster.get("active_members") or 0
    open_slots = max(0, 50 - member_count)
    clan_score = clan.get("clanScore")
    war_trophies = clan.get("clanWarTrophies")
    required_trophies = clan.get("requiredTrophies")
    donations_total = clan.get("donationsPerWeek")
    if donations_total in (None, 0):
        donations_total = roster.get("donations_week_total")

    top_donors = sorted(
        members,
        key=lambda member: (member.get("donations_week") or 0, -(member.get("clan_rank") or 999)),
        reverse=True,
    )
    top_donors = [member for member in top_donors if (member.get("donations_week") or 0) > 0][:3]
    top_trophy_member = max(members, key=lambda member: member.get("trophies") or 0, default=None)

    lines = [
        f"**{clan_name} Status**",
        (
            f"- Roster: {member_count}/50 members | {open_slots} open | avg King Level {_fmt_num(roster.get('avg_exp_level'), 2)} "
            f"| avg trophies {_fmt_num(roster.get('avg_trophies'), 2)}"
        ),
        (
            f"- Ladder/Economy: clan score {_fmt_num(clan_score)} | war trophies {_fmt_num(war_trophies)} "
            f"| required trophies {_fmt_num(required_trophies)} | weekly donations {_fmt_num(donations_total)}"
        ),
    ]

    if top_trophy_member or top_donors:
        top_trophy_text = (
            f"{_member_label(top_trophy_member)} {_fmt_num(top_trophy_member.get('trophies'))}"
            if top_trophy_member else "n/a"
        )
        donor_text = _join_member_bits(
            top_donors,
            lambda member: f"{_member_label(member)} {_fmt_num(member.get('donations_week') or 0)}",
        )
        lines.append(f"- Standouts: top trophies {top_trophy_text} | top donors {donor_text}")

    if war_status:
        war_bits = [
            f"season {war_status.get('season_id')}" if war_status.get("season_id") is not None else None,
            f"week {war_status.get('week')}" if war_status.get("week") is not None else None,
            f"state {war_status.get('war_state') or 'n/a'}",
            f"rank {war_status.get('race_rank')}" if war_status.get("race_rank") is not None else None,
            f"fame {_fmt_num(war_status.get('fame'))}",
            f"repair {_fmt_num(war_status.get('repair_points'))}",
            f"score {_fmt_num(war_status.get('clan_score'))}",
        ]
        lines.append(f"- War now: {' | '.join(bit for bit in war_bits if bit)}")

    if season_summary:
        top_contributors = _join_member_bits(
            season_summary.get("top_contributors") or [],
            lambda member: f"{_member_label(member)} {_fmt_num(member.get('total_fame') or 0)}",
        )
        lines.append(
            f"- War season: {season_summary.get('races', 0)} races | total fame {_fmt_num(season_summary.get('total_clan_fame'))} "
            f"| fame/member {_fmt_num(season_summary.get('fame_per_active_member'), 2)} | top contributors {top_contributors}"
        )
        lines.append(
            f"- Watch list: {len(season_summary.get('nonparticipants') or [])} with no war decks this season | "
            f"{len((at_risk or {}).get('members') or [])} at risk | {len(slumping or [])} on losing streaks | "
            f"{len(recent_joins or [])} joined in last 30d"
        )

    if deck_status and deck_status.get("total_participants"):
        lines.append(
            f"- War today: {len(deck_status.get('used_all_4') or [])} used all 4 decks | "
            f"{len(deck_status.get('used_some') or [])} used some | "
            f"{len(deck_status.get('used_none') or [])} unused"
        )

    if recent_joins:
        lines.append(
            f"- Recent joins: {_join_member_bits(recent_joins, lambda member: _member_label(member))}"
        )

    if slumping:
        slumping_text = _join_member_bits(
            slumping,
            lambda member: f"{_member_label(member)} L{member.get('current_streak')}",
        )
        lines.append(
            f"- Slumping: {slumping_text}"
        )

    if at_risk and at_risk.get("members"):
        lines.append(
            f"- At risk: {_join_member_bits(at_risk['members'], lambda member: _member_label(member))}"
        )

    if war and war.get("clans"):
        lines.append(f"- Live war feed: {len(war.get('clans') or [])} clans in current river race")

    return "\n".join(lines)


def _build_clan_status_short_report(clan=None, war=None):
    clan = clan or {}
    roster = db.get_clan_roster_summary()
    war_status = db.get_current_war_status()
    season_summary = db.get_war_season_summary(top_n=2)
    at_risk = db.get_members_at_risk(require_war_participation=True)
    slumping = db.get_members_on_losing_streak(min_streak=3)

    clan_name = clan.get("name") or (war_status.get("clan_name") if war_status else None) or "Clan"
    member_count = clan.get("members") or clan.get("memberCount") or roster.get("active_members") or 0
    open_slots = max(0, 50 - member_count)
    lines = [
        f"**{clan_name} Status (Short)**",
        (
            f"- Roster: {member_count}/50 | open {open_slots} | avg level {_fmt_num(roster.get('avg_exp_level'), 1)} "
            f"| avg trophies {_fmt_num(roster.get('avg_trophies'), 0)}"
        ),
    ]
    if war_status:
        lines.append(
            f"- War: season {war_status.get('season_id') if war_status.get('season_id') is not None else 'n/a'} "
            f"| week {war_status.get('week') if war_status.get('week') is not None else 'n/a'} "
            f"| rank {war_status.get('race_rank') if war_status.get('race_rank') is not None else 'n/a'} "
            f"| fame {_fmt_num(war_status.get('fame'))}"
        )
    if season_summary:
        top_contributors = _join_member_bits(
            season_summary.get("top_contributors") or [],
            lambda member: f"{_member_label(member)} {_fmt_num(member.get('total_fame') or 0)}",
            limit=2,
        )
        lines.append(
            f"- Season: fame/member {_fmt_num(season_summary.get('fame_per_active_member'), 1)} | top {top_contributors}"
        )
    lines.append(
        f"- Watch: {len((at_risk or {}).get('members') or [])} at risk | {len(slumping or [])} slumping"
    )
    return "\n".join(lines)


def _build_help_report(role: str) -> str:
    if role == "clanops":
        return "\n".join(
            [
                "**Elixir Help — ClanOps**",
                "- Ask without mentioning me in this channel when you want operational input.",
                "- Direct commands: `status`, `clan status`, `clan status short`, `help`.",
                "- I can help with roster reviews, war participation, promotions, demotions, kicks, recent form, decks, donations, and member lookups.",
                "- I can resolve members by in-game name, tag, alias, or Discord handle.",
                "- If I have something useful to add to an ops discussion here, I may inject proactively.",
            ]
        )
    return "\n".join(
        [
            "**Elixir Help — Interactive**",
            "- Mention me in this channel when you want help: `@Elixir help`.",
            "- I can answer member questions about current trophies, league/arena, deck, signature cards, recent form, war decks left, war participation, and clan rank.",
            "- I can answer clan questions like who is in the clan, recent joins, donation leaders, and current war status.",
            "- I am read-only in interactive channels. I do not make admin decisions here.",
        ]
    )


def _build_weekly_clanops_review(clan=None, war=None):
    clan = clan or {}
    war = war or {}
    roster = db.get_clan_roster_summary()
    promotions = db.get_promotion_candidates()
    at_risk = db.get_members_at_risk(require_war_participation=True)
    season_summary = db.get_war_season_summary(top_n=3)

    clan_name = clan.get("name") or "Clan"
    composition = promotions.get("composition") or {}
    recommended = promotions.get("recommended") or []
    borderline = promotions.get("borderline") or []
    flagged = (at_risk or {}).get("members") or []
    nonparticipants = (season_summary or {}).get("nonparticipants") or []

    def _promotion_reason(member):
        bits = [
            f"{_fmt_num(member.get('donations') or 0)} donations",
            f"{_fmt_num(member.get('war_races_played') or 0)} war races",
        ]
        if member.get("tenure_days") is not None:
            bits.append(f"{member['tenure_days']}d tenure")
        if member.get("days_inactive") is not None:
            bits.append(f"seen {member['days_inactive']}d ago")
        return ", ".join(bits)

    def _risk_reason(member):
        reasons = member.get("reasons") or []
        if not reasons:
            return "needs review"
        return "; ".join(reason.get("detail") or reason.get("type") for reason in reasons[:3])

    lines = [
        "**Weekly ClanOps Review**",
        f"- {clan_name}: {roster.get('active_members', 0)}/50 active | elders {composition.get('elders', 0)} | target elder band {composition.get('target_elder_min', 0)}-{composition.get('target_elder_max', 0)} | remaining elder capacity {composition.get('elder_capacity_remaining', 0)}",
    ]

    if recommended:
        lines.append(
            f"- Promote now ({len(recommended)}): "
            + _join_member_bits(recommended, lambda member: f"{_member_label(member)} — {_promotion_reason(member)}", limit=5)
        )
    else:
        lines.append("- Promote now: none this week")

    if borderline:
        lines.append(
            f"- Borderline ({len(borderline)}): "
            + _join_member_bits(borderline, lambda member: f"{_member_label(member)} — {_promotion_reason(member)}", limit=3)
        )

    if flagged:
        lines.append(
            f"- Demotion/kick watch ({len(flagged)}): "
            + _join_member_bits(flagged, lambda member: f"{_member_label(member)} — {_risk_reason(member)}", limit=5)
        )
    else:
        lines.append("- Demotion/kick watch: none right now")

    if nonparticipants:
        lines.append(
            f"- No war decks this season ({len(nonparticipants)}): "
            + _join_member_bits(nonparticipants, lambda member: _member_label(member), limit=5)
        )

    if war:
        clan_war = (war or {}).get("clan") or {}
        if clan_war:
            lines.append(
                f"- Current river race: fame {_fmt_num(clan_war.get('fame'))} | repair {_fmt_num(clan_war.get('repairPoints'))} | score {_fmt_num(clan_war.get('clanScore'))}"
            )

    return _with_leader_ping("\n".join(lines))


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
    if result.get("event_type") != "channel_share":
        return
    share_content = result.get("share_content", "")
    if not share_content:
        return
    target_ref = result.get("share_channel") or "announcements"
    target = prompts.resolve_channel_reference(target_ref)
    if not target:
        log.warning("Unknown share target channel: %s", target_ref)
        return
    target_channel = bot.get_channel(target["id"])
    if not target_channel:
        return
    if target.get("role") == "arena_relay":
        share_content = _with_leader_ping(share_content)
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
    runtime_status.mark_job_start("heartbeat")
    # Check active hours
    now_chicago = datetime.now(CHICAGO)
    if not (HEARTBEAT_START_HOUR <= now_chicago.hour < HEARTBEAT_END_HOUR):
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
                runtime_status.mark_job_success("heartbeat", f"{len(other_signals)} signal(s), no post")
                return
            await _post_to_elixir(channel, result)
            content = result.get("content", result.get("summary", ""))
            if content:
                await asyncio.to_thread(
                    db.save_message,
                    _channel_scope(channel), "assistant", content,
                    channel_id=channel.id,
                    channel_name=getattr(channel, "name", None),
                    channel_kind=str(channel.type),
                    workflow="observation",
                    event_type=result.get("event_type"),
                )
            log.info("Posted observation: %s", result.get("summary"))

        runtime_status.mark_job_success("heartbeat", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("heartbeat", str(e))


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "8"))       # 8am Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "20"))  # 8pm Chicago
PLAYER_INTEL_REFRESH_HOURS = int(os.getenv("PLAYER_INTEL_REFRESH_HOURS", "6"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "12"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "6"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))


async def _site_data_refresh():
    """Morning job — refresh clan data and roster on poapkings.com."""
    runtime_status.mark_job_start("site_data_refresh")
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            log.error("Site data refresh: CR API failed")
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
    """Evening job — generate all site content and refresh data."""
    runtime_status.mark_job_start("site_content_cycle")
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
        runtime_status.mark_job_success("site_content_cycle", "content updated")
    except Exception as e:
        log.error("Site content cycle error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_content_cycle", str(e))


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
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
            await asyncio.sleep(0.3)
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
                content = result.get("content", result.get("summary", ""))
                if content:
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", content,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type=result.get("event_type"),
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

@bot.event
async def on_ready():
    log.info("Elixir online as %s 🧪", bot.user)
    prompts.ensure_valid_discord_channel_config()
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
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_clanops_weekly_review())
            ),
            "cron",
            day_of_week=CLANOPS_WEEKLY_REVIEW_DAY,
            hour=CLANOPS_WEEKLY_REVIEW_HOUR,
            minute=0,
            id="clanops_weekly_review",
        )
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago), "
                 "site data refresh at %dam, content cycle at %dpm, player intel refresh every %dh, clanops review %s at %02d:00",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 SITE_DATA_HOUR, SITE_CONTENT_HOUR, PLAYER_INTEL_REFRESH_HOURS,
                 CLANOPS_WEEKLY_REVIEW_DAY, CLANOPS_WEEKLY_REVIEW_HOUR)
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
    conversation_scope = _channel_conversation_scope(message.channel, message.author.id)
    raw_question = _strip_bot_mentions(message.content) if mentioned else message.content.strip()

    # Non-responsive singleton channels are outbound only.
    if not channel_config.get("respond_allowed", True):
        return

    if role == "onboarding" and not mentioned:
        return

    clan_status_mode = _clan_status_mode(raw_question)

    if role in {"clanops", "interactive"} and _is_help_request(raw_question):
        help_content = await asyncio.to_thread(_build_help_report, role)
        workflow_name = "clanops" if role == "clanops" else workflow
        event_type = f"{role}_help"
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            discord_message_id=message.id,
        )
        await _reply_text(message, help_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            help_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            event_type=event_type,
        )
        return

    if role == "clanops" and (_is_status_request(raw_question) or clan_status_mode):
        clan = {}
        war = {}
        if clan_status_mode:
            try:
                clan, war = await _load_live_clan_context()
            except Exception as exc:
                log.warning("Clan status refresh failed: %s", exc)
        if clan_status_mode == "full":
            report_builder = _build_clan_status_report
            report_args = (clan, war)
            event_type = "clan_status_report"
        elif clan_status_mode == "short":
            report_builder = _build_clan_status_short_report
            report_args = (clan, war)
            event_type = "clan_status_short_report"
        else:
            report_builder = _build_status_report
            report_args = ()
            event_type = "status_report"
        status_content = await asyncio.to_thread(report_builder, *report_args)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await _reply_text(message, status_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            status_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=event_type,
        )
        return

    proactive = role == "clanops" and not mentioned
    if proactive and not _clanops_cooldown_elapsed(message.channel.id):
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
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
                question = raw_question
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
                    await message.reply("Having a hiccup — try again in a sec! 🧪")
                    return
                content = result.get("content", result.get("summary", ""))
                await _reply_text(message, content)
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
                question = raw_question
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    conversation_scope,
                    CHANNEL_CONVERSATION_LIMIT,
                )
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )

                await asyncio.to_thread(
                    db.save_message,
                    conversation_scope,
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
                        conversation_scope,
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

                await _reply_text(message, content)
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
