import asyncio
import os
from datetime import datetime, timezone

import cr_api
import db
import elixir_agent
from runtime import status as runtime_status

__all__ = [
    "_build_roster_join_dates_report", "_build_kick_risk_report",
    "_build_top_war_contributors_report", "_build_status_report",
    "_build_schedule_report", "_DB_STATUS_MEMORY_TABLES",
    "_db_status_group_for_table", "_db_status_group_label",
    "_build_db_status_report", "_build_clan_status_report",
    "_build_war_status_report", "_build_clan_status_short_report",
    "_build_help_report", "_build_weekly_clanops_review",
    "_build_weekly_clan_recap_context", "_load_live_clan_context",
]

from runtime.helpers._common import (
    _canon_tag,
    _chicago,
    _fmt_bytes,
    _fmt_iso_short,
    _fmt_num,
    _fmt_relative,
    _format_relative_join_age,
    _job_next_runs,
    _join_member_bits,
    _log,
    _member_label,
    _recent_join_display_rows,
    _runtime_app,
    _schedule_specs,
    _scheduler,
    _status_badge,
    _with_leader_ping,
)


def _build_roster_join_dates_report():
    members = db.list_members()
    lines = ["**Clan Roster + Join Dates**"]
    for index, member in enumerate(members, start=1):
        name = member.get("current_name") or member.get("member_name") or member.get("player_tag") or "Unknown"
        role = member.get("role") or "member"
        joined = member.get("joined_date")
        suffix = f"joined {joined}" if joined else "join date not tracked yet"
        lines.append(f"{index}. {name} ({role}) — {suffix}")
    return "\n".join(lines)


def _build_kick_risk_report():
    risk = db.get_members_at_risk(
        inactivity_days=7,
        min_donations_week=0,
        require_war_participation=False,
        tenure_grace_days=0,
    )
    members = (risk or {}).get("members") or []
    lines = ["**Kick Risk (Inactive 7+ Days)**"]
    if not members:
        lines.append("No active members are currently over the 7-day inactivity threshold.")
        return "\n".join(lines)

    for member in members:
        name = _member_label(member)
        inactive_reason = next(
            (reason for reason in (member.get("reasons") or []) if reason.get("type") == "inactive"),
            None,
        )
        detail = inactive_reason.get("detail") if inactive_reason else "inactive 7+ days"
        lines.append(f"- {name} — {detail}")
    return "\n".join(lines)


def _build_top_war_contributors_report(limit=5):
    summary = db.get_war_season_summary(top_n=limit)
    if not summary:
        return "**Top War Contributors**\nNo current war season data is available yet."

    season_id = summary.get("season_id")
    contributors = summary.get("top_contributors") or []
    lines = [f"**Top War Contributors (Season {season_id})**"]
    if not contributors:
        lines.append("No war contributor data is available yet for this season.")
        return "\n".join(lines)

    for index, member in enumerate(contributors, start=1):
        name = _member_label(member)
        fame = member.get("total_fame", 0)
        races = member.get("races_played", 0)
        lines.append(f"{index}. {name} — {fame:,} fame across {races} race(s)")
    return "\n".join(lines)


