from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch
import re

from runtime.activities import normalize_activity_key, resolve_activity
from storage.contextual_memory import archive_member_note_memory, upsert_member_note_memory


COMMAND_ALIASES = {
    "site-data": "poap-kings-data-sync",
    "site-content": "site-content",
    "site-publish": "poap-kings-sync",
    "home-message": "poap-kings-home-sync",
    "members-message": "poap-kings-members-sync",
    "roster-bios": "poap-kings-roster-bios-sync",
    "promote-content": "poap-kings-promotion-sync",
    "heartbeat": "clan-awareness",
    "player-intel": "player-progression",
    "clanops-review": "leadership-review",
    "poap-kings-site-sync": "site-content",
    "promotion": "promotion-content",
}

COMMAND_HELP = {
    "help": "Show the Elixir operator help page.",
    "status": "Show Elixir runtime health, last jobs, and current telemetry.",
    "db-status": "Show a grouped SQLite database status view. Use `db-status clan`, `db-status war`, or `db-status memory`.",
    "schedule": "Show the configured job cadence and next scheduler runs.",
    "signals": "Show signal routing rules, recent routed signals, and pending system signals.",
    "clan-status": "Fetch live clan/war data and print the operational clan status report.",
    "war-status": "Fetch live clan/war data and print Elixir's current war-awareness report.",
    "clan-list": "List active clan members. Default shows name, tag, and Discord name; `full` adds join/birthday/profile/POAP status.",
    "profile": "Show the stored member profile and metadata for one member.",
    "memory": "Inspect Elixir's stored conversation and contextual memory.",
    "verify-discord": "Verify a member's Discord link and ensure the Member role is assigned.",
    "set-discord": "Manually assign a Discord identity to a clan member.",
    "set-join-date": "Set a member join date in YYYY-MM-DD format.",
    "clear-join-date": "Clear a member join date.",
    "set-birthday": "Set a member birthday as month and day.",
    "clear-birthday": "Clear a member birthday.",
    "set-profile-url": "Set a member profile URL.",
    "clear-profile-url": "Clear a member profile URL.",
    "set-poap-address": "Set a member POAP address.",
    "clear-poap-address": "Clear a member POAP address.",
    "set-note": "Set a member note.",
    "clear-note": "Clear a member note.",
    "clan-awareness": "Force the recurring clan-awareness activity now. This processes non-war clan signals and routed clan-event outcomes.",
    "war-awareness": "Force the recurring war-awareness activity now. This is Elixir's single scheduled owner for River Race coordination.",
    "player-progression": "Force the recurring player-progression activity now.",
    "leadership-review": "Force the weekly leadership-review activity now.",
    "system-signals": "Queue any missing startup system signals and publish pending system announcements now.",
    "poap-kings-sync": "Publish the full POAP KINGS website bundle now. This refreshes daily site data plus the members-page weekly recap payload.",
    "poap-kings-data-sync": "Publish only the POAP KINGS structured site data now (clan + roster).",
    "site-content": "Force the recurring site-content activity now. This publishes the POAP KINGS daily clan, roster, and home payloads.",
    "poap-kings-home-sync": "Regenerate and publish only the POAP KINGS website home message.",
    "poap-kings-members-sync": "Regenerate and publish only the POAP KINGS members-page weekly recap payload.",
    "poap-kings-roster-bios-sync": "Regenerate and publish only the POAP KINGS roster intro and member bios.",
    "poap-kings-promotion-sync": "Regenerate and publish only the POAP KINGS website promotion payload.",
    "promotion-content": "Force the recurring promotion-content activity now. This updates the website and #promote-the-clan together.",
    "weekly-recap": "Force the weekly public clan recap post now.",
}

LEADER_ONLY_COMMANDS = {
    "memory",
    "verify-discord",
    "set-discord",
    "set-join-date",
    "clear-join-date",
    "set-birthday",
    "clear-birthday",
    "set-profile-url",
    "clear-profile-url",
    "set-poap-address",
    "clear-poap-address",
    "set-note",
    "clear-note",
    "clan-awareness",
    "war-awareness",
    "player-progression",
    "leadership-review",
    "system-signals",
    "poap-kings-sync",
    "poap-kings-data-sync",
    "site-content",
    "poap-kings-home-sync",
    "poap-kings-members-sync",
    "poap-kings-roster-bios-sync",
    "poap-kings-promotion-sync",
    "promotion-content",
    "weekly-recap",
}

COMMAND_ORDER = [
    "help",
    "status",
    "db-status",
    "schedule",
    "signals",
    "clan-status",
    "war-status",
    "clan-list",
    "profile",
    "memory",
    "verify-discord",
    "set-discord",
    "set-join-date",
    "clear-join-date",
    "set-birthday",
    "clear-birthday",
    "set-profile-url",
    "clear-profile-url",
    "set-poap-address",
    "clear-poap-address",
    "set-note",
    "clear-note",
    "clan-awareness",
    "war-awareness",
    "player-progression",
    "leadership-review",
    "system-signals",
    "poap-kings-sync",
    "poap-kings-data-sync",
    "site-content",
    "poap-kings-home-sync",
    "poap-kings-members-sync",
    "poap-kings-roster-bios-sync",
    "poap-kings-promotion-sync",
    "promotion-content",
    "weekly-recap",
]

