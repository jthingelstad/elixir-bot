from __future__ import annotations

import asyncio
import re
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from runtime.activities import (
    normalize_activity_key,
    resolve_activity,
    schedule_specs_from_registry,
)
from runtime.activity_runner import (
    ActivityRunError,
    ManualActivityNotAllowed,
    UnknownActivityError,
    run_activity_once,
)
from storage.contextual_memory import (
    archive_member_note_memory,
    upsert_member_note_memory,
)


@dataclass(frozen=True)
class AdminCommandSpec:
    key: str
    path: tuple[str, ...]
    description: str
    leader_only: bool = False
    write: bool = False
    event_type: str | None = None
    root: str = "clanops"

    @property
    def discord_path(self) -> tuple[str, ...]:
        return (self.root, *self.path)


RELAY_STATUS_VIEWS = {"all", "pending", "decided"}

COMMAND_SPECS = {
    "elixir.help": AdminCommandSpec(
        "elixir.help",
        ("help",),
        "How to use Elixir as a clan member.",
        event_type="elixir_help",
        root="elixir",
    ),
    "email.set": AdminCommandSpec(
        "email.set",
        ("email", "set"),
        "Add an email to your profile; I'll send a code to verify it.",
        write=True,
        event_type="email_set",
        root="elixir",
    ),
    "email.verify": AdminCommandSpec(
        "email.verify",
        ("email", "verify"),
        "Enter the 6-digit code I emailed you.",
        write=True,
        event_type="email_verify",
        root="elixir",
    ),
    "email.show": AdminCommandSpec(
        "email.show",
        ("email", "show"),
        "Show the email on your profile.",
        event_type="email_show",
        root="elixir",
    ),
    "help": AdminCommandSpec(
        "help", ("help",), "Show the Elixir operator help page.", event_type="help"
    ),
    "release": AdminCommandSpec(
        "release",
        ("release",),
        "Draft or publish Elixir release notes (leader).",
        leader_only=True,
        write=True,
        event_type="release",
    ),
    # system.* / memory.show were removed from Discord; the Observatory owns that
    # telemetry. signal.publish-pending is gone entirely — API drift is an
    # operator concern, worked from the AGENT-TEAM Error Watch runbook
    # (AGENT-TEAM/operations-manager.md), not a queue of its own.
    "clan.status": AdminCommandSpec(
        "clan.status",
        ("clan", "status"),
        "Show the operational clan status report.",
        event_type="clan_status_report",
    ),
    "clan.war": AdminCommandSpec(
        "clan.war",
        ("clan", "war"),
        "Show the live war-awareness report.",
        event_type="war_status_report",
    ),
    "clan.members": AdminCommandSpec(
        "clan.members",
        ("clan", "members"),
        "List active clan members.",
        event_type="clan_members_report",
    ),
    "member.show": AdminCommandSpec(
        "member.show",
        ("member", "show"),
        "Show the stored member profile and metadata for one member.",
        event_type="member_profile_report",
    ),
    "member.verify-discord": AdminCommandSpec(
        "member.verify-discord",
        ("member", "verify-discord"),
        "Verify a member's Discord link and Member role.",
        leader_only=True,
        write=True,
        event_type="member_verify_discord",
    ),
    "member.audit-discord": AdminCommandSpec(
        "member.audit-discord",
        ("member", "audit-discord"),
        "Audit Discord ↔ clan member linkage and surface gaps.",
        leader_only=True,
        event_type="member_audit_discord",
    ),
    "member.email": AdminCommandSpec(
        "member.email",
        ("member", "email"),
        "Show or set a member's email (leader).",
        leader_only=True,
        write=True,
        event_type="member_email",
    ),
    "member.set": AdminCommandSpec(
        "member.set",
        ("member", "set"),
        "Set one member field.",
        leader_only=True,
        write=True,
        event_type="member_set",
    ),
    "member.clear": AdminCommandSpec(
        "member.clear",
        ("member", "clear"),
        "Clear one member field.",
        leader_only=True,
        write=True,
        event_type="member_clear",
    ),
    "relay.status": AdminCommandSpec(
        "relay.status",
        ("relay", "status"),
        "Show leader action recommendations and reaction decisions.",
        leader_only=True,
        event_type="relay_status_report",
    ),
    "relay.test-card": AdminCommandSpec(
        "relay.test-card",
        ("relay", "test-card"),
        "Post a test #actions leader action card for a real action type.",
        leader_only=True,
        write=True,
        event_type="relay_test_card",
    ),
    "activity.list": AdminCommandSpec(
        "activity.list",
        ("activity", "list"),
        "List registered recurring activities.",
        event_type="activity_list",
    ),
    "activity.show": AdminCommandSpec(
        "activity.show",
        ("activity", "show"),
        "Show one recurring activity in detail.",
        event_type="activity_show",
    ),
    "activity.run": AdminCommandSpec(
        "activity.run",
        ("activity", "run"),
        "Run one registered activity now.",
        leader_only=True,
        write=True,
        event_type="activity_run",
    ),
    "tournament.watch": AdminCommandSpec(
        "tournament.watch",
        ("tournament", "watch"),
        "Start watching a tournament by tag.",
        leader_only=True,
        write=True,
        event_type="tournament_watch",
    ),
    "tournament.status": AdminCommandSpec(
        "tournament.status",
        ("tournament", "status"),
        "Show active tournament tracking status.",
        event_type="tournament_status",
    ),
    "tournament.stop": AdminCommandSpec(
        "tournament.stop",
        ("tournament", "stop"),
        "Stop watching the active tournament.",
        leader_only=True,
        write=True,
        event_type="tournament_stop",
    ),
    "tournament.recap": AdminCommandSpec(
        "tournament.recap",
        ("tournament", "recap"),
        "Generate or regenerate a tournament recap.",
        leader_only=True,
        write=True,
        event_type="tournament_recap",
    ),
    "tournament.history": AdminCommandSpec(
        "tournament.history",
        ("tournament", "history"),
        "List past tournaments.",
        event_type="tournament_history",
    ),
}

