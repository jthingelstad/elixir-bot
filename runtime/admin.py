from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch
import re

from runtime.activities import (
    list_registered_activities,
    normalize_activity_key,
    resolve_activity,
    schedule_specs_from_registry,
)
from storage.contextual_memory import archive_member_note_memory, upsert_member_note_memory

@dataclass(frozen=True)
class AdminCommandSpec:
    key: str
    path: tuple[str, ...]
    description: str
    leader_only: bool = False
    write: bool = False
    event_type: str | None = None


DB_STATUS_GROUPS = {"all", "clan", "war", "memory"}
SIGNAL_SHOW_VIEWS = {"all", "routes", "recent", "pending"}
MEMBER_SET_FIELDS = {"discord", "join-date", "birthday", "profile-url", "poap-address", "note"}
MEMBER_CLEAR_FIELDS = {"discord", "join-date", "birthday", "profile-url", "poap-address", "note"}
INTEGRATION_PUBLISH_TARGETS = {"all", "data", "home", "members", "roster-bios", "promote"}

COMMAND_SPECS = {
    "help": AdminCommandSpec("help", ("help",), "Show the Elixir operator help page.", event_type="help"),
    "system.status": AdminCommandSpec("system.status", ("system", "status"), "Show Elixir runtime health and telemetry.", event_type="status_report"),
    "system.storage": AdminCommandSpec("system.storage", ("system", "storage"), "Show grouped database storage status.", event_type="storage_report"),
    "system.schedule": AdminCommandSpec("system.schedule", ("system", "schedule"), "Show scheduled activities and next runs.", event_type="schedule_report"),
    "clan.status": AdminCommandSpec("clan.status", ("clan", "status"), "Show the operational clan status report.", event_type="clan_status_report"),
    "clan.war": AdminCommandSpec("clan.war", ("clan", "war"), "Show the live war-awareness report.", event_type="war_status_report"),
    "clan.members": AdminCommandSpec("clan.members", ("clan", "members"), "List active clan members.", event_type="clan_members_report"),
    "member.show": AdminCommandSpec("member.show", ("member", "show"), "Show the stored member profile and metadata for one member.", event_type="member_profile_report"),
    "member.verify-discord": AdminCommandSpec("member.verify-discord", ("member", "verify-discord"), "Verify a member's Discord link and Member role.", leader_only=True, write=True, event_type="member_verify_discord"),
    "member.set": AdminCommandSpec("member.set", ("member", "set"), "Set one member field.", leader_only=True, write=True, event_type="member_set"),
    "member.clear": AdminCommandSpec("member.clear", ("member", "clear"), "Clear one member field.", leader_only=True, write=True, event_type="member_clear"),
    "memory.show": AdminCommandSpec("memory.show", ("memory", "show"), "Inspect stored conversation and contextual memory.", leader_only=True, event_type="memory_report"),
    "signal.show": AdminCommandSpec("signal.show", ("signal", "show"), "Show signal routing, recent routed signals, and pending system signals.", event_type="signals_report"),
    "signal.publish-pending": AdminCommandSpec("signal.publish-pending", ("signal", "publish-pending"), "Queue missing startup signals and publish pending system announcements.", leader_only=True, write=True, event_type="signal_publish_pending"),
    "activity.list": AdminCommandSpec("activity.list", ("activity", "list"), "List registered recurring activities.", event_type="activity_list"),
    "activity.show": AdminCommandSpec("activity.show", ("activity", "show"), "Show one recurring activity in detail.", event_type="activity_show"),
    "activity.run": AdminCommandSpec("activity.run", ("activity", "run"), "Run one registered activity now.", leader_only=True, write=True, event_type="activity_run"),
    "integration.list": AdminCommandSpec("integration.list", ("integration", "list"), "List configured integration modules.", event_type="integration_list"),
    "integration.poap-kings.status": AdminCommandSpec("integration.poap-kings.status", ("integration", "poap-kings", "status"), "Show POAP KINGS integration status.", event_type="integration_poap_kings_status"),
    "integration.poap-kings.publish": AdminCommandSpec("integration.poap-kings.publish", ("integration", "poap-kings", "publish"), "Publish one POAP KINGS integration target.", leader_only=True, write=True, event_type="integration_poap_kings_publish"),
}