def _build_status_report():
    runtime = runtime_status.snapshot()
    data = db.get_system_status()
    api = runtime["api"]
    llm = runtime["llm"]
    roster = data.get("roster_summary") or {}
    freshness = data.get("freshness") or {}
    endpoint_bits = []
    for item in (data.get("raw_payloads_by_endpoint") or [])[:4]:
        endpoint_bits.append(f"{item['endpoint']}={item['count']}")
    endpoint_summary = ", ".join(endpoint_bits) or "none"
    scheduler = _scheduler()
    scheduler_badge = "🟢" if scheduler.running else "🔴"
    discord_badge = "🟢" if runtime["env"]["has_discord_token"] else "🔴"
    claude_env_badge = "🟢" if runtime["env"]["has_claude_api_key"] else "🔴"
    cr_env_badge = "🟢" if runtime["env"]["has_cr_api_key"] else "🔴"
    schema_display = data.get("schema_display") or f"v{data.get('schema_version')}"
    memory = data.get("contextual_memory") or {}
    vec_badge = "🟢" if memory.get("sqlite_vec_enabled") else "🟡"
    lines = [
        "**Elixir Status**",
        f"🏷️ Release: `{elixir_agent.RELEASE_LABEL}`",
        f"🤖 Build: `{elixir_agent.BUILD_HASH}`",
        f"⏱️ Uptime: {_fmt_relative(runtime.get('started_at'))} (since {_fmt_iso_short(runtime.get('started_at'))})",
        f"{scheduler_badge} Scheduler: {'running' if scheduler.running else 'stopped'}",
        f"🗄️ DB: `{os.path.basename(data.get('db_path') or 'n/a')}` | schema {schema_display} | {_fmt_bytes(data.get('db_size_bytes'))} | active members {roster.get('active_members', 0)}/50",
        f"🧾 Data freshness: roster {_fmt_relative(freshness.get('member_state_at'))}, profiles {_fmt_relative(freshness.get('player_profile_at'))}, battles {_fmt_relative(freshness.get('battle_fact_at'))}, war {_fmt_relative(freshness.get('war_state_at'))}",
        f"📊 Data counts: raw payloads {data.get('counts', {}).get('raw_payload_count', 0)}, battle facts {data.get('counts', {}).get('battle_fact_count', 0)}, messages {data.get('counts', {}).get('message_count', 0)}, discord links {data.get('counts', {}).get('discord_links', 0)}",
        f"📥 Raw ingest: latest {((data.get('latest_raw_payload') or {}).get('endpoint') or 'n/a')} @ {_fmt_relative((data.get('latest_raw_payload') or {}).get('fetched_at'))}; endpoints {endpoint_summary}",
        f"🎯 Player intel backlog: {data.get('stale_player_intel_targets', 0)} stale target(s)",
        f"🧠 Context memory: {memory.get('total', 0)} total ({memory.get('leader_notes', 0)} leader / {memory.get('inferences', 0)} inference / {memory.get('system_notes', 0)} system) | latest {_fmt_relative(memory.get('latest_memory_at'))} | vec {vec_badge}",
        f"{_status_badge(api.get('last_ok'))} CR API: last {(api.get('last_endpoint') or 'n/a')} ({api.get('last_entity_key') or '-'}) {_fmt_relative(api.get('last_call_at'))}; status {api.get('last_status_code') or 'n/a'}; {'ok' if api.get('last_ok') else 'error' if api.get('last_ok') is not None else 'n/a'}; {api.get('last_duration_ms') or 'n/a'}ms; total {api.get('call_count', 0)} calls / {api.get('error_count', 0)} errors / {api.get('consecutive_error_count', 0)} consecutive failures",
        f"{_status_badge(llm.get('last_ok'))} Claude: last {(llm.get('last_workflow') or 'n/a')} via {(llm.get('last_model') or 'n/a')} {_fmt_relative(llm.get('last_call_at'))}; {'ok' if llm.get('last_ok') else 'error' if llm.get('last_ok') is not None else 'n/a'}; {llm.get('last_duration_ms') or 'n/a'}ms; tokens p/c/t {llm.get('last_prompt_tokens') or 'n/a'}/{llm.get('last_completion_tokens') or 'n/a'}/{llm.get('last_total_tokens') or 'n/a'}; cache w/r {llm.get('last_cache_creation_tokens') or 'n/a'}/{llm.get('last_cache_read_tokens') or 'n/a'}; total {llm.get('call_count', 0)} calls / {llm.get('error_count', 0)} errors",
        f"🔐 Env: Discord {discord_badge}, Claude {claude_env_badge}, CR {cr_env_badge}",
    ]
    role_status = _runtime_app()._member_role_grant_status()
    if role_status["configured"]:
        member_role_badge = "🟢" if role_status["ok"] else "🔴"
        lines.append(
            f"{member_role_badge} Member role auto-grant: {'ready' if role_status['ok'] else role_status['reason']} "
            f"(bot top {role_status.get('bot_top_role_position') if role_status.get('bot_top_role_position') is not None else 'n/a'} / "
            f"member {role_status.get('member_role_position') if role_status.get('member_role_position') is not None else 'n/a'} / "
            f"manage_roles {role_status.get('manage_roles')})"
        )
    if data.get("latest_signal"):
        lines.append(
            f"📣 Latest signal log: {data['latest_signal']['signal_type']} on {data['latest_signal']['signal_date']}"
        )
    if data.get("current_season_id") is not None:
        lines.append(f"🏁 Current war season id: {data['current_season_id']}")
    return "\n".join(lines)