ZERO_ARG_COMMANDS = {
    "help",
    "status",
    "schedule",
    "signals",
    "clan-status",
    "war-status",
    "clan-awareness",
    "war-awareness",
    "player-progression",
    "leadership-review",
    "system-signals",
    "poap-kings-sync",
    "poap-kings-data-sync",
    "site-content",
    "poap-kings-home-sync",
    "poap-kings-members-sync",
    "poap-kings-roster-bios-sync",
    "poap-kings-promotion-sync",
    "promotion-content",
    "weekly-recap",
}

DB_STATUS_GROUPS = {"clan", "war", "memory"}


def admin_command_requires_leader(command: str) -> bool:
    return COMMAND_ALIASES.get(command, command) in LEADER_ONLY_COMMANDS


def normalize_admin_command(command: str) -> str:
    return COMMAND_ALIASES.get((command or "").lower(), (command or "").lower())


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_admin_help(
    *,
    mention_prefix: str = "@Elixir do",
    slash_prefix: str = "/elixir",
    cli_prefix: str = "venv/bin/python scripts/elixir_do.py",
) -> str:
    lines = [
        "**Elixir Admin Commands**",
        f"Use grouped slash commands under `{slash_prefix} ...` for private replies, `{mention_prefix} <command>` in `#clanops` for public room replies, or flat `{cli_prefix} <command>` commands in the terminal.",
        "",
        "Commands:",
    ]
    for name in COMMAND_ORDER:
        lines.append(f"- `{name}`: {COMMAND_HELP[name]}")
    lines.extend(
        [
            "",
            "Preview mode:",
            f"- Add `--preview`, or use `{mention_prefix} <command> preview`, to suppress Discord sends and site pushes.",
            "- Preview mode still runs the job logic and shows would-be Discord posts.",
            "",
            "Examples:",
            f"- `{slash_prefix} clan-list`",
            f"- `{slash_prefix} clan-list full:true`",
            f"- `{slash_prefix} profile show member:Ditika`",
            f"- `{slash_prefix} memory show member:Ditika`",
            f"- `{slash_prefix} profile verify-discord member:King Thing`",
            f"- `{slash_prefix} profile set-join-date member:Ditika date:2026-03-07`",
            f"- `{slash_prefix} jobs run job:clan-awareness preview:true`",
            f"- `{mention_prefix} poap-kings-sync --preview`",
            f"- `{mention_prefix} memory member \"Ditika\" search \"war consistency\" --limit 5`",
            f"- `{mention_prefix} verify-discord \"King Thing\"`",
            f"- `{mention_prefix} set-discord \"King Thing\" @kingthing`",
            f"- `{mention_prefix} set-join-date \"Ditika\" 2026-03-07`",
            f"- `{mention_prefix} set-poap-address \"King Levy\" 0xabc123...`",
            f"- `{mention_prefix} set-note \"King Thing\" \"Founder and systems builder\"`",
        ]
    )
    return "\n".join(lines)


def parse_admin_command(text: str, *, require_prefix: bool = False):
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return None
    if not tokens:
        return None

    lowered = [token.lower() for token in tokens]
    explicit_prefix = False
    if lowered[:2] == ["elixir", "do"]:
        explicit_prefix = True
        tokens = tokens[2:]
        lowered = lowered[2:]
    elif lowered and lowered[0] in {"do", "run", "elixir-do"}:
        explicit_prefix = True
        tokens = tokens[1:]
        lowered = lowered[1:]

    if require_prefix and not explicit_prefix:
        return None
    if not tokens:
        return None

    preview = False
    short = False
    filtered = []
    for original, lower in zip(tokens, lowered):
        if lower in {"preview", "--preview"}:
            preview = True
            continue
        if lower in {"--short", "short"}:
            short = True
            continue
        filtered.append(original)

    if not filtered:
        return None

    command = normalize_admin_command(filtered[0])
    extra = filtered[1:]

    if command == "memory":
        args = {"limit": "5"}
        i = 0
        free_tokens = []
        while i < len(extra):
            token = extra[i]
            lower = token.lower()
            if lower in {"member", "--member"} and i + 1 < len(extra):
                args["member"] = extra[i + 1]
                i += 2
                continue
            if lower in {"search", "query", "--search", "--query"} and i + 1 < len(extra):
                args["query"] = extra[i + 1]
                i += 2
                continue
            if lower in {"--limit", "limit"} and i + 1 < len(extra):
                args["limit"] = extra[i + 1]
                i += 2
                continue
            if lower in {"--system-internal", "system-internal", "--internal", "internal"}:
                args["include_system_internal"] = "true"
                i += 1
                continue
            free_tokens.append(token)
            i += 1

        if free_tokens:
            if len(free_tokens) == 1 and "member" not in args and "query" not in args:
                args["member"] = free_tokens[0]
            elif "query" not in args:
                args["query"] = " ".join(free_tokens)
            else:
                return None
        return {"command": "memory", "preview": preview, "short": False, "args": args}

    if command == "help" and not extra:
        return {"command": "help", "preview": preview, "short": False, "args": {}}
    if command in {"status", "schedule", "signals", "war-status"} and not extra:
        return {"command": command, "preview": preview, "short": False, "args": {}}
    if command == "db-status" and not extra:
        return {"command": command, "preview": preview, "short": False, "args": {}}
    if command == "db-status" and len(extra) == 1 and extra[0].lower() in DB_STATUS_GROUPS:
        return {"command": command, "preview": preview, "short": False, "args": {"group": extra[0].lower()}}
    if command == "clan-status" and not extra:
        return {"command": command, "preview": preview, "short": short, "args": {}}
    if command == "clan-list" and not extra:
        return {"command": command, "preview": preview, "short": False, "args": {}}
    if command == "clan-list" and len(extra) == 1 and extra[0].lower() in {"full", "--full"}:
        return {"command": command, "preview": preview, "short": False, "args": {"full": "true"}}
    if command == "profile" and len(extra) >= 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": " ".join(extra)}}
    if command == "verify-discord" and len(extra) >= 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": " ".join(extra)}}
    if command == "set-discord" and len(extra) >= 2:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "discord_name": " ".join(extra[1:])}}
    if command == "set-join-date" and len(extra) == 2:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "date": extra[1]}}
    if command == "clear-join-date" and len(extra) == 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0]}}
    if command == "set-birthday" and len(extra) == 3:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "month": extra[1], "day": extra[2]}}
    if command == "clear-birthday" and len(extra) == 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0]}}
    if command == "set-profile-url" and len(extra) == 2:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "url": extra[1]}}
    if command == "clear-profile-url" and len(extra) == 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0]}}
    if command == "set-poap-address" and len(extra) == 2:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "poap_address": extra[1]}}
    if command == "clear-poap-address" and len(extra) == 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0]}}
    if command == "set-note" and len(extra) >= 2:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0], "note": " ".join(extra[1:])}}
    if command == "clear-note" and len(extra) == 1:
        return {"command": command, "preview": preview, "short": False, "args": {"member": extra[0]}}
    if command in ZERO_ARG_COMMANDS and not extra:
        return {"command": command, "preview": preview, "short": False, "args": {}}
    return None