_COMMAND_SPEC_BY_DISCORD_PATH = {spec.discord_path: spec for spec in COMMAND_SPECS.values()}
if len(_COMMAND_SPEC_BY_DISCORD_PATH) != len(COMMAND_SPECS):
    raise RuntimeError("Admin command specs must have unique Discord paths")

COMMAND_GROUP_ORDER = [
    "clan",
    "member",
    "relay",
    "activity",
    "integration",
]

COMMAND_HELP = {key: spec.description for key, spec in COMMAND_SPECS.items()}
LEADER_ONLY_COMMANDS = {key for key, spec in COMMAND_SPECS.items() if spec.leader_only}
COMMAND_ORDER = list(COMMAND_SPECS)


def _command_request(
    key: str,
    *,
    args: dict | None = None,
    preview: bool = False,
    short: bool = False,
) -> dict:
    spec = COMMAND_SPECS.get(key)
    path = spec.path if spec else tuple()
    return {
        "kind": "command",
        "key": key,
        "command": key,
        "resource": path[0] if path else None,
        "action": path[-1] if path else None,
        "path": path,
        "args": args or {},
        "preview": preview,
        "short": short,
    }


def admin_command_requires_leader(command: str | dict) -> bool:
    key = command.get("key") if isinstance(command, dict) else str(command or "")
    return key in LEADER_ONLY_COMMANDS