def _build_schedule_report():
    next_runs = {item["id"]: item["next_run"] for item in _job_next_runs()}
    scheduler = _scheduler()
    scheduler_badge = "🟢" if scheduler.running else "🔴"
    lines = [
        "**Elixir Schedule**",
        f"{scheduler_badge} Scheduler: {'running' if scheduler.running else 'stopped'} | timezone America/Chicago",
    ]
    current_owner = None
    for spec in _schedule_specs():
        if spec["owner_subagent"] != current_owner:
            current_owner = spec["owner_subagent"]
            lines.append(f"")
            lines.append(f"**{current_owner}**")
        deliveries = "; ".join(spec["delivery_targets"])
        lines.append(
            f"- `{spec['activity_key']}` via `{spec['job_function']}`: {spec['schedule']} "
            f"Next run: {next_runs.get(spec['job_id'], 'n/a')} "
            f"Deliveries: {deliveries}"
        )
    return "\n".join(lines)


_DB_STATUS_MEMORY_TABLES = {
    "channel_state",
    "clan_memories",
    "clan_memory_audit",
    "clan_memory_embeddings",
    "clan_memory_index_status",
    "clan_memory_links",
    "clan_memory_tag_links",
    "clan_memory_tags",
    "clan_memory_versions",
    "conversation_threads",
    "memory_episodes",
    "memory_facts",
    "messages",
}


def _db_status_group_for_table(table_name: str) -> str:
    if table_name in _DB_STATUS_MEMORY_TABLES:
        return "memory"
    if (table_name or "").startswith("war_"):
        return "war"
    return "clan"


def _db_status_group_label(group: str) -> str:
    return {
        "clan": "Clan",
        "war": "War",
        "memory": "Memory",
    }.get(group, group.title())


def _build_db_status_report(group: str | None = None):
    data = db.get_database_status()
    requested_group = (group or "").strip().lower() or None
    tables = data.get("tables") or []
    lines = [
        "**Elixir DB Status**",
        (
            f"- File: `{os.path.basename(data.get('db_path') or 'n/a')}` | schema v{data.get('schema_version')} | "
            f"size {_fmt_bytes(data.get('db_size_bytes'))} | WAL {_fmt_bytes(data.get('wal_size_bytes'))} | "
            f"SHM {_fmt_bytes(data.get('shm_size_bytes'))}"
        ),
        (
            f"- Storage: page size {_fmt_num(data.get('page_size'))} B | "
            f"pages {_fmt_num(data.get('page_count'))} | "
            f"free pages {_fmt_num(data.get('freelist_count'))} | "
            f"journal {data.get('journal_mode') or 'n/a'} | tables {_fmt_num(data.get('table_count'))}"
        ),
    ]

    if requested_group:
        group_tables = [table for table in tables if _db_status_group_for_table(table.get("name") or "") == requested_group]
        group_rows = sum(int(table.get("row_count") or 0) for table in group_tables)
        group_bytes = sum(int(table.get("approx_bytes") or 0) for table in group_tables)
        lines[0] = f"**Elixir DB Status | {_db_status_group_label(requested_group)}**"
        lines.append(
            f"- Group: {_fmt_num(len(group_tables))} tables | {_fmt_num(group_rows)} rows | {_fmt_bytes(group_bytes)}"
        )
        lines.append("- Tables:")
        for table in group_tables:
            lines.append(
                f"  {table.get('name')}: {_fmt_num(table.get('row_count'))} rows | {_fmt_bytes(table.get('approx_bytes'))}"
            )
        return "\n".join(lines)

    group_totals = {}
    grouped_tables = {}
    for table in tables:
        table_group = _db_status_group_for_table(table.get("name") or "")
        bucket = group_totals.setdefault(table_group, {"tables": 0, "rows": 0, "bytes": 0})
        bucket["tables"] += 1
        bucket["rows"] += int(table.get("row_count") or 0)
        bucket["bytes"] += int(table.get("approx_bytes") or 0)
        grouped_tables.setdefault(table_group, []).append(table)

    lines.append(
        "- Use `/elixir system storage` for the full rollup or "
        "`/elixir system storage view:<all|clan|war|memory>` for a focused section."
    )
    for table_group in ("clan", "war", "memory"):
        bucket = group_totals.get(table_group, {"tables": 0, "rows": 0, "bytes": 0})
        lines.append(
            f"- {_db_status_group_label(table_group)}: {_fmt_num(bucket['tables'])} tables | "
            f"{_fmt_num(bucket['rows'])} rows | {_fmt_bytes(bucket['bytes'])}"
        )
        tables_for_group = sorted(
            grouped_tables.get(table_group, []),
            key=lambda table: (-(int(table.get("approx_bytes") or 0)), table.get("name") or ""),
        )
        if not tables_for_group:
            continue
        lines.append("  Tables:")
        for table in tables_for_group:
            lines.append(
                f"  - {table.get('name')}: {_fmt_num(table.get('row_count'))} rows | {_fmt_bytes(table.get('approx_bytes'))}"
            )
    return "\n".join(lines)