class _PreviewChannel:
    def __init__(self, channel_id: int, name: str, captured_posts: list[tuple[str, str]]):
        self.id = channel_id
        self.name = name.lstrip("#")
        self.type = "text"
        self._captured_posts = captured_posts

    async def send(self, content: str):
        self._captured_posts.append((self.name, content))


class _ChannelLookup:
    def __init__(self, channels_by_id: dict[int, object]):
        self._channels_by_id = channels_by_id

    def get_channel(self, channel_id: int):
        return self._channels_by_id.get(channel_id)


def _format_preview_posts(posts: list[tuple[str, str]]) -> str:
    if not posts:
        return "_Preview mode: no Discord posts were produced._"
    lines = ["_Preview mode: captured Discord posts:_", ""]
    for name, content in posts:
        lines.append(f"**#{name}**")
        lines.append(content)
        lines.append("")
    return "\n".join(lines).strip()


def _build_clan_list_report(*, full: bool = False) -> str:
    import db

    if full:
        members = db.list_member_metadata_rows()
        lines = [f"**Clan List Full ({len(members)} active)**"]
    else:
        members = db.list_members()
        lines = [f"**Clan List ({len(members)} active)**"]
    if not members:
        lines.append("_No active members found._")
        return "\n".join(lines)
    for member in members:
        name = member.get("current_name") or member.get("member_name") or member.get("player_tag")
        if full:
            joined_date = member.get("joined_date") or "n/a"
            if member.get("birth_month") and member.get("birth_day"):
                birthday = f"{int(member['birth_month']):02d}-{int(member['birth_day']):02d}"
            else:
                birthday = "n/a"
            profile_flag = "yes" if member.get("profile_url") else "no"
            poap_flag = "yes" if member.get("poap_address") else "no"
            lines.append(
                f"- {name} — joined {joined_date} — birthday {birthday} — profile {profile_flag} — POAP {poap_flag}"
            )
            continue

        tag = member.get("player_tag") or member.get("tag") or "n/a"
        line = f"- {name} — `{tag}`"
        discord_user_id = str(member.get("discord_user_id") or "").strip()
        if discord_user_id.isdigit():
            line += f" — <@{discord_user_id}>"
        lines.append(line)
    return "\n".join(lines)


def _fmt_optional(value, empty="n/a"):
    if value in (None, "", []):
        return empty
    return str(value)