def normalize_admin_command(command: str) -> str:
    return str(command or "").strip().lower()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_admin_help(*, slash_prefix: str = "/clanops") -> str:
    grouped = {group: [] for group in COMMAND_GROUP_ORDER}
    for spec in COMMAND_SPECS.values():
        if spec.key == "help":
            continue
        grouped.setdefault(spec.path[0], []).append(spec)

    lines = [
        "**Elixir Admin Commands**",
        f"Use grouped slash commands under `{slash_prefix} ...` in `#clanops` for private replies.",
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
            "- Add `preview:true` to suppress Discord sends and site pushes when supported.",
            "- Preview mode still runs the logic and shows would-be Discord posts.",
            "",
            "Examples:",
            f"- `{slash_prefix} clan status`",
            f"- `{slash_prefix} clan members detail:full`",
            f"- `{slash_prefix} member show member:Ditika`",
            f"- `{slash_prefix} member set member:Ditika field:join-date value:2026-03-07`",
            f"- `{slash_prefix} relay status view:pending`",
            f"- `{slash_prefix} activity run activity:engine-tick preview:true`",
        ]
    )
    return "\n".join(lines)


def _parse_birthday_value(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", str(value or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2)


class _PreviewChannel:
    def __init__(self, channel_id: int, name: str, captured_posts: list[tuple[str, str]]):
        self.id = channel_id
        self.name = name.lstrip("#")
        self.type = "text"
        self._captured_posts = captured_posts

    async def send(self, content: str | None = None, **kwargs):
        if content is None:
            embed = kwargs.get("embed")
            title = getattr(embed, "title", None) if embed is not None else None
            content = f"[embed] {title}" if title else "[non-text message]"
        self._captured_posts.append((self.name, str(content)))
        return SimpleNamespace(id=len(self._captured_posts))


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
            lines.append(
                f"- {name} — joined {joined_date} — birthday {birthday} — profile {profile_flag}"
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
    best_trophies = (
        f"{profile['best_trophies']:,}" if isinstance(profile.get("best_trophies"), int) else None
    )
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
        f"profile {_fmt_optional(profile.get('profile_url'))}"
    )
    lines.append(
        f"- Clan state: Collection Level {_fmt_optional(profile.get('cr_collection_level'))} | trophies {_fmt_optional(trophies)} | "
        f"best {_fmt_optional(best_trophies)} | donations {_fmt_optional(profile.get('donations_week'))} | received {_fmt_optional(profile.get('donations_received_week'))}"
    )
    email_display = None
    if profile.get("email"):
        email_display = f"{profile['email']} ({'verified' if profile.get('email_verified_at') else 'unverified'})"
    lines.append(
        f"- Discord + contact: linked {'yes' if profile.get('in_discord') else 'no'} | "
        f"handle {_fmt_optional(profile.get('discord_display_name') or profile.get('discord_username'))} | "
        f"last seen {_fmt_optional(profile.get('discord_last_seen_at'))} | "
        f"email {_fmt_optional(email_display)} | note {_fmt_optional(profile.get('note'))}"
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


def _format_leader_action_outcome(action: dict) -> str:
    outcome = action.get("outcome") or {}
    if action.get("action_type") == "in_game_relay":
        deltas = outcome.get("deltas") or {}
        bits = []
        for key, label in (
            ("engaged_count", "engaged"),
            ("finished_count", "finished"),
            ("untouched_count", "untouched"),
            ("clan_fame", "fame"),
        ):
            value = deltas.get(key)
            if value is not None:
                sign = "+" if value > 0 else ""
                bits.append(f"{label} {sign}{value}")
        return " | ".join(bits) if bits else "outcome pending more data"
    if action.get("action_type") in {
        "promotion_recommendation",
        "kick_recommendation",
        "demotion_recommendation",
    }:
        changed = outcome.get("changed") or {}
        if changed:
            return " | ".join(
                f"{key} changed {'yes' if value else 'no'}" for key, value in changed.items()
            )
    return "outcome pending"


def _format_leader_action_line(action: dict) -> str:
    action_id = action.get("action_id")
    status = action.get("status") or "unknown"
    kind = action.get("action_type") or "leader_action"
    objective = action.get("objective") or "n/a"
    prompt = _truncate_for_report(action.get("prompt_text") or "", 110)
    decided = action.get("decided_at")
    by = action.get("decided_by_discord_user_id")
    decision = f" | decided {decided}" if decided else ""
    if by:
        decision += f" by <@{by}>"
    note = ""
    if action.get("decision_note"):
        note = f" | note: {_truncate_for_report(action.get('decision_note'), 90)}"
    outcome = ""
    if status == "done":
        outcome = f" | {_format_leader_action_outcome(action)}"
    return f"- `R{action_id}` `{status}` `{kind}` `{objective}`: {prompt}{decision}{note}{outcome}"


def _build_relay_status_report(*, view: str = "all", limit: int = 10, conn=None) -> str:
    import db

    close = conn is None
    conn = conn or db.get_connection()
    try:
        view = normalize_admin_command(view) or "all"
        if view not in RELAY_STATUS_VIEWS:
            raise ValueError(f"invalid relay status view: {view}")
        limit = max(1, min(int(limit or 10), 25))

        if view == "pending":
            actions = db.list_leader_actions(status=db.ACTION_PROPOSED, limit=limit, conn=conn)
        elif view == "decided":
            actions = [
                action
                for action in db.list_leader_actions(limit=limit * 3, conn=conn)
                if action.get("status") in {db.ACTION_DONE, db.ACTION_REJECTED}
            ][:limit]
        else:
            actions = db.list_leader_actions(limit=limit, conn=conn)

        refreshed = []
        for action in actions:
            if action.get("status") == db.ACTION_DONE:
                refreshed.append(
                    db.refresh_leader_action_outcome(action["action_id"], conn=conn) or action
                )
            else:
                refreshed.append(action)

        pending_count = len(db.list_leader_actions(status=db.ACTION_PROPOSED, limit=50, conn=conn))
        lines = ["**Arena Relay Leader Actions**"]
        lines.append(f"- Pending: {pending_count}")
        lines.append(
            "- Feedback: ✅/☑️ means done, ❌ means declined (engine re-nominates on sustained evidence)"
        )
        lines.append("")
        if not refreshed:
            lines.append("_No leader actions recorded yet._")
        else:
            for action in refreshed:
                lines.append(_format_leader_action_line(action))
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
            f"- `{spec['activity_key']}` — {spec['owner_lane']} — {spec['activity_role']} — {spec['schedule']}"
        )
        lines.append(f"  {spec['purpose']}")
    return "\n".join(lines)


def _build_activity_show_report(activity_key: str) -> str:
    import elixir

    resolved = resolve_activity(activity_key, elixir)
    lines = [f"**Activity: {resolved['activity_key']}**"]
    lines.append(f"- Owner lane: `{resolved['owner_lane']}`")
    lines.append(f"- Role: {resolved['activity_role']}")
    lines.append(f"- Purpose: {resolved['purpose']}")
    lines.append(f"- Job: `{resolved['job_function']}`")
    lines.append(
        f"- Schedule: {next(spec['schedule'] for spec in schedule_specs_from_registry(elixir) if spec['activity_key'] == resolved['activity_key'])}"
    )
    lines.append(f"- Manual trigger: {'yes' if resolved['manual_trigger_allowed'] else 'no'}")
    lines.append("- Delivery targets:")
    for target in resolved["delivery_targets"]:
        lines.append(f"  - {target}")
    return "\n".join(lines)


@asynccontextmanager
async def _preview_job_runtime():
    import prompts
    import runtime.app as runtime_app
    from runtime import jobs as runtime_jobs

    captured_posts: list[tuple[str, str]] = []
    channels = {
        channel["id"]: _PreviewChannel(channel["id"], channel["name"], captured_posts)
        for channel in prompts.discord_channel_configs()
    }
    stack = ExitStack()
    try:
        preview_bot = _ChannelLookup(channels)
        stack.enter_context(patch.object(runtime_app, "bot", preview_bot))
        if hasattr(runtime_jobs, "bot"):
            stack.enter_context(patch.object(runtime_jobs, "bot", preview_bot))
        yield captured_posts
    finally:
        stack.close()


async def _run_runtime_job(job_name: str, preview: bool) -> str:
    import elixir

    activity_key = normalize_activity_key(job_name)
    if not activity_key:
        return f"Unknown job: {job_name}"
    resolved = resolve_activity(activity_key, elixir)
    display_name = resolved["activity_key"]
    if not resolved["manual_trigger_allowed"]:
        return f"`{display_name}` cannot be run manually."
    if preview:
        async with _preview_job_runtime() as captured_posts:
            try:
                await run_activity_once(display_name, runtime_module=elixir)
            except Exception as exc:
                return f"`{display_name}` failed in preview mode: {exc}"
            return (
                f"Ran `{display_name}` in preview mode.\n\n{_format_preview_posts(captured_posts)}"
            )
    try:
        await run_activity_once(display_name, runtime_module=elixir)
    except (UnknownActivityError, ManualActivityNotAllowed, ActivityRunError) as exc:
        return f"`{display_name}` failed: {exc}"
    except Exception as exc:
        return f"`{display_name}` failed: {exc}"
    return f"Ran `{display_name}`."


_MEMBER_QUERY_MAX_LEN = 64


def _resolve_member_tag(member_query: str, *, conn=None) -> tuple[str, str]:
    import db
    from storage.roster import pick_best_match

    query = (member_query or "").strip()
    if not query:
        raise ValueError("Member name or tag is required.")
    if len(query) > _MEMBER_QUERY_MAX_LEN:
        raise ValueError(f"Member name/tag must be {_MEMBER_QUERY_MAX_LEN} characters or fewer.")
    tag_match = re.search(r"(#?[A-Z0-9]+)\)$", query, re.IGNORECASE)
    if tag_match:
        matches = db.resolve_member(tag_match.group(1), limit=3, conn=conn)
    else:
        matches = db.resolve_member(query, limit=3, conn=conn)
    if not matches:
        raise ValueError(f"No clan member matched {member_query!r}.")
    best = pick_best_match(matches)
    if best is None:
        choices = ", ".join(
            item.get("member_ref_with_handle") or item.get("current_name") or item.get("player_tag")
            for item in matches[:3]
        )
        raise ValueError(f"Ambiguous member {member_query!r}. Top matches: {choices}")
    return best["player_tag"], best.get("member_ref_with_handle") or best.get(
        "current_name"
    ) or best["player_tag"]


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


def _suggest_clan_member_for_discord_user(display_values: list[str]) -> str | None:
    import db as _db

    for value in display_values:
        if not value:
            continue
        matches = _db.resolve_member(value, limit=2)
        if not matches:
            continue
        top = matches[0]
        exactish = top.get("match_source") in {
            "current_name_exact",
            "alias_exact",
            "player_tag_exact",
        }
        if exactish and (
            len(matches) == 1 or top.get("match_score", 0) - matches[1].get("match_score", 0) >= 100
        ):
            name = top.get("current_name") or top.get("member_name") or top.get("player_tag")
            return f"{name} (`{top['player_tag']}`)"
    return None


async def _run_member_audit_discord() -> str:
    import db
    import runtime.app as app

    guild = app.bot.get_guild(app.GUILD_ID) if app.GUILD_ID else None
    if guild is None:
        return "Guild not cached in the running bot."

    member_role = guild.get_role(app.MEMBER_ROLE_ID) if app.MEMBER_ROLE_ID else None

    clan_members = await asyncio.to_thread(db.list_members, "active")
    unlinked_clan = [m for m in clan_members if not m.get("discord_user_id")]

    unlinked_discord: list[tuple[object, str | None]] = []
    role_missing: list[object] = []
    for guild_member in guild.members:
        if guild_member.bot:
            continue
        link = await asyncio.to_thread(db.get_linked_member_for_discord_user, guild_member.id)
        if link:
            if member_role and member_role not in guild_member.roles:
                role_missing.append(guild_member)
            continue
        display_values = [
            getattr(guild_member, "nick", None),
            getattr(guild_member, "display_name", None),
            getattr(guild_member, "global_name", None),
            getattr(guild_member, "name", None),
        ]
        suggestion = await asyncio.to_thread(
            _suggest_clan_member_for_discord_user,
            [v for v in display_values if v],
        )
        unlinked_discord.append((guild_member, suggestion))

    lines = [
        "**Discord ↔ Clan Member Audit**",
        f"- Active clan members: {len(clan_members)} ({len(unlinked_clan)} without a Discord link)",
        f"- Unlinked Discord users: {len(unlinked_discord)}",
        f"- Linked users missing the Member role: {len(role_missing)}",
        "",
    ]

    if unlinked_clan:
        lines.append("**Clan members without a Discord link**")
        for m in unlinked_clan[:25]:
            name = m.get("current_name") or m.get("member_name") or m.get("player_tag")
            lines.append(f"- {name} (`{m['player_tag']}`)")
        if len(unlinked_clan) > 25:
            lines.append(f"- …and {len(unlinked_clan) - 25} more")
        lines.append("")

    if unlinked_discord:
        lines.append("**Discord users not linked to a clan member**")
        for guild_member, suggestion in unlinked_discord[:25]:
            label = f"{guild_member.display_name} (<@{guild_member.id}>)"
            if suggestion:
                lines.append(
                    f"- {label} → likely **{suggestion}**. Run `/clanops member verify-discord member:{suggestion.split(' (`')[0]}`."
                )
            else:
                lines.append(
                    f"- {label} → no confident match. Use `/clanops member set` to link manually."
                )
        if len(unlinked_discord) > 25:
            lines.append(f"- …and {len(unlinked_discord) - 25} more")
        lines.append("")

    if role_missing:
        lines.append("**Linked users missing the Member role**")
        for guild_member in role_missing[:25]:
            lines.append(
                f"- {guild_member.display_name} (<@{guild_member.id}>) — run `/clanops member verify-discord` to reapply"
            )
        if len(role_missing) > 25:
            lines.append(f"- …and {len(role_missing) - 25} more")
        lines.append("")

    if not unlinked_clan and not unlinked_discord and not role_missing:
        lines.append("Everything is linked.")

    return "\n".join(lines).rstrip()


def _translate_member_field_command(
    action: str, field: str, value: str | None = None
) -> tuple[str, dict]:
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


async def dispatch_admin_command(
    command: str | dict,
    *,
    preview: bool = False,
    short: bool = False,
    args: dict | None = None,
) -> str:
    import elixir

    if isinstance(command, dict):
        request = dict(command)
    else:
        request = _command_request(
            normalize_admin_command(str(command)),
            args=args or {},
            preview=preview,
            short=short,
        )

    args = request.get("args") or {}
    preview = bool(request.get("preview", False))
    short = bool(request.get("short", False))
    key = normalize_admin_command(request.get("key") or request.get("command"))

    if key == "help":
        return render_admin_help()
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
    if key == "relay.status":
        return await asyncio.to_thread(
            _build_relay_status_report,
            view=args.get("view", "all"),
            limit=args.get("limit", 10),
        )
    if key == "member.verify-discord":
        return await _run_verify_discord(preview=preview, args=args)
    if key == "member.audit-discord":
        return await _run_member_audit_discord()
    if key == "member.set":
        return await _run_member_field_command("set", preview=preview, args=args)
    if key == "member.clear":
        return await _run_member_field_command("clear", preview=preview, args=args)
    if key == "activity.list":
        return await asyncio.to_thread(_build_activity_list_report)
    if key == "activity.show":
        return await asyncio.to_thread(_build_activity_show_report, args["activity"])
    if key == "activity.run":
        return await _run_runtime_job(args["activity"], preview=preview)
    raise ValueError(f"Unknown admin command: {key}")


__all__ = [
    "LEADER_ONLY_COMMANDS",
    "admin_command_requires_leader",
    "_build_relay_status_report",
    "COMMAND_HELP",
    "COMMAND_ORDER",
    "COMMAND_SPECS",
    "dispatch_admin_command",
    "normalize_admin_command",
    "render_admin_help",
]