COMMAND_GROUP_ORDER = [
    "system",
    "clan",
    "member",
    "memory",
    "signal",
    "activity",
    "integration",
]

LEGACY_COMMAND_HINTS = {
    "status": "Use `@Elixir do system status` or `/elixir system status`.",
    "db-status": "Use `@Elixir do system storage <all|clan|war|memory>` or `/elixir system storage`.",
    "schedule": "Use `@Elixir do system schedule` or `/elixir system schedule`.",
    "signals": "Use `@Elixir do signal show` or `/elixir signal show`.",
    "clan-status": "Use `@Elixir do clan status` or `/elixir clan status`.",
    "war-status": "Use `@Elixir do clan war` or `/elixir clan war`.",
    "clan-list": "Use `@Elixir do clan members` or `/elixir clan members`.",
    "profile": "Use `@Elixir do member show <member>` or `/elixir member show`.",
    "verify-discord": "Use `@Elixir do member verify-discord <member>` or `/elixir member verify-discord`.",
    "set-discord": "Use `@Elixir do member set <member> discord <value>` or `/elixir member set`.",
    "set-join-date": "Use `@Elixir do member set <member> join-date <YYYY-MM-DD>` or `/elixir member set`.",
    "clear-join-date": "Use `@Elixir do member clear <member> join-date` or `/elixir member clear`.",
    "set-birthday": "Use `@Elixir do member set <member> birthday <MM-DD>` or `/elixir member set`.",
    "clear-birthday": "Use `@Elixir do member clear <member> birthday` or `/elixir member clear`.",
    "set-profile-url": "Use `@Elixir do member set <member> profile-url <url>` or `/elixir member set`.",
    "clear-profile-url": "Use `@Elixir do member clear <member> profile-url` or `/elixir member clear`.",
    "set-poap-address": "Use `@Elixir do member set <member> poap-address <value>` or `/elixir member set`.",
    "clear-poap-address": "Use `@Elixir do member clear <member> poap-address` or `/elixir member clear`.",
    "set-note": "Use `@Elixir do member set <member> note <text>` or `/elixir member set`.",
    "clear-note": "Use `@Elixir do member clear <member> note` or `/elixir member clear`.",
    "memory": "Use `@Elixir do memory show ...` or `/elixir memory show`.",
    "system-signals": "Use `@Elixir do signal publish-pending` or `/elixir signal publish-pending`.",
    "jobs": "Use `@Elixir do activity run <activity>` or `/elixir activity run`.",
    "clan-awareness": "Use `@Elixir do activity run clan-awareness` or `/elixir activity run activity:clan-awareness`.",
    "war-awareness": "Use `@Elixir do activity run war-awareness` or `/elixir activity run activity:war-awareness`.",
    "player-progression": "Use `@Elixir do activity run player-progression` or `/elixir activity run activity:player-progression`.",
    "leadership-review": "Use `@Elixir do activity run leadership-review` or `/elixir activity run activity:leadership-review`.",
    "weekly-recap": "Use `@Elixir do activity run weekly-recap` or `/elixir activity run activity:weekly-recap`.",
    "site-content": "Use `@Elixir do activity run site-content` or `/elixir activity run activity:site-content`.",
    "promotion-content": "Use `@Elixir do activity run promotion-content` or `/elixir activity run activity:promotion-content`.",
    "poap-kings-sync": "Use `@Elixir do integration poap-kings publish all` or `/elixir integration poap-kings publish target:all`.",
    "poap-kings-data-sync": "Use `@Elixir do integration poap-kings publish data` or `/elixir integration poap-kings publish target:data`.",
    "poap-kings-home-sync": "Use `@Elixir do integration poap-kings publish home` or `/elixir integration poap-kings publish target:home`.",
    "poap-kings-members-sync": "Use `@Elixir do integration poap-kings publish members` or `/elixir integration poap-kings publish target:members`.",
    "poap-kings-roster-bios-sync": "Use `@Elixir do integration poap-kings publish roster-bios` or `/elixir integration poap-kings publish target:roster-bios`.",
    "poap-kings-promotion-sync": "Use `@Elixir do integration poap-kings publish promote` or `/elixir integration poap-kings publish target:promote`.",
    "heartbeat": "Use `@Elixir do activity run clan-awareness` or `/elixir activity run activity:clan-awareness`.",
    "player-intel": "Use `@Elixir do activity run player-progression` or `/elixir activity run activity:player-progression`.",
    "clanops-review": "Use `@Elixir do activity run leadership-review` or `/elixir activity run activity:leadership-review`.",
    "site-data": "Use `@Elixir do integration poap-kings publish data` or `/elixir integration poap-kings publish target:data`.",
    "site-publish": "Use `@Elixir do integration poap-kings publish all` or `/elixir integration poap-kings publish target:all`.",
    "home-message": "Use `@Elixir do integration poap-kings publish home` or `/elixir integration poap-kings publish target:home`.",
    "members-message": "Use `@Elixir do integration poap-kings publish members` or `/elixir integration poap-kings publish target:members`.",
    "roster-bios": "Use `@Elixir do integration poap-kings publish roster-bios` or `/elixir integration poap-kings publish target:roster-bios`.",
    "promote-content": "Use `@Elixir do integration poap-kings publish promote` or `/elixir integration poap-kings publish target:promote`.",
    "poap-kings-site-sync": "Use `@Elixir do activity run site-content` or `/elixir activity run activity:site-content`.",
    "promotion": "Use `@Elixir do activity run promotion-content` or `/elixir activity run activity:promotion-content`.",
}

