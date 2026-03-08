from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch


COMMAND_HELP = {
    "help": "Show the Elixir operator help page.",
    "status": "Show Elixir runtime health, last jobs, and current telemetry.",
    "schedule": "Show the configured job cadence and next scheduler runs.",
    "clan-status": "Fetch live clan/war data and print the operational clan status report.",
    "heartbeat": "Force one heartbeat cycle now.",
    "site-data": "Force the site data refresh job now.",
    "site-content": "Force the full site content cycle now.",
    "site-publish": "Commit and push the current Elixir-owned website files now.",
    "home-message": "Regenerate only the website home message.",
    "members-message": "Regenerate only the website members message.",
    "roster-bios": "Regenerate only the website roster intro and member bios.",
    "promote-content": "Regenerate only the website promotion payload locally.",
    "promotion": "Force the promotion content sync now. This updates the website and #promote-the-clan together.",
    "player-intel": "Force the player intel refresh job now.",
    "clanops-review": "Force the weekly clanops review post now.",
}

COMMAND_ORDER = [
    "help",
    "status",
    "schedule",
    "clan-status",
    "heartbeat",
    "site-data",
    "site-content",
    "site-publish",
    "home-message",
    "members-message",
    "roster-bios",
    "promote-content",
    "promotion",
    "player-intel",
    "clanops-review",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_admin_help(*, channel_prefix: str = "do", cli_prefix: str = "venv/bin/python scripts/elixir_do.py") -> str:
    lines = [
        "**Elixir Admin Commands**",
        f"Use `{channel_prefix} <command>` in `#clanops` or `{cli_prefix} <command>` in the terminal.",
        "",
        "Commands:",
    ]
    for name in COMMAND_ORDER:
        lines.append(f"- `{name}`: {COMMAND_HELP[name]}")
    lines.extend(
        [
            "",
            "Preview mode:",
            f"- Add `--preview`, or use `{channel_prefix} <command> preview`, to suppress Discord sends and site pushes.",
            "- Preview mode still runs the job logic and shows would-be Discord posts.",
            "",
            "Examples:",
            f"- `{channel_prefix} heartbeat --preview`",
            f"- `{channel_prefix} site-content`",
            f"- `{channel_prefix} promotion --preview`",
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
        filtered.append(lower)

    if not filtered:
        return None

    command = filtered[0]
    extra = filtered[1:]

    if command == "help" and not extra:
        return {"command": "help", "preview": preview, "short": False}
    if command in {"status", "schedule"} and not extra:
        return {"command": command, "preview": preview, "short": False}
    if command == "clan-status" and not extra:
        return {"command": command, "preview": preview, "short": short}
    if command in COMMAND_HELP and command not in {"help", "status", "schedule", "clan-status"} and not extra:
        return {"command": command, "preview": preview, "short": False}
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
            patch("runtime.jobs.site_content.commit_and_push", side_effect=lambda *args, **kwargs: None)
        )
        yield captured_posts
    finally:
        stack.close()


async def _load_site_context():
    import elixir
    import site_content

    clan, war = await elixir._load_live_clan_context()
    roster = await asyncio.to_thread(site_content.load_current, "roster")
    if roster is None and clan.get("memberList"):
        roster = await asyncio.to_thread(site_content.build_roster_data, clan, True)
        await asyncio.to_thread(site_content.write_content, "roster", roster)
    return clan, war, roster


async def _run_runtime_job(job_name: str, preview: bool) -> str:
    import elixir

    job_map = {
        "heartbeat": elixir._heartbeat_tick,
        "site-data": elixir._site_data_refresh,
        "site-content": elixir._site_content_cycle,
        "promotion": elixir._promotion_content_cycle,
        "player-intel": elixir._player_intel_refresh,
        "clanops-review": elixir._clanops_weekly_review,
    }
    if preview:
        async with _preview_job_runtime() as captured_posts:
            await job_map[job_name]()
            return f"Ran `{job_name}` in preview mode.\n\n{_format_preview_posts(captured_posts)}"
    await job_map[job_name]()
    return f"Ran `{job_name}`."


async def _run_site_publish(preview: bool) -> str:
    import site_content

    if preview:
        return "Preview mode: site publish skipped."
    ok = await asyncio.to_thread(site_content.commit_and_push, "Elixir manual site publish")
    if not ok:
        raise RuntimeError("site publish failed")
    return "Site content published."


async def _run_home_message(preview: bool) -> str:
    import elixir
    import site_content

    clan, war, roster = await _load_site_context()
    previous = await asyncio.to_thread(site_content.load_current, "home")
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
    await asyncio.to_thread(site_content.write_content, "home", payload)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir home message update")
    return text


async def _run_members_message(preview: bool) -> str:
    import elixir
    import site_content

    clan, war, roster = await _load_site_context()
    previous = await asyncio.to_thread(site_content.load_current, "members")
    previous_message = previous.get("message", "") if previous else ""
    text = await asyncio.to_thread(
        elixir.elixir_agent.generate_members_message,
        clan,
        war,
        previous_message,
        roster_data=roster,
    )
    if not text:
        raise RuntimeError("members message generation returned nothing")
    payload = {"message": text, "generated": _utcnow()}
    await asyncio.to_thread(site_content.write_content, "members", payload)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir members message update")
    return text


async def _run_roster_bios(preview: bool) -> str:
    import db
    import elixir
    import site_content

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
    await asyncio.to_thread(site_content.write_content, "roster", roster_payload)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir roster bio update")
    return roster_payload.get("intro", "")


async def _run_promote_content(preview: bool) -> str:
    import elixir
    import site_content

    clan, war, roster = await _load_site_context()
    promote = await asyncio.to_thread(
        elixir.elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=roster,
    )
    if not promote:
        raise RuntimeError("promotion content generation returned nothing")
    await asyncio.to_thread(site_content.write_content, "promote", promote)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir promotion content update")
    return json.dumps(promote, indent=2)


async def dispatch_admin_command(command: str, *, preview: bool = False, short: bool = False) -> str:
    import elixir

    if command == "help":
        return render_admin_help()
    if command == "status":
        return elixir._build_status_report()
    if command == "schedule":
        return elixir._build_schedule_report()
    if command == "clan-status":
        clan, war = await elixir._load_live_clan_context()
        if short:
            return elixir._build_clan_status_short_report(clan, war)
        return elixir._build_clan_status_report(clan, war)
    if command == "site-publish":
        return await _run_site_publish(preview=preview)
    if command == "home-message":
        return await _run_home_message(preview=preview)
    if command == "members-message":
        return await _run_members_message(preview=preview)
    if command == "roster-bios":
        return await _run_roster_bios(preview=preview)
    if command == "promote-content":
        return await _run_promote_content(preview=preview)
    return await _run_runtime_job(command, preview=preview)


__all__ = [
    "COMMAND_HELP",
    "COMMAND_ORDER",
    "dispatch_admin_command",
    "parse_admin_command",
    "render_admin_help",
]