def _build_clan_status_report(clan=None, war=None):
    clan = clan or {}
    war = war or {}
    roster = db.get_clan_roster_summary()
    members = db.list_members()
    war_status = db.get_current_war_status()
    season_summary = db.get_war_season_summary(top_n=3)
    at_risk = db.get_members_at_risk(require_war_participation=False)
    slumping = db.get_members_on_losing_streak(min_streak=3)
    recent_joins, recent_join_count = _recent_join_display_rows(clan)
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
            f"{len((at_risk or {}).get('members') or [])} at risk | {len(slumping or [])} on cold streaks | "
            f"{recent_join_count} joined in last 30d"
        )

    if deck_status and deck_status.get("total_participants"):
        lines.append(
            f"- War today: {len(deck_status.get('used_all_4') or [])} used all 4 decks | "
            f"{len(deck_status.get('used_some') or [])} used some | "
            f"{len(deck_status.get('used_none') or [])} unused"
        )

    if recent_joins:
        recent_joins_text = _join_member_bits(
            recent_joins,
            lambda member: f"{_member_label(member)} ({_format_relative_join_age(member.get('joined_date'))})",
            limit=5,
        )
        lines.append(
            f"- Recent joins: {recent_joins_text}"
        )

    if slumping:
        slumping_text = _join_member_bits(
            slumping,
            lambda member: f"{_member_label(member)} lost {member.get('current_streak')} straight",
        )
        lines.append(
            f"- Cold streaks: {slumping_text}"
        )

    if at_risk and at_risk.get("members"):
        lines.append(
            f"- At risk: {_join_member_bits(at_risk['members'], lambda member: _member_label(member))}"
        )

    if war and war.get("clans"):
        lines.append(f"- Live war feed: {len(war.get('clans') or [])} clans in current river race")

    return "\n".join(lines)