def _build_member_profile_report(member_query: str, *, conn=None) -> str:
    import db

    member_tag, label = _resolve_member_tag(member_query, conn=conn)
    profile = db.get_member_profile(member_tag, conn=conn)
    if not profile:
        raise ValueError(f"No stored profile found for {label}.")
    birthday = None
    if profile.get("birth_month") and profile.get("birth_day"):
        birthday = f"{int(profile['birth_month']):02d}-{int(profile['birth_day']):02d}"
    trophies = f"{profile['trophies']:,}" if isinstance(profile.get("trophies"), int) else None
    best_trophies = f"{profile['best_trophies']:,}" if isinstance(profile.get("best_trophies"), int) else None
    streak = None
    if profile.get("recent_form"):
        streak = f"{profile['recent_form'].get('current_streak')}{profile['recent_form'].get('current_streak_type') or ''}"
    lines = [f"**Member Profile: {label}**"]
    lines.append(
        f"- Identity: {_fmt_optional(profile.get('member_name'))} | tag `{profile.get('player_tag')}` | role {_fmt_optional(profile.get('role'))} | rank {_fmt_optional(profile.get('clan_rank'))} | status {_fmt_optional(profile.get('status'))}"
    )
    lines.append(
        f"- Join + metadata: joined {_fmt_optional(profile.get('joined_date'))} | birthday "
        f"{_fmt_optional(birthday)} | "
        f"profile {_fmt_optional(profile.get('profile_url'))} | POAP {_fmt_optional(profile.get('poap_address'))}"
    )
    lines.append(
        f"- Clan state: level {_fmt_optional(profile.get('exp_level'))} | trophies {_fmt_optional(trophies)} | "
        f"best {_fmt_optional(best_trophies)} | donations {_fmt_optional(profile.get('donations_week'))} | received {_fmt_optional(profile.get('donations_received_week'))}"
    )
    lines.append(
        f"- Discord + notes: linked {'yes' if profile.get('in_discord') else 'no'} | "
        f"handle {_fmt_optional(profile.get('discord_display_name') or profile.get('discord_username'))} | "
        f"last seen {_fmt_optional(profile.get('discord_last_seen_at'))} | note {_fmt_optional(profile.get('note'))}"
    )
    lines.append(
        f"- Player history: wins {_fmt_optional(profile.get('career_wins'))} | losses {_fmt_optional(profile.get('career_losses'))} | "
        f"battles {_fmt_optional(profile.get('career_battle_count'))} | total donations {_fmt_optional(profile.get('career_total_donations'))} | "
        f"war day wins {_fmt_optional(profile.get('war_day_wins'))} | 3-crowns {_fmt_optional(profile.get('three_crown_wins'))}"
    )
    if profile.get("recent_form"):
        form = profile["recent_form"]
        lines.append(
            f"- Recent form: {form.get('wins', 0)}-{form.get('losses', 0)} over {form.get('sample_size', 'n/a')} | "
            f"label {_fmt_optional(form.get('form_label'))} | streak {_fmt_optional(streak)}"
        )
    signature_cards = profile.get("signature_cards")
    if isinstance(signature_cards, dict):
        signature_cards = signature_cards.get("cards") or []
    if signature_cards:
        cards = ", ".join(card.get("name") or "Unknown" for card in signature_cards[:5])
        lines.append(f"- Signature cards: {cards}")
    if profile.get("bio"):
        lines.append(f"- Bio: {profile['bio']}")
    return "\n".join(lines)


def _truncate_for_report(text, limit=160):
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _build_signals_report(*, recent_limit: int = 10, conn=None) -> str:
    import db
    from runtime.channel_subagents import signal_routing_summary

    close = conn is None
    conn = conn or db.get_connection()
    try:
        recent_limit = max(1, min(int(recent_limit or 10), 20))
        routing = signal_routing_summary()
        pending_system = db.list_pending_system_signals(conn=conn)
        recent_outcomes = db.list_recent_signal_outcomes(limit=recent_limit * 3, conn=conn)

        grouped_outcomes: dict[str, dict] = {}
        for outcome in recent_outcomes:
            source_key = outcome.get("source_signal_key") or "unknown"
            group = grouped_outcomes.setdefault(
                source_key,
                {
                    "source_signal_key": source_key,
                    "source_signal_type": outcome.get("source_signal_type") or "unknown",
                    "updated_at": outcome.get("updated_at") or outcome.get("created_at"),
                    "outcomes": [],
                },
            )
            timestamp = outcome.get("updated_at") or outcome.get("created_at")
            if timestamp and (group["updated_at"] is None or timestamp > group["updated_at"]):
                group["updated_at"] = timestamp
            group["outcomes"].append(outcome)

        recent_groups = sorted(
            grouped_outcomes.values(),
            key=lambda item: ((item.get("updated_at") or ""), item.get("source_signal_key") or ""),
            reverse=True,
        )[:recent_limit]

        lines = ["**Elixir Signals**", "", "Routing:"]
        for item in routing:
            lines.append(f"- `{item['family']}`: {item['match']}")
            for target in item["targets"]:
                requirement = "required" if target.get("required") else "optional"
                condition = f" - {target['condition']}" if target.get("condition") else ""
                lines.append(
                    f"  -> `{target['subagent']}` `{target['intent']}` ({requirement}){condition}"
                )

        lines.extend(["", f"Recent routed signals ({len(recent_groups)}):"])
        if not recent_groups:
            lines.append("_No routed signals recorded yet._")
        else:
            for group in recent_groups:
                timestamp = group.get("updated_at") or "n/a"
                lines.append(
                    f"- `{group['source_signal_type']}` at {timestamp} - `{group['source_signal_key']}`"
                )
                for outcome in sorted(
                    group["outcomes"],
                    key=lambda item: ((item.get("target_channel_key") or ""), (item.get("intent") or "")),
                ):
                    requirement = "required" if outcome.get("required") else "optional"
                    error_detail = outcome.get("error_detail")
                    error_text = f" - {_truncate_for_report(error_detail, 90)}" if error_detail else ""
                    lines.append(
                        f"  -> `{outcome.get('target_channel_key')}` `{outcome.get('intent')}` "
                        f"{outcome.get('delivery_status') or 'unknown'} ({requirement}){error_text}"
                    )

        lines.extend(["", f"Pending system signals ({len(pending_system)}):"])
        if not pending_system:
            lines.append("_No pending system signals._")
        else:
            for signal in pending_system[:5]:
                lines.append(
                    f"- `{signal.get('type') or signal.get('signal_type') or 'unknown'}` "
                    f"`{signal.get('signal_key')}` created {signal.get('created_at') or 'n/a'}"
                )
        return "\n".join(lines)
    finally:
        if close:
            conn.close()


def _format_contextual_memory_item(memory: dict) -> str:
    source = memory.get("source_type") or "unknown"
    if source == "leader_note":
        source_label = "leader"
    elif source == "elixir_inference":
        source_label = f"inference {float(memory.get('confidence') or 0.0):.2f}"
    else:
        source_label = "system"
    created_at = memory.get("created_at") or "n/a"
    summary = _truncate_for_report(memory.get("summary") or memory.get("title") or memory.get("body") or "")
    tags = memory.get("tags") or []
    tag_text = f" | tags {', '.join(tags[:3])}" if tags else ""
    return (
        f"- `#{memory.get('memory_id')}` {created_at} | {memory.get('scope') or 'n/a'} | "
        f"{source_label} | {memory.get('status') or 'n/a'}: {summary}{tag_text}"
    )


