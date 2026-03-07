import asyncio
import os
from datetime import datetime, timezone

import cr_api
import db
import elixir_agent
import prompts
from runtime import app as _app
from runtime.app import (
    BOT_ROLE_ID,
    CHICAGO,
    CLANOPS_PROACTIVE_COOLDOWN_SECONDS,
    LEADER_ROLE_ID,
    bot,
    log,
    scheduler,
)
from runtime import status as runtime_status


async def _post_to_elixir(*args, **kwargs):
    return await _app._post_to_elixir(*args, **kwargs)

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


__all__ = [
    name for name in globals()
    if not name.startswith("__") and name not in {"_post_to_elixir"}
]
