import asyncio
import re
import os
from datetime import datetime, timezone

import cr_api
import db
import elixir_agent
import prompts
from runtime.activities import schedule_specs_from_registry
from runtime import status as runtime_status

BOT_ROLE_ID = None
CHICAGO = None
LEADER_ROLE_ID = None
bot = None
log = None
scheduler = None


def _runtime_app():
    from runtime import app as app_module
    return app_module


def _bot():
    return bot if bot is not None else _runtime_app().bot


def _scheduler():
    return scheduler if scheduler is not None else _runtime_app().scheduler


def _log():
    return log if log is not None else _runtime_app().log


def _chicago():
    return CHICAGO if CHICAGO is not None else _runtime_app().CHICAGO


def _leader_role_id():
    return LEADER_ROLE_ID if LEADER_ROLE_ID is not None else _runtime_app().LEADER_ROLE_ID


def _bot_role_id():
    return BOT_ROLE_ID if BOT_ROLE_ID is not None else _runtime_app().BOT_ROLE_ID


async def _post_to_elixir(*args, **kwargs):
    return await _runtime_app()._post_to_elixir(*args, **kwargs)


def _pick_resolved_member(matches):
    if not matches:
        return None
    exactish = [item for item in matches if item.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0]
    if len(matches) == 1:
        return matches[0]
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top
    return None


def _rewrite_member_refs_in_text(text: str, replacements: list[tuple[str, str]]) -> str:
    updated = text or ""
    if not updated:
        return updated
    for alias, ref in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if not alias or not ref or ref == alias:
            continue
        pattern = re.compile(
            rf"(?<![\w<]){re.escape(alias)}(?![\w>])(?!\s*\((?:<@|@))",
            re.IGNORECASE,
        )
        updated = pattern.sub(ref, updated)
    return updated


async def _apply_member_refs_to_result(result: dict | None):
    # Mention injection disabled — return result unchanged.
    # Data (discord links, identities) is preserved; we just no longer
    # rewrite player names into Discord <@id> pings in bot output.
    return result