def _format_conversation_facts(title: str, facts: list[dict], limit: int) -> list[str]:
    if not facts:
        return [f"- {title}: none"]
    lines = [f"- {title}: {len(facts)}"]
    for fact in facts[:limit]:
        confidence = float(fact.get("confidence") or 0.0)
        lines.append(
            f"- {title[:-1]} `{fact.get('fact_type')}` ({confidence:.2f}) updated {fact.get('updated_at') or 'n/a'}: "
            f"{_truncate_for_report(fact.get('fact_value') or '')}"
        )
    return lines


def _format_conversation_episodes(title: str, episodes: list[dict], limit: int) -> list[str]:
    if not episodes:
        return [f"- {title}: none"]
    lines = [f"- {title}: {len(episodes)}"]
    for episode in episodes[:limit]:
        lines.append(
            f"- {title[:-1]} `{episode.get('episode_type')}` importance {episode.get('importance') or 0} "
            f"at {episode.get('created_at') or 'n/a'}: {_truncate_for_report(episode.get('summary') or '')}"
        )
    return lines


def _get_conversation_memory_totals(conn) -> dict:
    row = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM memory_facts) AS facts, "
        "(SELECT COUNT(*) FROM memory_episodes) AS episodes, "
        "(SELECT COUNT(*) FROM channel_state) AS channels"
    ).fetchone()
    return dict(row)