COMMAND_HELP = {key: spec.description for key, spec in COMMAND_SPECS.items()}
LEADER_ONLY_COMMANDS = {key for key, spec in COMMAND_SPECS.items() if spec.leader_only}
COMMAND_ORDER = list(COMMAND_SPECS)


def _command_request(
    key: str,
    *,
    args: dict | None = None,
    preview: bool = False,
    short: bool = False,
    kind: str = "command",
    hint: str | None = None,
    legacy_input: str | None = None,
) -> dict:
    spec = COMMAND_SPECS.get(key)
    path = spec.path if spec else tuple()
    return {
        "kind": kind,
        "key": key,
        "command": key,
        "resource": path[0] if path else None,
        "action": path[-1] if path else None,
        "path": path,
        "args": args or {},
        "preview": preview,
        "short": short,
        "hint": hint,
        "legacy_input": legacy_input,
    }


def admin_command_requires_leader(command: str | dict) -> bool:
    key = command.get("key") if isinstance(command, dict) else str(command or "")
    return key in LEADER_ONLY_COMMANDS


def normalize_admin_command(command: str) -> str:
    return str(command or "").strip().lower()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_admin_help(
    *,
    mention_prefix: str = "@Elixir do",
    slash_prefix: str = "/elixir",
) -> str:
    grouped = {group: [] for group in COMMAND_GROUP_ORDER}
    for spec in COMMAND_SPECS.values():
        if spec.key == "help":
            continue
        grouped.setdefault(spec.path[0], []).append(spec)

    lines = [
        "**Elixir Admin Commands**",
        f"Use grouped slash commands under `{slash_prefix} ...` for private replies and `{mention_prefix} ...` in `#clanops` for public room replies.",
        "",
    ]
    for group in COMMAND_GROUP_ORDER:
        specs = grouped.get(group) or []
        if not specs:
            continue
        lines.append(f"**{group.title()}**")
        for spec in specs:
            path_label = " ".join(spec.path)
            lines.append(f"- `{path_label}`: {spec.description}")
        lines.append("")
    lines.extend(
        [
            "Preview mode:",
            f"- Add `preview:true` in slash or `--preview` after `{mention_prefix} ...` to suppress Discord sends and site pushes when supported.",
            "- Preview mode still runs the logic and shows would-be Discord posts.",
            "",
            "Examples:",
            f"- `{slash_prefix} system status`",
            f"- `{slash_prefix} clan members detail:full`",
            f"- `{slash_prefix} member show member:Ditika`",
            f"- `{slash_prefix} member set member:Ditika field:join-date value:2026-03-07`",
            f"- `{slash_prefix} signal show view:recent`",
            f"- `{slash_prefix} activity run activity:clan-awareness preview:true`",
            f"- `{slash_prefix} integration poap-kings publish target:promote preview:true`",
            f"- `{mention_prefix} member show \"Ditika\"`",
            f"- `{mention_prefix} member set \"King Thing\" note \"Founder and systems builder\"`",
            f"- `{mention_prefix} activity run clan-awareness --preview`",
            f"- `{mention_prefix} integration poap-kings publish data --preview`",
        ]
    )
    return "\n".join(lines)