def _build_war_status_report(clan=None, war=None):
    clan = clan or {}
    war = war or {}
    war_status = db.get_current_war_status() or {}
    current_day = db.get_current_war_day_state() or {}
    season_id = current_day.get("season_id") if current_day else war_status.get("season_id")
    section_index = current_day.get("section_index") if current_day else war_status.get("section_index")
    week_summary = db.get_war_week_summary(season_id=season_id, section_index=section_index)
    season_summary = db.get_war_season_summary(season_id=season_id, top_n=3)
    recent_days = db.list_recent_war_day_summaries(limit=4)
    defense_status = db.get_latest_clan_boat_defense_status()

    clan_name = clan.get("name") or war_status.get("clan_name") or "Clan"
    lines = [f"**{clan_name} War Status**"]

    def _fame_today_label(member):
        return f"{_member_label(member)} {_fmt_num(member.get('fame_today') or 0)}"

    def _fame_total_label(member):
        return f"{_member_label(member)} {_fmt_num(member.get('fame') or 0)}"

    def _season_fame_label(member):
        return f"{_member_label(member)} {_fmt_num(member.get('total_fame') or 0)}"

    if war_status:
        live_bits = [
            f"state {war_status.get('war_state') or 'n/a'}",
            f"season {war_status.get('season_id')}" if war_status.get("season_id") is not None else None,
            f"week {war_status.get('week')}" if war_status.get("week") is not None else None,
            war_status.get("phase_display"),
            f"rank {war_status.get('race_rank')}" if war_status.get("race_rank") is not None else None,
            f"fame {_fmt_num(war_status.get('fame'))}",
            f"score {_fmt_num(war_status.get('clan_score'))}",
            f"period {_fmt_num(war_status.get('period_points'))}" if war_status.get("period_points") is not None else None,
            "finished yes" if war_status.get("race_completed") else None,
            f"finish {war_status.get('finish_time')}" if war_status.get("finish_time") else None,
            "completed early" if war_status.get("race_completed_early") else None,
            f"stakes {war_status.get('trophy_stakes_text')}" if war_status.get("trophy_stakes_known") and war_status.get("trophy_stakes_text") else None,
        ]
        lines.append(f"- Live: {' | '.join(bit for bit in live_bits if bit)}")
    else:
        lines.append("- Live: no stored war state yet.")

    if current_day:
        lines.append(
            f"- Clock: {current_day.get('phase_display') or current_day.get('phase') or 'current war day'} | "
            f"time left {current_day.get('time_left_text') or 'n/a'} | key `{current_day.get('war_day_key') or 'n/a'}`"
        )
        if current_day.get("phase") == "battle":
            lines.append(
                f"- Engagement: {current_day.get('engaged_count', 0)} engaged | "
                f"{current_day.get('finished_count', 0)} finished all 4 | "
                f"{current_day.get('untouched_count', 0)} untouched | "
                f"{current_day.get('total_participants', 0)} tracked"
            )
            lines.append(
                f"- Leaders today: {_join_member_bits(current_day.get('top_fame_today') or [], _fame_today_label, limit=5)}"
            )
            lines.append(
                f"- Waiting on: {_join_member_bits(current_day.get('used_none') or [], lambda member: _member_label(member), limit=6)}"
            )
        else:
            defense_bits = []
            if defense_status:
                if defense_status.get("num_defenses_remaining") is not None:
                    defense_bits.append(f"defenses remaining {_fmt_num(defense_status.get('num_defenses_remaining'))}")
                if defense_status.get("progress_earned_from_defenses") is not None:
                    defense_bits.append(f"defense points {_fmt_num(defense_status.get('progress_earned_from_defenses'))}")
                if defense_status.get("phase_display"):
                    defense_bits.append(f"latest logged {defense_status.get('phase_display')}")
            lines.append(
                "- Practice focus: boat defenses matter before battle days."
                + (f" Latest defense log: {' | '.join(defense_bits)}" if defense_bits else "")
            )

    if week_summary:
        race = week_summary.get("race") or {}
        top_participants = week_summary.get("top_participants") or []
        day_summaries = week_summary.get("day_summaries") or []
        if race:
            lines.append(
                f"- This week: rank {_fmt_num(race.get('our_rank'))}/{_fmt_num(race.get('total_clans'))} | "
                f"fame {_fmt_num(race.get('our_fame'))} | participants {_fmt_num(week_summary.get('participant_count'))} | "
                f"top {_join_member_bits(top_participants, _fame_total_label, limit=3)}"
            )
        elif day_summaries:
            lines.append(
                f"- This week so far: {len(day_summaries)} tracked war day(s) | "
                f"top {_join_member_bits(top_participants, _fame_total_label, limit=3)}"
            )

    if recent_days:
        lines.append(
            "- Recent war days: "
            + " | ".join(
                (
                    f"{day.get('phase_display')}: {day.get('engaged_count', 0)} engaged, "
                    f"{day.get('finished_count', 0)} finished, "
                    f"leader {(_member_label((day.get('top_fame_today') or [{}])[0]) if day.get('top_fame_today') else 'none')}"
                )
                if day.get("phase") == "battle"
                else f"{day.get('phase_display')}: tracked"
                for day in recent_days[:3]
            )
        )

    if season_summary:
        lines.append(
            f"- This season: {season_summary.get('races', 0)} race(s) | total fame {_fmt_num(season_summary.get('total_clan_fame'))} | "
            f"fame/member {_fmt_num(season_summary.get('fame_per_active_member'), 2)} | "
            f"top {_join_member_bits(season_summary.get('top_contributors') or [], _season_fame_label, limit=3)}"
        )
        lines.append(
            f"- Season watch: {len(season_summary.get('nonparticipants') or [])} with no war participation yet"
        )

    if war and war.get("clans"):
        lines.append(f"- Live feed: {len(war.get('clans') or [])} clan(s) in the current river race")

    return "\n".join(lines)


def _build_clan_status_short_report(clan=None, war=None):
    clan = clan or {}
    roster = db.get_clan_roster_summary()
    war_status = db.get_current_war_status()
    season_summary = db.get_war_season_summary(top_n=2)
    at_risk = db.get_members_at_risk(require_war_participation=False)
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
            f"{' | finished' if war_status.get('race_completed') else ''}"
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
        f"- Watch: {len((at_risk or {}).get('members') or [])} at risk | {len(slumping or [])} on cold streaks"
    )
    return "\n".join(lines)