def _list_recent_conversation_facts(limit: int, *, conn) -> list[dict]:
    rows = conn.execute(
        "SELECT subject_type, subject_key, fact_type, fact_value, confidence, updated_at "
        "FROM memory_facts ORDER BY updated_at DESC, fact_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _list_recent_conversation_episodes(limit: int, *, conn) -> list[dict]:
    rows = conn.execute(
        "SELECT subject_type, subject_key, episode_type, summary, importance, created_at "
        "FROM memory_episodes ORDER BY created_at DESC, episode_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _build_memory_report(*, member_query: str | None = None, query: str | None = None, limit: int = 5,
                         include_system_internal: bool = False, conn=None) -> str:
    import db
    from memory_store import list_memories, search_memories

    close = conn is None
    conn = conn or db.get_connection()
    try:
        viewer_scope = "system_internal" if include_system_internal else "leadership"
        limit = max(1, min(int(limit or 5), 10))
        system_status = db.get_system_status(conn=conn)
        memory_status = system_status.get("contextual_memory") or {}
        conversation_totals = _get_conversation_memory_totals(conn)
        lines = ["**Elixir Memory**"]
        lines.append(
            "- Conversation store: "
            f"{conversation_totals.get('facts', 0)} facts | "
            f"{conversation_totals.get('episodes', 0)} episodes | "
            f"{conversation_totals.get('channels', 0)} channel states"
        )
        lines.append(
            "- Context store: "
            f"{memory_status.get('total', 0)} total | "
            f"{memory_status.get('leader_notes', 0)} leader | "
            f"{memory_status.get('inferences', 0)} inference | "
            f"{memory_status.get('system_notes', 0)} system | "
            f"sqlite-vec {'on' if memory_status.get('sqlite_vec_enabled') else 'off'}"
        )
        lines.append(f"- View: `{viewer_scope}`")

        member_filter = None
        member_label = None
        member_profile = None
        if member_query:
            member_tag, member_label = _resolve_member_tag(member_query, conn=conn)
            member_filter = {"member_tag": member_tag}
            member_profile = db.get_member_profile(member_tag, conn=conn) or {}
            lines.append(f"- Member: {member_label} (`{member_tag}`)")

            memory_context = db.build_memory_context(
                discord_user_id=member_profile.get("discord_user_id"),
                member_tag=member_tag,
                viewer_scope=viewer_scope,
                conn=conn,
            )
            lines.append("")
            lines.append("Conversation memory:")
            user_ctx = memory_context.get("discord_user") or {}
            member_ctx = memory_context.get("member") or {}
            lines.extend(_format_conversation_facts("User facts", user_ctx.get("facts") or [], limit))
            lines.extend(_format_conversation_episodes("User episodes", user_ctx.get("episodes") or [], limit))
            lines.extend(_format_conversation_facts("Member facts", member_ctx.get("facts") or [], limit))
            lines.extend(_format_conversation_episodes("Member episodes", member_ctx.get("episodes") or [], limit))
        else:
            recent_facts = _list_recent_conversation_facts(limit, conn=conn)
            recent_episodes = _list_recent_conversation_episodes(limit, conn=conn)
            lines.append("")
            lines.append("Recent conversation memory:")
            if not recent_facts:
                lines.append("- Recent facts: none")
            else:
                lines.append(f"- Recent facts: {len(recent_facts)}")
                for fact in recent_facts:
                    lines.append(
                        f"- Fact `{fact.get('subject_type')}:{fact.get('subject_key')}` "
                        f"`{fact.get('fact_type')}` ({float(fact.get('confidence') or 0.0):.2f}) "
                        f"updated {fact.get('updated_at') or 'n/a'}: "
                        f"{_truncate_for_report(fact.get('fact_value') or '')}"
                    )
            if not recent_episodes:
                lines.append("- Recent episodes: none")
            else:
                lines.append(f"- Recent episodes: {len(recent_episodes)}")
                for episode in recent_episodes:
                    lines.append(
                        f"- Episode `{episode.get('subject_type')}:{episode.get('subject_key')}` "
                        f"`{episode.get('episode_type')}` importance {episode.get('importance') or 0} "
                        f"at {episode.get('created_at') or 'n/a'}: {_truncate_for_report(episode.get('summary') or '')}"
                    )

        lines.append("")
        if query:
            results = search_memories(
                query,
                viewer_scope=viewer_scope,
                include_system_internal=include_system_internal,
                filters=member_filter,
                limit=limit,
                conn=conn,
            )
            lines.append(f"Contextual memory search for `{query}`:")
            if not results:
                lines.append("_No contextual memories matched._")
            else:
                for result in results:
                    lines.append(_format_contextual_memory_item(result.memory))
        else:
            items = list_memories(
                viewer_scope=viewer_scope,
                include_system_internal=include_system_internal,
                filters=member_filter,
                limit=limit,
                conn=conn,
            )
            subject = f" for {member_label}" if member_label else ""
            lines.append(f"Recent contextual memories{subject}:")
            if not items:
                lines.append("_No contextual memories stored._")
            else:
                for item in items:
                    lines.append(_format_contextual_memory_item(item))

        latest_memory_at = memory_status.get("latest_memory_at")
        if latest_memory_at:
            lines.append("")
            lines.append(f"- Latest contextual memory at {latest_memory_at}")
        return "\n".join(lines)
    finally:
        if close:
            conn.close()


@asynccontextmanager
async def _preview_job_runtime():
    import prompts
    from runtime import jobs as runtime_jobs

    captured_posts: list[tuple[str, str]] = []
    channels = {
        channel["id"]: _PreviewChannel(channel["id"], channel["name"], captured_posts)
        for channel in prompts.discord_channel_configs()
    }
    stack = ExitStack()
    try:
        stack.enter_context(patch.object(runtime_jobs, "bot", _ChannelLookup(channels)))
        stack.enter_context(
            patch("runtime.jobs.poap_kings_site.commit_and_push", side_effect=lambda *args, **kwargs: None)
        )
        stack.enter_context(
            patch("runtime.jobs.poap_kings_site.publish_site_content", side_effect=lambda *args, **kwargs: False)
        )
        yield captured_posts
    finally:
        stack.close()


async def _load_site_context():
    import elixir
    from integrations.poap_kings import site as site_content
    from integrations.poap_kings import site as poap_kings_site

    clan, war = await elixir._load_live_clan_context()
    roster = await asyncio.to_thread(poap_kings_site.load_published, "roster")
    if roster is None:
        roster = await asyncio.to_thread(site_content.load_current, "roster")
    if roster is None and clan.get("memberList"):
        roster = await asyncio.to_thread(site_content.build_roster_data, clan, True)
    return clan, war, roster


async def _run_runtime_job(job_name: str, preview: bool) -> str:
    import elixir
    activity_key = normalize_activity_key(job_name)
    if activity_key:
        resolved = resolve_activity(activity_key, elixir)
        job_callable = resolved["job_callable"]
        display_name = resolved["activity_key"]
    else:
        job_map = {
            "poap-kings-data-sync": elixir._site_data_refresh,
        }
        job_callable = job_map[job_name]
        display_name = job_name
    if preview:
        async with _preview_job_runtime() as captured_posts:
            try:
                await job_callable()
            except Exception as exc:
                return f"`{display_name}` failed in preview mode: {exc}"
            return f"Ran `{display_name}` in preview mode.\n\n{_format_preview_posts(captured_posts)}"
    try:
        await job_callable()
    except Exception as exc:
        return f"`{display_name}` failed: {exc}"
    return f"Ran `{display_name}`."


async def _run_system_signals(preview: bool) -> str:
    import elixir

    if preview:
        async with _preview_job_runtime() as captured_posts:
            try:
                count = await elixir._publish_pending_system_signal_updates(seed_startup_signals=True)
            except Exception as exc:
                return f"`system-signals` failed in preview mode: {exc}"
            summary = f"Ran `system-signals` in preview mode for {count} pending signal(s)."
            return f"{summary}\n\n{_format_preview_posts(captured_posts)}"

    try:
        count = await elixir._publish_pending_system_signal_updates(seed_startup_signals=True)
    except Exception as exc:
        return f"`system-signals` failed: {exc}"
    return f"Ran `system-signals` for {count} pending signal(s)."


async def _run_poap_kings_sync(preview: bool) -> str:
    import elixir

    if preview:
        async with _preview_job_runtime() as captured_posts:
            try:
                await elixir._site_content_cycle()
                members_text = await _run_members_message(preview=True)
            except Exception as exc:
                return f"`poap-kings-sync` failed in preview mode: {exc}"
            sections = ["Ran `poap-kings-sync` in preview mode.", "", _format_preview_posts(captured_posts)]
            if members_text:
                sections.extend(["", "**Members Page Weekly Recap Preview**", members_text])
            return "\n".join(section for section in sections if section is not None)

    await elixir._site_content_cycle()
    await _run_members_message(preview=False)
    return "Ran `poap-kings-sync`."


async def _run_home_message(preview: bool) -> str:
    import elixir
    from integrations.poap_kings import site as poap_kings_site

    clan, war, roster = await _load_site_context()
    previous = await asyncio.to_thread(poap_kings_site.load_published, "home")
    previous_message = previous.get("message", "") if previous else ""
    text = await asyncio.to_thread(
        elixir.elixir_agent.generate_home_message,
        clan,
        war,
        previous_message,
        roster_data=roster,
    )
    if not text:
        raise RuntimeError("home message generation returned nothing")
    payload = {"message": text, "generated": _utcnow()}
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"home": payload},
            "Elixir POAP KINGS home message update",
        )
    return text