def _legacy_hint_request(tokens: list[str], preview: bool, short: bool) -> dict | None:
    if not tokens:
        return None
    legacy_key = normalize_admin_command(tokens[0])
    if legacy_key == "jobs" and len(tokens) >= 2 and normalize_admin_command(tokens[1]) == "run":
        return _command_request(
            "jobs",
            kind="legacy_hint",
            preview=preview,
            short=short,
            hint=LEGACY_COMMAND_HINTS["jobs"],
            legacy_input=" ".join(tokens),
        )
    hint = LEGACY_COMMAND_HINTS.get(legacy_key)
    if not hint:
        return None
    return _command_request(
        legacy_key,
        kind="legacy_hint",
        preview=preview,
        short=short,
        hint=hint,
        legacy_input=" ".join(tokens),
    )


def _parse_memory_args(tokens: list[str]) -> dict | None:
    args = {"limit": "5"}
    i = 0
    free_tokens = []
    while i < len(tokens):
        token = tokens[i]
        lower = token.lower()
        if lower in {"member", "--member"} and i + 1 < len(tokens):
            args["member"] = tokens[i + 1]
            i += 2
            continue
        if lower in {"query", "search", "--query", "--search"} and i + 1 < len(tokens):
            args["query"] = tokens[i + 1]
            i += 2
            continue
        if lower in {"limit", "--limit"} and i + 1 < len(tokens):
            args["limit"] = tokens[i + 1]
            i += 2
            continue
        if lower in {"system-internal", "--system-internal", "internal", "--internal"}:
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
    return args