def _build_help_report(role: str) -> str:
    """Build the help response. The capability list is sourced from the intent
    registry so adding a new route in one place updates both the LLM router and
    this report.
    """
    from runtime.intent_registry import help_routes_for_workflow

    workflow = "clanops" if role == "clanops" else "interactive"
    routes = help_routes_for_workflow(workflow)

    capability_lines = []
    for r in routes:
        examples = r.get("examples") or []
        example_hint = f' — try "{examples[0]}"' if examples else ""
        capability_lines.append(f"- **{r['label']}**: {r['help_summary']}{example_hint}")

    if role == "clanops":
        operator_section = [
            "",
            "**Operator commands** (slash commands, not natural language)",
            "- Use `/elixir ...` for private operator commands in this channel.",
            "- Use `@Elixir do ...` when you want the command and result to stay public.",
            "- System: `/elixir system status`, `/elixir system storage`, `/elixir system schedule`.",
            "- Clan: `/elixir clan status`, `/elixir clan war`, `/elixir clan members`.",
            "- Member: `/elixir member show`, `/elixir member verify-discord`, `/elixir member set`, `/elixir member clear`.",
            "- Signal: `/elixir signal show`, `/elixir signal publish-pending`.",
            "- Activity: `/elixir activity list`, `/elixir activity show`, `/elixir activity run`.",
            "- Integration: `/elixir integration list`, `/elixir integration poap-kings status`, `/elixir integration poap-kings publish`.",
            "- Public examples: `@Elixir do clan status`, `@Elixir do member show \"weird name\"`.",
        ]
        return "\n".join(
            ["**Elixir Help — ClanOps**", "", "**What I can help with**"]
            + capability_lines
            + operator_section
        )

    return "\n".join(
        [
            "**Elixir Help — Interactive**",
            "",
            "- Mention me in this channel when you want help: `@Elixir help`.",
            "",
        ]
        + capability_lines
        + [
            "",
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
        (
            f"- **{clan_name}**: {roster.get('active_members', 0)}/50 active | "
            f"**elders** {composition.get('elders', 0)} | "
            f"**target elder band** {composition.get('target_elder_min', 0)}-{composition.get('target_elder_max', 0)} | "
            f"**remaining elder capacity** {composition.get('elder_capacity_remaining', 0)}"
        ),
    ]

    if recommended:
        lines.append(
            f"- ⬆️ **Promote now ({len(recommended)}):** "
            + _join_member_bits(recommended, lambda member: f"{_member_label(member)} — {_promotion_reason(member)}", limit=5)
        )
    else:
        lines.append("- ⬆️ **Promote now:** none this week")

    if borderline:
        lines.append(
            f"- ⚠️ **Borderline ({len(borderline)}):** "
            + _join_member_bits(borderline, lambda member: f"{_member_label(member)} — {_promotion_reason(member)}", limit=3)
        )

    if flagged:
        lines.append(
            f"- ⬇️ **Demotion/kick watch ({len(flagged)}):** "
            + _join_member_bits(flagged, lambda member: f"{_member_label(member)} — {_risk_reason(member)}", limit=5)
        )
    else:
        lines.append("- ⬇️ **Demotion/kick watch:** none right now")

    if nonparticipants:
        lines.append(
            f"- 💤 **No war decks this season ({len(nonparticipants)}):** "
            + _join_member_bits(nonparticipants, lambda member: _member_label(member), limit=5)
        )

    if war:
        clan_war = (war or {}).get("clan") or {}
        if clan_war:
            lines.append(
                f"- 🚤 **Current river race:** fame {_fmt_num(clan_war.get('fame'))} | repair {_fmt_num(clan_war.get('repairPoints'))} | score {_fmt_num(clan_war.get('clanScore'))}"
            )

    return _with_leader_ping("\n".join(lines))


def _build_weekly_clan_recap_context(clan=None, war=None):
    clan = clan or {}
    war = war or {}
    summary = db.get_weekly_digest_summary(days=7)
    try:
        clan_trend_summary = db.build_clan_trend_summary_context(days=30, window_days=7)
    except Exception as exc:
        _log().warning("Weekly recap clan trend summary unavailable: %s", exc)
        clan_trend_summary = ""
    roster = summary.get("roster") or {}
    war_score_trend = summary.get("war_score_trend") or {}
    season_summary = summary.get("war_season_summary") or {}
    clan_name = clan.get("name") or "POAP KINGS"
    clan_tag = clan.get("tag") or "#J2RGCRVG"

    lines = [
        "=== WEEKLY CLAN RECAP SNAPSHOT ===",
        f"clan_name: {clan_name}",
        f"clan_tag: {clan_tag}",
        f"window_days: {summary.get('window_days', 7)}",
    ]

    # ── STORY BEATS (the week's narrative — lead with this) ──────────────

    lines.append("")
    lines.append("=== STORY BEATS (the week's narrative — lead with this) ===")

    if war_score_trend:
        lines.append(
            "war_trend: "
            f"direction {war_score_trend.get('direction') or 'unknown'} | "
            f"score_change {_fmt_num(war_score_trend.get('score_change'))} | "
            f"trophy_change_total {_fmt_num(war_score_trend.get('trophy_change_total'))} | "
            f"races {war_score_trend.get('races') or 0} | "
            f"avg_rank {_fmt_num(war_score_trend.get('avg_rank'), 2)} | "
            f"avg_fame {_fmt_num(war_score_trend.get('avg_fame'), 2)}"
        )

    trending_war = (summary.get("trending_war_contributors") or {}).get("members") or []
    if trending_war:
        lines.append(
            "war momentum leaders: "
            + _join_member_bits(
                trending_war,
                lambda member: f"{_member_label(member)} trend {_fmt_num(member.get('fame_delta') or 0)} fame",
                limit=5,
            )
        )

    trophy_risers = summary.get("trophy_risers") or []
    if trophy_risers:
        lines.append(
            "biggest trophy rises: "
            + _join_member_bits(
                trophy_risers,
                lambda member: (
                    f"{_member_label(member)} {member.get('change', 0):+,.0f} "
                    f"({_fmt_num(member.get('old_trophies'))} -> {_fmt_num(member.get('new_trophies'))})"
                ),
                limit=5,
            )
        )

    trophy_drops = summary.get("trophy_drops") or []
    if trophy_drops:
        lines.append(
            "notable trophy slides: "
            + _join_member_bits(
                trophy_drops,
                lambda member: (
                    f"{_member_label(member)} {member.get('change', 0):+,.0f} "
                    f"({_fmt_num(member.get('old_trophies'))} -> {_fmt_num(member.get('new_trophies'))})"
                ),
                limit=3,
            )
        )

    hot_streaks = summary.get("hot_streaks") or []
    if hot_streaks:
        lines.append(
            "battle pulse heaters: "
            + _join_member_bits(
                hot_streaks,
                lambda member: f"{_member_label(member)} won {member.get('current_streak')} straight ({member.get('summary')})",
                limit=5,
            )
        )

    progression = summary.get("progression_highlights") or []
    if progression:
        lines.append("")
        lines.append("=== PLAYER PROGRESSION HIGHLIGHTS ===")
        for member in progression[:8]:
            bits = []
            if member.get("level_gain"):
                bits.append(f"King Level +{member['level_gain']}")
            if member.get("pol_league_gain"):
                bits.append(f"Path of Legend +{member['pol_league_gain']} league(s)")
            if member.get("best_trophies_gain"):
                bits.append(f"best trophies +{_fmt_num(member['best_trophies_gain'])}")
            if member.get("trophies_change"):
                bits.append(f"current trophies {member['trophies_change']:+,}")
            if member.get("wins_gain"):
                bits.append(f"career wins +{_fmt_num(member['wins_gain'])}")
            if member.get("favorite_card"):
                bits.append(f"favorite card {member['favorite_card']}")
            lines.append(f"- {_member_label(member)} | " + " | ".join(bits))

    recent_joins = summary.get("recent_joins") or []
    if recent_joins:
        lines.append(
            "recent joins this week: "
            + _join_member_bits(
                recent_joins,
                lambda member: f"{_member_label(member)} ({_format_relative_join_age(member.get('joined_date'))})",
                limit=5,
            )
        )

    # Tournament results from the past week
    try:
        recent_tournaments = db.get_recent_tournaments_for_recap(days=7)
        if recent_tournaments:
            lines.append("")
            lines.append("=== CLAN TOURNAMENTS THIS WEEK ===")
            for t in recent_tournaments:
                t_lines = [
                    f"- {t['name']} ({t['deck_selection'] or 'unknown format'}) | "
                    f"{t['participant_count']} participants | {t['battles_captured']} battles"
                ]
                if t.get("winner_name"):
                    t_lines[0] += f" | Winner: {t['winner_name']} ({t['winner_score']} wins)"
                if t.get("top_cards"):
                    t_lines.append(f"  top cards: {t['top_cards']}")
                lines.extend(t_lines)
    except Exception as exc:
        _log().debug("Weekly recap tournament section unavailable: %s", exc)

    # ── REFERENCE DATA (for framing, not narration) ──────────────────────

    lines.append("")
    lines.append("=== REFERENCE DATA (for framing, not narration) ===")

    lines.append(
        f"roster_now: {roster.get('active_members', 0)}/50 active | open_slots {roster.get('open_slots', 0)} | "
        f"avg_level {_fmt_num(roster.get('avg_exp_level'), 2)} | avg_trophies {_fmt_num(roster.get('avg_trophies'), 0)} | "
        f"weekly_donations {_fmt_num(roster.get('donations_week_total'), 0)}"
    )

    if season_summary:
        top_contributors = _join_member_bits(
            season_summary.get("top_contributors") or [],
            lambda member: f"{_member_label(member)} {_fmt_num(member.get('total_fame') or 0)} fame",
            limit=5,
        )
        lines.append(
            "current_war_season: "
            f"season {season_summary.get('season_id')} | races {season_summary.get('races') or 0} | "
            f"total_fame {_fmt_num(season_summary.get('total_clan_fame'))} | "
            f"fame_per_active_member {_fmt_num(season_summary.get('fame_per_active_member'), 2)} | "
            f"top_contributors {top_contributors or 'n/a'}"
        )

    recent_races = summary.get("recent_war_races") or []
    if recent_races:
        lines.append("")
        lines.append("recent river races:")
        for race in recent_races:
            lines.append(
                f"- season {race.get('season_id')} week {race.get('week')} | rank {race.get('our_rank')} / {race.get('total_clans')} | "
                f"fame {_fmt_num(race.get('our_fame'))} | trophy_change {_fmt_num(race.get('trophy_change'))} | "
                f"finished {race.get('created_date')}"
            )
            top_participants = race.get("top_participants") or []
            if top_participants:
                lines.append(
                    "  top participants: "
                    + _join_member_bits(
                        top_participants,
                        lambda member: f"{_member_label(member)} {_fmt_num(member.get('fame') or 0)} fame / {member.get('decks_used') or 0} decks",
                        limit=3,
                    )
                )
            standings = race.get("standings_preview") or []
            if standings:
                lines.append(
                    "  podium snapshot: "
                    + ", ".join(
                        f"#{item.get('rank')} {item.get('name')} ({_fmt_num(item.get('fame'))} fame)"
                        for item in standings if item.get("name")
                    )
                )

    if clan_trend_summary:
        lines.append("")
        lines.append(clan_trend_summary)

    top_donors = summary.get("top_donors") or []
    if top_donors:
        lines.append(
            "top donors right now: "
            + _join_member_bits(
                top_donors,
                lambda member: f"{_member_label(member)} {_fmt_num(member.get('donations_week') or 0)}",
                limit=5,
            )
        )

    if war:
        clan_war = (war.get("clan") or {})
        if clan_war:
            lines.append(
                "live war snapshot now: "
                f"fame {_fmt_num(clan_war.get('fame'))} | repair {_fmt_num(clan_war.get('repairPoints'))} | "
                f"score {_fmt_num(clan_war.get('clanScore'))} | participants {len(clan_war.get('participants') or [])}"
            )

    return "\n".join(lines)


async def _load_live_clan_context():
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
        _runtime_app()._clear_cr_api_failure_alert_if_recovered()
    except Exception:
        await _runtime_app()._maybe_alert_cr_api_failure("live clan refresh")
        raise
    member_list = clan.get("memberList") or []
    if member_list:
        previous_roster = await asyncio.to_thread(db.get_active_roster_map)
        previous_tags = {_canon_tag(tag) for tag in (previous_roster or {})}
        if previous_tags:
            today = datetime.now(_chicago()).date().isoformat()
            live_recent_joins = []
            for member in member_list:
                tag = _canon_tag(member.get("tag"))
                if not tag or tag in previous_tags:
                    continue
                live_recent_joins.append({
                    "player_tag": tag,
                    "tag": tag,
                    "current_name": member.get("name") or tag,
                    "name": member.get("name") or tag,
                    "member_ref": member.get("name") or tag,
                    "joined_date": today,
                })
            if live_recent_joins:
                clan["_elixir_recent_joins"] = sorted(
                    live_recent_joins,
                    key=lambda item: (item.get("current_name") or "").lower(),
                )
        await asyncio.to_thread(db.snapshot_members, member_list)
        await asyncio.to_thread(db.snapshot_clan_daily_metrics, clan)
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
    except Exception:
        await _runtime_app()._maybe_alert_cr_api_failure("live war refresh")
        war = {}
    if war:
        await asyncio.to_thread(db.upsert_war_current_state, war)
    return clan, war