async def _run_members_message(preview: bool) -> str:
    import elixir
    from integrations.poap_kings import site as poap_kings_site

    clan, war, roster = await _load_site_context()
    recap_context = await asyncio.to_thread(elixir._build_weekly_clan_recap_context, clan, war)
    previous = await asyncio.to_thread(poap_kings_site.load_published, "members")
    previous_message = previous.get("message", "") if previous else ""
    text = await asyncio.to_thread(
        elixir.elixir_agent.generate_weekly_digest,
        recap_context,
        previous_message,
    )
    if not text:
        raise RuntimeError("members page weekly recap generation returned nothing")
    payload = {
        "title": "Weekly Recap",
        "message": text,
        "generated": _utcnow(),
        "source": "weekly_clan_recap",
    }
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"members": payload},
            "Elixir POAP KINGS members page weekly recap update",
        )
    return text


async def _run_roster_bios(preview: bool) -> str:
    import db
    import elixir
    from integrations.poap_kings import site as poap_kings_site

    clan, war, roster = await _load_site_context()
    result = await asyncio.to_thread(
        elixir.elixir_agent.generate_roster_bios,
        clan,
        war,
        roster_data=roster,
    )
    if not result:
        raise RuntimeError("roster bios generation returned nothing")
    if "intro" not in result and "content" in result:
        result = json.loads(result["content"])

    roster_payload = roster or {"members": []}
    roster_payload["intro"] = result.get("intro", "")
    bios_by_tag = result.get("members", {}) or {}
    await asyncio.to_thread(db.upsert_member_generated_profiles, bios_by_tag)
    for member in roster_payload.get("members", []):
        item = bios_by_tag.get(member["tag"], {}) or bios_by_tag.get("#" + member["tag"], {})
        if item:
            member["bio"] = item.get("bio", "")
            member["highlight"] = item.get("highlight", "general")
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"roster": roster_payload},
            "Elixir POAP KINGS roster bio update",
        )
    return roster_payload.get("intro", "")


async def _run_promote_content(preview: bool) -> str:
    import elixir
    from integrations.poap_kings import site as poap_kings_site
    from runtime import jobs as runtime_jobs

    clan, war, roster = await _load_site_context()
    promote = await asyncio.to_thread(
        elixir.elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=roster,
    )
    if not promote:
        raise RuntimeError("promotion content generation returned nothing")
    runtime_jobs._validate_promote_content_or_raise(promote)
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"promote": promote},
            "Elixir POAP KINGS promotion content update",
        )
    return json.dumps(promote, indent=2)