def _parse_birthday_value(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", str(value or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


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
    head = normalize_admin_command(filtered[0])
    tail = filtered[1:]
    if head == "help" and not tail:
        return _command_request("help", preview=preview, short=short)

    if head == "system" and tail:
        action = normalize_admin_command(tail[0])
        if action == "status" and len(tail) == 1:
            return _command_request("system.status", preview=preview)
        if action == "storage":
            view = "all"
            if len(tail) == 2:
                view = normalize_admin_command(tail[1])
            if len(tail) in {1, 2} and view in DB_STATUS_GROUPS:
                return _command_request("system.storage", args={"view": view}, preview=preview)
        if action == "schedule" and len(tail) == 1:
            return _command_request("system.schedule", preview=preview)

    if head == "clan" and tail:
        action = normalize_admin_command(tail[0])
        if action == "status" and len(tail) == 1:
            return _command_request("clan.status", args={"short": "true" if short else "false"}, preview=preview, short=short)
        if action == "war" and len(tail) == 1:
            return _command_request("clan.war", preview=preview)
        if action == "members":
            detail = "summary"
            if len(tail) == 2:
                detail = "full" if normalize_admin_command(tail[1]) in {"full", "detail:full"} else normalize_admin_command(tail[1])
            if len(tail) in {1, 2} and detail in {"summary", "full"}:
                return _command_request("clan.members", args={"detail": detail}, preview=preview)

    if head == "member" and tail:
        action = normalize_admin_command(tail[0])
        if action == "show" and len(tail) >= 2:
            return _command_request("member.show", args={"member": " ".join(tail[1:])}, preview=preview)
        if action == "verify-discord" and len(tail) >= 2:
            return _command_request("member.verify-discord", args={"member": " ".join(tail[1:])}, preview=preview)
        if action == "set" and len(tail) >= 4:
            member = tail[1]
            field = normalize_admin_command(tail[2])
            value = " ".join(tail[3:])
            if field in MEMBER_SET_FIELDS and value:
                return _command_request("member.set", args={"member": member, "field": field, "value": value}, preview=preview)
        if action == "clear" and len(tail) == 3:
            member = tail[1]
            field = normalize_admin_command(tail[2])
            if field in MEMBER_CLEAR_FIELDS:
                return _command_request("member.clear", args={"member": member, "field": field}, preview=preview)

    if head == "memory" and tail and normalize_admin_command(tail[0]) == "show":
        args = _parse_memory_args(tail[1:])
        if args is not None:
            return _command_request("memory.show", args=args, preview=preview)

    if head == "signal" and tail:
        action = normalize_admin_command(tail[0])
        if action == "show":
            view = "all"
            limit = "10"
            i = 1
            while i < len(tail):
                token = normalize_admin_command(tail[i])
                if token in SIGNAL_SHOW_VIEWS:
                    view = token
                    i += 1
                    continue
                if token in {"limit", "--limit"} and i + 1 < len(tail):
                    limit = tail[i + 1]
                    i += 2
                    continue
                return None
            return _command_request("signal.show", args={"view": view, "limit": limit}, preview=preview)
        if action == "publish-pending" and len(tail) == 1:
            return _command_request("signal.publish-pending", preview=preview)

    if head == "activity" and tail:
        action = normalize_admin_command(tail[0])
        if action == "list" and len(tail) == 1:
            return _command_request("activity.list", preview=preview)
        if action == "show" and len(tail) == 2 and normalize_activity_key(tail[1]):
            return _command_request("activity.show", args={"activity": normalize_activity_key(tail[1])}, preview=preview)
        if action == "run" and len(tail) == 2 and normalize_activity_key(tail[1]):
            return _command_request("activity.run", args={"activity": normalize_activity_key(tail[1])}, preview=preview)

    if head == "integration" and tail:
        action = normalize_admin_command(tail[0])
        if action == "list" and len(tail) == 1:
            return _command_request("integration.list", preview=preview)
        if action == "poap-kings" and len(tail) >= 2:
            subaction = normalize_admin_command(tail[1])
            if subaction == "status" and len(tail) == 2:
                return _command_request("integration.poap-kings.status", preview=preview)
            if subaction == "publish" and len(tail) == 3:
                target = normalize_admin_command(tail[2])
                if target in INTEGRATION_PUBLISH_TARGETS:
                    return _command_request("integration.poap-kings.publish", args={"target": target}, preview=preview)
    legacy = _legacy_hint_request(filtered, preview, short)
    if legacy:
        return legacy
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


def _build_signals_report(*, view: str = "all", recent_limit: int = 10, conn=None) -> str:
    import db
    from runtime.channel_subagents import signal_routing_summary

    close = conn is None
    conn = conn or db.get_connection()
    try:
        view = normalize_admin_command(view) or "all"
        if view not in SIGNAL_SHOW_VIEWS:
            raise ValueError(f"invalid signal view: {view}")
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

        lines = ["**Elixir Signals**"]
        if view in {"all", "routes"}:
            lines.extend(["", "Routing:"])
            for item in routing:
                lines.append(f"- `{item['family']}`: {item['match']}")
                for target in item["targets"]:
                    requirement = "required" if target.get("required") else "optional"
                    condition = f" - {target['condition']}" if target.get("condition") else ""
                    lines.append(
                        f"  -> `{target['subagent']}` `{target['intent']}` ({requirement}){condition}"
                    )

        if view in {"all", "recent"}:
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

        if view in {"all", "pending"}:
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


def _build_activity_list_report() -> str:
    import elixir

    specs = schedule_specs_from_registry(elixir)
    lines = [f"**Elixir Activities ({len(specs)})**"]
    for spec in specs:
        lines.append(
            f"- `{spec['activity_key']}` — {spec['owner_subagent']} — {spec['schedule']}"
        )
        lines.append(
            f"  {spec['purpose']}"
        )
    return "\n".join(lines)


def _build_activity_show_report(activity_key: str) -> str:
    import elixir

    resolved = resolve_activity(activity_key, elixir)
    lines = [f"**Activity: {resolved['activity_key']}**"]
    lines.append(f"- Owner: `{resolved['owner_subagent']}`")
    lines.append(f"- Purpose: {resolved['purpose']}")
    lines.append(f"- Job: `{resolved['job_function']}`")
    lines.append(f"- Schedule: {next(spec['schedule'] for spec in schedule_specs_from_registry(elixir) if spec['activity_key'] == resolved['activity_key'])}")
    lines.append(f"- Manual trigger: {'yes' if resolved['manual_trigger_allowed'] else 'no'}")
    lines.append("- Delivery targets:")
    for target in resolved["delivery_targets"]:
        lines.append(f"  - {target}")
    return "\n".join(lines)


def _build_integration_list_report() -> str:
    lines = ["**Elixir Integrations**"]
    lines.append("- `poap-kings` — Website publishing and content sync for poapkings.com.")
    lines.append("  Commands: `integration poap-kings status`, `integration poap-kings publish <target>`")
    return "\n".join(lines)


def _build_poap_kings_status_report() -> str:
    import runtime.jobs as runtime_jobs
    from integrations.poap_kings import site as poap_kings_site

    repo = poap_kings_site._site_repo()
    branch = poap_kings_site._site_branch()
    enabled = poap_kings_site.site_enabled()
    lines = ["**Integration: poap-kings**"]
    lines.append(f"- Enabled: {'yes' if enabled else 'no'}")
    lines.append(f"- Repo: `{repo}`")
    lines.append(f"- Branch: `{branch}`")
    lines.append(f"- Visibility channel: `#poapkings-com`")
    lines.append("- Targets:")
    for target in ("all", "data", "home", "members", "roster-bios", "promote"):
        lines.append(f"  - `{target}`")
    status_snapshot = runtime_jobs.runtime_status.snapshot() if hasattr(runtime_jobs, "runtime_status") else {}
    jobs = status_snapshot.get("jobs") or {}
    for job_name in ("site_content_cycle", "site_data_refresh", "promotion_content_cycle", "weekly_clan_recap"):
        if job_name in jobs:
            entry = jobs[job_name]
            lines.append(
                f"- Job `{job_name}`: {entry.get('status') or 'n/a'} | {_truncate_for_report(entry.get('detail') or '', 100)}"
            )
    return "\n".join(lines)


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
                return f"`signal.publish-pending` failed in preview mode: {exc}"
            summary = f"Published {count} pending system signal(s) in preview mode."
            return f"{summary}\n\n{_format_preview_posts(captured_posts)}"

    try:
        count = await elixir._publish_pending_system_signal_updates(seed_startup_signals=True)
    except Exception as exc:
        return f"`signal.publish-pending` failed: {exc}"
    return f"Published {count} pending system signal(s)."


async def _run_poap_kings_sync(preview: bool) -> str:
    import elixir

    if preview:
        async with _preview_job_runtime() as captured_posts:
            try:
                await elixir._site_content_cycle()
                members_text = await _run_members_message(preview=True)
            except Exception as exc:
                return f"`integration.poap-kings.publish all` failed in preview mode: {exc}"
            sections = [
                "Published `integration.poap-kings` target `all` in preview mode.",
                "",
                _format_preview_posts(captured_posts),
            ]
            if members_text:
                sections.extend(["", "**Members Page Weekly Recap Preview**", members_text])
            return "\n".join(section for section in sections if section is not None)

    await elixir._site_content_cycle()
    await _run_members_message(preview=False)
    return "Published `integration.poap-kings` target `all`."


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
    if command == "clear-discord":
        if preview:
            return f"Preview: would clear the Discord link for {label}."
        await asyncio.to_thread(db.clear_member_discord_link, member_tag)
        return f"Cleared the Discord link for {label}."
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


def _translate_member_field_command(action: str, field: str, value: str | None = None) -> tuple[str, dict]:
    field = normalize_admin_command(field)
    if action == "set":
        if field == "discord":
            return "set-discord", {"discord_name": value}
        if field == "join-date":
            return "set-join-date", {"date": value}
        if field == "birthday":
            parsed = _parse_birthday_value(value or "")
            if not parsed:
                raise ValueError("birthday value must be in MM-DD format")
            month, day = parsed
            return "set-birthday", {"month": month, "day": day}
        if field == "profile-url":
            return "set-profile-url", {"url": value}
        if field == "poap-address":
            return "set-poap-address", {"poap_address": value}
        if field == "note":
            return "set-note", {"note": value}
    if action == "clear":
        return f"clear-{field}", {}
    raise ValueError(f"Unsupported member field action: {action} {field}")


async def _run_member_field_command(action: str, *, preview: bool, args: dict) -> str:
    member_args = {"member": args["member"]}
    command, extra_args = _translate_member_field_command(action, args["field"], args.get("value"))
    member_args.update(extra_args)
    return await _run_member_metadata_command(command, preview=preview, args=member_args)


async def _run_integration_publish(target: str, *, preview: bool) -> str:
    target = normalize_admin_command(target)
    if target == "all":
        return await _run_poap_kings_sync(preview=preview)
    if target == "data":
        return await _run_runtime_job("poap-kings-data-sync", preview=preview)
    if target == "home":
        return await _run_home_message(preview=preview)
    if target == "members":
        return await _run_members_message(preview=preview)
    if target == "roster-bios":
        return await _run_roster_bios(preview=preview)
    if target == "promote":
        return await _run_promote_content(preview=preview)
    raise ValueError(f"Unknown integration publish target: {target}")


async def dispatch_admin_command(command: str | dict, *, preview: bool = False, short: bool = False, args: dict | None = None) -> str:
    import elixir

    if isinstance(command, dict):
        request = dict(command)
    else:
        command_key = normalize_admin_command(str(command))
        if command_key in LEGACY_COMMAND_HINTS:
            request = _command_request(
                command_key,
                kind="legacy_hint",
                hint=LEGACY_COMMAND_HINTS[command_key],
                legacy_input=command_key,
                preview=preview,
                short=short,
            )
        else:
            request = _command_request(command_key, args=args or {}, preview=preview, short=short)

    args = request.get("args") or {}
    preview = bool(request.get("preview", False))
    short = bool(request.get("short", False))
    key = normalize_admin_command(request.get("key") or request.get("command"))

    if request.get("kind") == "legacy_hint":
        return request.get("hint") or "That command was renamed."

    if key == "help":
        return render_admin_help()
    if key == "system.status":
        return elixir._build_status_report()
    if key == "system.storage":
        group = args.get("view")
        return elixir._build_db_status_report(group=None if group in {None, "", "all"} else group)
    if key == "system.schedule":
        return elixir._build_schedule_report()
    if key == "signal.show":
        return await asyncio.to_thread(
            _build_signals_report,
            view=args.get("view", "all"),
            recent_limit=args.get("limit", 10),
        )
    if key == "clan.status":
        clan, war = await elixir._load_live_clan_context()
        if short:
            return elixir._build_clan_status_short_report(clan, war)
        return elixir._build_clan_status_report(clan, war)
    if key == "clan.war":
        clan, war = await elixir._load_live_clan_context()
        return elixir._build_war_status_report(clan, war)
    if key == "clan.members":
        return await asyncio.to_thread(
            _build_clan_list_report,
            full=normalize_admin_command(args.get("detail")) == "full",
        )
    if key == "member.show":
        return await asyncio.to_thread(_build_member_profile_report, args["member"])
    if key == "memory.show":
        return await asyncio.to_thread(
            _build_memory_report,
            member_query=args.get("member"),
            query=args.get("query"),
            limit=args.get("limit", 5),
            include_system_internal=str(args.get("include_system_internal", "")).lower() in {"1", "true", "yes", "on"},
        )
    if key == "member.verify-discord":
        return await _run_verify_discord(preview=preview, args=args)
    if key == "member.set":
        return await _run_member_field_command("set", preview=preview, args=args)
    if key == "member.clear":
        return await _run_member_field_command("clear", preview=preview, args=args)
    if key == "signal.publish-pending":
        return await _run_system_signals(preview=preview)
    if key == "activity.list":
        return await asyncio.to_thread(_build_activity_list_report)
    if key == "activity.show":
        return await asyncio.to_thread(_build_activity_show_report, args["activity"])
    if key == "activity.run":
        return await _run_runtime_job(args["activity"], preview=preview)
    if key == "integration.list":
        return await asyncio.to_thread(_build_integration_list_report)
    if key == "integration.poap-kings.status":
        return await asyncio.to_thread(_build_poap_kings_status_report)
    if key == "integration.poap-kings.publish":
        return await _run_integration_publish(args["target"], preview=preview)
    raise ValueError(f"Unknown admin command: {key}")


__all__ = [
    "LEADER_ONLY_COMMANDS",
    "admin_command_requires_leader",
    "_build_signals_report",
    "COMMAND_HELP",
    "COMMAND_ORDER",
    "COMMAND_SPECS",
    "dispatch_admin_command",
    "normalize_admin_command",
    "parse_admin_command",
    "render_admin_help",
]