def _match_clan_member(nickname):
    """Match a Discord nickname to a clan member. Returns (tag, name) or None.

    Uses Elixir's member resolver but only accepts high-confidence exact matches.
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


def _is_schedule_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "schedule",
        "schedules",
        "!schedule",
        "/schedule",
        "job schedule",
        "job schedules",
        "elixir schedule",
        "@elixir schedule",
    }


def _is_db_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "db status",
        "database status",
        "!db-status",
        "/db-status",
        "elixir db status",
        "@elixir db status",
    }


def _is_clan_list_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "clan list",
        "list clan",
        "clan roster",
        "list roster",
        "member list",
        "list members",
        "show clan members",
        "show roster",
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


def _is_war_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "war status",
        "!war-status",
        "/war-status",
        "river race status",
        "war health",
        "current war status",
        "poap kings war status",
    }


def _extract_profile_target(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        tokens = re.findall(r'''(?:"([^"]+)")|(?:'([^']+)')|(\S+)''', raw)
        flat = [a or b or c for a, b, c in tokens]
    except Exception:
        flat = raw.split()
    lowered = [token.lower() for token in flat]
    if len(flat) >= 2 and lowered[0] == "profile":
        return " ".join(flat[1:]).strip() or None
    if len(flat) >= 3 and lowered[0] == "member" and lowered[1] == "profile":
        return " ".join(flat[2:]).strip() or None
    return None


def _is_roster_join_dates_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who are the members of the clan and when did they join?",
        "who are the members of the clan and when did they join",
        "who is in the clan and when did they join?",
        "who is in the clan and when did they join",
        "list the clan members and when they joined",
        "list clan members and join dates",
        "show clan members and join dates",
        "roster with join dates",
    }


def _is_kick_risk_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who is at risk of being kicked based on participation thresholds?",
        "who is at risk of being kicked based on participation thresholds",
        "who is at risk of being kicked?",
        "who is at risk of being kicked",
        "who is at kick risk?",
        "who is at kick risk",
        "who should be kicked for inactivity?",
        "who should be kicked for inactivity",
        "which members are inactive for more than 1 week?",
        "which members are inactive for more than 1 week",
        "which members are inactive for more than a week?",
        "which members are inactive for more than a week",
    }


def _is_top_war_contributors_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who are the top 5 contributors to clan wars this season?",
        "who are the top 5 contributors to clan wars this season",
        "who are the top war contributors this season?",
        "who are the top war contributors this season",
        "top war contributors this season",
        "show top war contributors this season",
        "who are the top contributors to clan wars this season?",
        "who are the top contributors to clan wars this season",
    }


def _is_member_deck_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if "deck" not in normalized:
        return False
    if "current deck" in normalized:
        return True
    explicit_patterns = (
        r"\bwhat cards are in my deck\b",
        r"\bwhat cards were in my deck\b",
        r"\bwhat is in my deck\b",
        r"\bwhat's in my deck\b",
        r"\bshow (?:me )?my deck\b",
        r"\bshow (?:me )?@?[a-z0-9_.-]+'?s deck\b",
        r"\bwhat cards are in @?[a-z0-9_.-]+'?s deck\b",
        r"\bwhat cards are in @?[a-z0-9_.-]+ deck\b",
        r"\bwhat is in @?[a-z0-9_.-]+'?s deck\b",
        r"\bwhat's in @?[a-z0-9_.-]+'?s deck\b",
    )
    return any(re.search(pattern, normalized) for pattern in explicit_patterns)


def _resolve_member_candidate(query: str):
    matches = db.resolve_member(query, limit=3)
    if not matches:
        return None, f"I couldn't find a clan member matching {query}."
    exactish = [item for item in matches if item.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0], None
    if len(matches) == 1:
        return matches[0], None
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top, None
    choices = ", ".join(
        item.get("member_ref_with_handle") or item.get("current_name") or item["player_tag"]
        for item in matches[:3]
    )
    return None, f"I couldn't tell which member you meant. Top matches: {choices}"


def _extract_member_deck_target(text: str, message):
    normalized = " ".join((text or "").strip().lower().split())
    if "my deck" in normalized:
        linked = db.get_linked_member_for_discord_user(message.author.id)
        if linked:
            return linked["player_tag"]
    mentioned_users = [
        user for user in getattr(message, "mentions", [])
        if getattr(user, "id", None) != getattr(getattr(_bot(), "user", None), "id", None)
    ]
    if len(mentioned_users) == 1:
        linked = db.get_linked_member_for_discord_user(mentioned_users[0].id)
        if linked:
            return linked["player_tag"]
        for candidate in (
            getattr(mentioned_users[0], "display_name", None),
            getattr(mentioned_users[0], "global_name", None),
            getattr(mentioned_users[0], "name", None),
        ):
            if candidate:
                return candidate
    handles = re.findall(r"(?<!\S)@([A-Za-z0-9_.-]{2,32})", text or "")
    if handles:
        return f"@{handles[0]}"
    return None


def _build_member_deck_report(member_query: str):
    member, error = _resolve_member_candidate(member_query)
    if error:
        return error
    deck = db.get_member_current_deck(member["player_tag"])
    label = member.get("member_ref_with_handle") or member.get("member_ref") or member.get("current_name") or member["player_tag"]
    if not deck or not deck.get("cards"):
        return f"I don't have a stored current deck yet for {label}."
    lines = [f"**Current Deck for {label}**"]
    has_mode_data = False
    for card in deck.get("cards") or []:
        card_name = card.get("name") or "Unknown Card"
        card_level = card.get("level")
        mode_status_label = card.get("mode_status_label")
        if card.get("supports_evo") or card.get("supports_hero"):
            has_mode_data = True
        suffix = f" ({mode_status_label})" if mode_status_label else ""
        if card_level is None:
            lines.append(f"- {card_name}{suffix}")
        else:
            lines.append(f"- {card_name} — Level {card_level}{suffix}")
    if has_mode_data:
        lines.append(
            "_Activation depends on deck slot; these labels show what the card supports or has unlocked._"
        )
    if deck.get("fetched_at"):
        lines.append(f"_Snapshot: {_fmt_iso_short(deck['fetched_at'])}_")
    return "\n".join(lines)


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


def _fallback_channel_response(question: str, workflow: str) -> str:
    normalized = " ".join((question or "").strip().lower().split())
    if "war participation rate" in normalized:
        return "I don't have enough recent war participation data to answer that reliably yet."
    if "what cards are in my deck" in normalized or "current deck" in normalized:
        return "I couldn't build a clean deck answer just now. Try again in a moment."
    if workflow == "clanops":
        return "I couldn't produce a clean answer from the data I have. Try asking a narrower clan ops question."
    return "I couldn't produce a clean answer just now. Try again in a moment."


def _fmt_iso_short(value):
    if not value:
        return "n/a"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_chicago()).strftime("%Y-%m-%d %I:%M %p CT")
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


def _status_badge(ok):
    if ok is None:
        return "⚪"
    return "🟢" if ok else "🔴"


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


def _canon_tag(tag):
    return (str(tag or "").strip().upper().lstrip("#"))


def _format_relative_join_age(joined_date):
    if not joined_date:
        return "join timing unknown"
    try:
        joined_day = datetime.strptime(joined_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return "join timing unknown"
    today = datetime.now(_chicago()).date()
    age_days = max(0, (today - joined_day).days)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def _recent_join_display_rows(clan):
    clan = clan or {}
    live_recent_joins = clan.get("_elixir_recent_joins") or []
    stored_recent_joins = db.list_recent_joins(days=30)
    if live_recent_joins:
        return live_recent_joins, max(len(live_recent_joins), len(stored_recent_joins))
    return stored_recent_joins, len(stored_recent_joins)


def _leader_role_mention():
    leader_role_id = _leader_role_id()
    return f"<@&{leader_role_id}>" if leader_role_id else ""


def _with_leader_ping(content):
    mention = _leader_role_mention()
    if not mention or not content or mention in content:
        return content
    return f"{mention}\n{content}"


def _job_next_runs():
    items = []
    for job in _scheduler().get_jobs():
        next_run = job.next_run_time.astimezone(_chicago()).strftime("%Y-%m-%d %I:%M %p CT") if job.next_run_time else "n/a"
        items.append({"id": job.id, "next_run": next_run})
    return sorted(items, key=lambda item: item["id"])


def _schedule_specs():
    return schedule_specs_from_registry(_runtime_app())


async def _reply_text(message, content):
    def _discord_safe_content(text: str) -> str:
        text = text or ""

        def _replace_image(match):
            alt = (match.group(1) or "").strip()
            url = (match.group(2) or "").strip()
            return f"{alt}: {url}" if alt else url

        return re.sub(r"!\[([^\]]*)\]\((https?://[^)]+)\)", _replace_image, text)

    posts = []
    if isinstance(content, list):
        posts = [item for item in content if isinstance(item, str) and item.strip()]
    else:
        posts = [content]

    sent_messages = []
    for post in posts:
        safe_post = _discord_safe_content(post)
        safe_post = _runtime_app()._resolve_custom_emoji(safe_post, getattr(message, "guild", None))
        if len(safe_post) > 2000:
            for chunk in [safe_post[i:i + 1990] for i in range(0, len(safe_post), 1990)]:
                sent = await message.reply(chunk)
                if sent is not None:
                    sent_messages.append(sent)
        else:
            sent = await message.reply(safe_post)
            if sent is not None:
                sent_messages.append(sent)
    return sent_messages


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
    if role == "clanops":
        return "\n".join(
            [
                "**Elixir Help — ClanOps**",
                "- Use `/elixir ...` for private operator commands in this channel.",
                "- Use `@Elixir do ...` when you want the command and result to stay public in the room.",
                "- System workflow: `/elixir system status`, `/elixir system storage`, and `/elixir system schedule`.",
                "- Clan workflow: `/elixir clan status`, `/elixir clan war`, `/elixir clan members`.",
                "- Member workflow: `/elixir member show`, `/elixir member verify-discord`, `/elixir member set`, and `/elixir member clear`.",
                "- Signal workflow: `/elixir signal show` and `/elixir signal publish-pending`.",
                "- Activity workflow: `/elixir activity list`, `/elixir activity show`, `/elixir activity run`.",
                "- Integration workflow: `/elixir integration list`, `/elixir integration poap-kings status`, `/elixir integration poap-kings publish`.",
                "- Public examples: `@Elixir do clan status`, `@Elixir do member show \"weird name\"`, `@Elixir do integration poap-kings publish data --preview`.",
                "- I can help with roster reviews, war participation, promotions, demotions, kicks, recent form, decks, donations, and member lookups.",
                "- I can resolve members by in-game name, tag, alias, or Discord handle.",
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
        (
            f"roster_now: {roster.get('active_members', 0)}/50 active | open_slots {roster.get('open_slots', 0)} | "
            f"avg_level {_fmt_num(roster.get('avg_exp_level'), 2)} | avg_trophies {_fmt_num(roster.get('avg_trophies'), 0)} | "
            f"weekly_donations {_fmt_num(roster.get('donations_week_total'), 0)}"
        ),
    ]

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
        lines.append("=== RECENT RIVER RACES ===")
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

    trending_war = (summary.get("trending_war_contributors") or {}).get("members") or []
    if trending_war:
        lines.append("")
        lines.append(
            "war momentum leaders: "
            + _join_member_bits(
                trending_war,
                lambda member: f"{_member_label(member)} trend {_fmt_num(member.get('fame_delta') or 0)} fame",
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

    trophy_risers = summary.get("trophy_risers") or []
    if trophy_risers:
        lines.append("")
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

    hot_streaks = summary.get("hot_streaks") or []
    if hot_streaks:
        lines.append("")
        lines.append(
            "battle pulse heaters: "
            + _join_member_bits(
                hot_streaks,
                lambda member: f"{_member_label(member)} won {member.get('current_streak')} straight ({member.get('summary')})",
                limit=5,
            )
        )

    if clan_trend_summary:
        lines.append("")
        lines.append(clan_trend_summary)

    top_donors = summary.get("top_donors") or []
    if top_donors:
        lines.append("")
        lines.append(
            "top donors right now: "
            + _join_member_bits(
                top_donors,
                lambda member: f"{_member_label(member)} {_fmt_num(member.get('donations_week') or 0)}",
                limit=5,
            )
        )

    recent_joins = summary.get("recent_joins") or []
    if recent_joins:
        lines.append("")
        lines.append(
            "recent joins this week: "
            + _join_member_bits(
                recent_joins,
                lambda member: f"{_member_label(member)} ({_format_relative_join_age(member.get('joined_date'))})",
                limit=5,
            )
        )

    trophy_drops = summary.get("trophy_drops") or []
    if trophy_drops:
        lines.append("")
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

    if war:
        clan_war = (war.get("clan") or {})
        if clan_war:
            lines.append("")
            lines.append(
                "live war snapshot now: "
                f"fame {_fmt_num(clan_war.get('fame'))} | repair {_fmt_num(clan_war.get('repairPoints'))} | "
                f"score {_fmt_num(clan_war.get('clanScore'))} | participants {len(clan_war.get('participants') or [])}"
            )

    return "\n".join(lines)


def _strip_bot_mentions(text: str) -> str:
    text = (text or "").lstrip()
    pattern = _leading_bot_mention_pattern()
    if pattern is None:
        return text.strip()
    while True:
        match = pattern.match(text)
        if not match:
            break
        text = text[match.end():].lstrip()
    return text.strip()


def _is_bot_mentioned(message) -> bool:
    pattern = _leading_bot_mention_pattern()
    if pattern is None:
        return False
    return bool(pattern.match(getattr(message, "content", "") or ""))


def _leading_bot_mention_pattern():
    parts = []
    bot_user = getattr(_bot(), "user", None)
    bot_id = getattr(bot_user, "id", None)
    if bot_id:
        parts.append(rf"<@!?{bot_id}>")
    bot_role_id = _bot_role_id()
    if bot_role_id:
        parts.append(rf"<@&{bot_role_id}>")
    if not parts:
        return None
    return re.compile(rf"^\s*(?:{'|'.join(parts)})(?:\s+|$)")


def _get_channel_behavior(channel_id):
    return prompts.discord_channels_by_id().get(channel_id)


def _get_singleton_channel(subagent):
    return prompts.discord_singleton_subagent(subagent)


def _get_singleton_channel_id(subagent):
    return _get_singleton_channel(subagent)["id"]


def _channel_reply_target_name(channel_config):
    return channel_config.get("name") or f"channel:{channel_config['id']}"


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


async def _share_channel_result(result, workflow):
    result = await _apply_member_refs_to_result(result)
    if result.get("event_type") != "channel_share":
        return
    share_content = result.get("share_content", "")
    if not share_content:
        return
    target_ref = result.get("share_channel") or "#clan-events"
    target = prompts.resolve_channel_reference(target_ref)
    if not target:
        _log().warning("Unknown share target channel: %s", target_ref)
        return
    target_channel = _bot().get_channel(target["id"])
    if not target_channel:
        return
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