def _resolve_member_tag(member_query: str, *, conn=None) -> tuple[str, str]:
    import db

    query = (member_query or "").strip()
    tag_match = re.search(r"(#?[A-Z0-9]+)\)$", query, re.IGNORECASE)
    if tag_match:
        candidate_tag = tag_match.group(1)
        matches = db.resolve_member(candidate_tag, limit=3, conn=conn)
    else:
        matches = db.resolve_member(query, limit=3, conn=conn)
    if not matches:
        raise ValueError(f"No clan member matched {member_query!r}.")
    exactish = [item for item in matches if item.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        best = exactish[0]
    elif len(matches) == 1:
        best = matches[0]
    elif (matches[0].get("match_score", 0) - matches[1].get("match_score", 0)) >= 100:
        best = matches[0]
    else:
        choices = ", ".join(
            item.get("member_ref_with_handle") or item.get("current_name") or item.get("player_tag")
            for item in matches[:3]
        )
        raise ValueError(f"Ambiguous member {member_query!r}. Top matches: {choices}")
    return best["player_tag"], best.get("member_ref_with_handle") or best.get("current_name") or best["player_tag"]


async def _run_member_metadata_command(command: str, *, preview: bool, args: dict) -> str:
    import db
    import runtime.onboarding as onboarding

    member_tag, label = await asyncio.to_thread(_resolve_member_tag, args["member"])
    if command == "set-discord":
        discord_name = args["discord_name"].strip()
        guild_member = await onboarding.resolve_discord_member_input(discord_name)
        if guild_member is not None:
            linked_label = f"{guild_member.display_name} (<@{guild_member.id}>)"
            if preview:
                return f"Preview: would link Discord identity for {label} to {linked_label}."
            await asyncio.to_thread(
                db.link_discord_user_to_member,
                guild_member.id,
                member_tag,
                username=guild_member.name,
                display_name=guild_member.display_name,
                source="manual_name_resolution",
            )
            return f"Linked Discord identity for {label} to {linked_label}."
        return (
            f"Couldn't resolve `{discord_name}` to a unique Discord member for {label}. "
            "Use a real mention, a numeric Discord user ID, or a unique exact username/display name that exists in the server."
        )
    if command == "set-join-date":
        if preview:
            return f"Preview: would set join date for {label} to {args['date']}."
        await asyncio.to_thread(db.set_member_join_date, member_tag, None, args["date"])
        return f"Set join date for {label} to {args['date']}."
    if command == "clear-join-date":
        if preview:
            return f"Preview: would clear join date for {label}."
        await asyncio.to_thread(db.clear_member_join_date, member_tag, None)
        return f"Cleared join date for {label}."
    if command == "set-birthday":
        month = int(args["month"])
        day = int(args["day"])
        if preview:
            return f"Preview: would set birthday for {label} to {month:02d}-{day:02d}."
        await asyncio.to_thread(db.set_member_birthday, member_tag, None, month, day)
        return f"Set birthday for {label} to {month:02d}-{day:02d}."
    if command == "clear-birthday":
        if preview:
            return f"Preview: would clear birthday for {label}."
        await asyncio.to_thread(db.clear_member_birthday, member_tag, None)
        return f"Cleared birthday for {label}."
    if command == "set-profile-url":
        if preview:
            return f"Preview: would set profile URL for {label} to {args['url']}."
        await asyncio.to_thread(db.set_member_profile_url, member_tag, None, args["url"])
        return f"Set profile URL for {label}."
    if command == "clear-profile-url":
        if preview:
            return f"Preview: would clear profile URL for {label}."
        await asyncio.to_thread(db.clear_member_profile_url, member_tag, None)
        return f"Cleared profile URL for {label}."
    if command == "set-poap-address":
        if preview:
            return f"Preview: would set POAP address for {label} to {args['poap_address']}."
        await asyncio.to_thread(db.set_member_poap_address, member_tag, None, args["poap_address"])
        return f"Set POAP address for {label}."
    if command == "clear-poap-address":
        if preview:
            return f"Preview: would clear POAP address for {label}."
        await asyncio.to_thread(db.clear_member_poap_address, member_tag, None)
        return f"Cleared POAP address for {label}."
    if command == "set-note":
        if preview:
            return f"Preview: would set note for {label} to: {args['note']}"
        await asyncio.to_thread(db.set_member_note, member_tag, None, args["note"])
        await asyncio.to_thread(
            upsert_member_note_memory,
            member_tag=member_tag,
            member_label=label,
            note=args["note"],
            created_by="leader:admin-command",
            metadata={"command": "set-note"},
        )
        return f"Set note for {label}."
    if command == "clear-note":
        if preview:
            return f"Preview: would clear note for {label}."
        await asyncio.to_thread(db.clear_member_note, member_tag, None)
        await asyncio.to_thread(
            archive_member_note_memory,
            member_tag=member_tag,
            actor="leader:admin-command",
        )
        return f"Cleared note for {label}."
    raise ValueError(f"Unknown metadata command: {command}")


async def _run_verify_discord(*, preview: bool, args: dict) -> str:
    import runtime.onboarding as onboarding

    member_tag, label = await asyncio.to_thread(_resolve_member_tag, args["member"])
    if preview:
        return f"Preview: would verify the Discord link and Member role for {label}."
    return await onboarding.verify_discord_membership(member_tag)


async def dispatch_admin_command(command: str, *, preview: bool = False, short: bool = False, args: dict | None = None) -> str:
    import elixir
    args = args or {}
    command = normalize_admin_command(command)

    if command == "help":
        return render_admin_help()
    if command == "status":
        return elixir._build_status_report()
    if command == "db-status":
        return elixir._build_db_status_report(group=args.get("group"))
    if command == "schedule":
        return elixir._build_schedule_report()
    if command == "signals":
        return await asyncio.to_thread(_build_signals_report)
    if command == "clan-status":
        clan, war = await elixir._load_live_clan_context()
        if short:
            return elixir._build_clan_status_short_report(clan, war)
        return elixir._build_clan_status_report(clan, war)
    if command == "war-status":
        clan, war = await elixir._load_live_clan_context()
        return elixir._build_war_status_report(clan, war)
    if command == "clan-list":
        return await asyncio.to_thread(
            _build_clan_list_report,
            full=str(args.get("full", "")).lower() in {"1", "true", "yes", "on", "full"},
        )
    if command == "profile":
        return await asyncio.to_thread(_build_member_profile_report, args["member"])
    if command == "memory":
        return await asyncio.to_thread(
            _build_memory_report,
            member_query=args.get("member"),
            query=args.get("query"),
            limit=args.get("limit", 5),
            include_system_internal=str(args.get("include_system_internal", "")).lower() in {"1", "true", "yes", "on"},
        )
    if command == "verify-discord":
        return await _run_verify_discord(preview=preview, args=args)
    if command in {
        "set-discord",
        "set-join-date",
        "clear-join-date",
        "set-birthday",
        "clear-birthday",
        "set-profile-url",
        "clear-profile-url",
        "set-poap-address",
        "clear-poap-address",
        "set-note",
        "clear-note",
    }:
        return await _run_member_metadata_command(command, preview=preview, args=args)
    if command == "system-signals":
        return await _run_system_signals(preview=preview)
    if command == "poap-kings-sync":
        return await _run_poap_kings_sync(preview=preview)
    if command == "poap-kings-home-sync":
        return await _run_home_message(preview=preview)
    if command == "poap-kings-members-sync":
        return await _run_members_message(preview=preview)
    if command == "poap-kings-roster-bios-sync":
        return await _run_roster_bios(preview=preview)
    if command == "poap-kings-promotion-sync":
        return await _run_promote_content(preview=preview)
    return await _run_runtime_job(command, preview=preview)


__all__ = [
    "LEADER_ONLY_COMMANDS",
    "admin_command_requires_leader",
    "_build_signals_report",
    "COMMAND_HELP",
    "COMMAND_ORDER",
    "dispatch_admin_command",
    "normalize_admin_command",
    "parse_admin_command",
    "render_admin_help",
]
