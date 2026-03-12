#!/usr/bin/env python3
"""Administrative control surface for Elixir jobs and reports.

Usage examples:
    venv/bin/python scripts/elixir_do.py help
    venv/bin/python scripts/elixir_do.py status
    venv/bin/python scripts/elixir_do.py schedule
    venv/bin/python scripts/elixir_do.py clan-status
    venv/bin/python scripts/elixir_do.py clan-list
    venv/bin/python scripts/elixir_do.py profile "Ditika"
    venv/bin/python scripts/elixir_do.py set-join-date "Ditika" 2026-03-07
    venv/bin/python scripts/elixir_do.py set-note "King Thing" "Founder and systems builder"
    venv/bin/python scripts/elixir_do.py heartbeat --preview
    venv/bin/python scripts/elixir_do.py system-signals --preview
    venv/bin/python scripts/elixir_do.py poap-kings-data-sync --preview
    venv/bin/python scripts/elixir_do.py poap-kings-sync
    venv/bin/python scripts/elixir_do.py promotion --preview
    venv/bin/python scripts/elixir_do.py player-intel
    venv/bin/python scripts/elixir_do.py clanops-review --preview
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv()

from runtime import admin as admin_commands

COMMAND_HELP = admin_commands.COMMAND_HELP


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elixir_do.py",
        description="Run Elixir admin jobs and reports on demand. Discord slash commands are grouped under /elixir; this CLI stays flat.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("help", help=COMMAND_HELP["help"])
    subparsers.add_parser("status", help=COMMAND_HELP["status"])
    subparsers.add_parser("schedule", help=COMMAND_HELP["schedule"])
    subparsers.add_parser("clan-list", help=COMMAND_HELP["clan-list"])

    clan_status = subparsers.add_parser("clan-status", help=COMMAND_HELP["clan-status"])
    clan_status.add_argument("--short", action="store_true", help="Print the short clan status report.")

    profile = subparsers.add_parser("profile", help=COMMAND_HELP["profile"])
    profile.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")

    memory = subparsers.add_parser("memory", help=COMMAND_HELP["memory"])
    memory.add_argument("--member", help="Inspect memory for one member.")
    memory.add_argument("--query", help="Search contextual memory text.")
    memory.add_argument("--limit", type=int, default=5, help="Maximum items per section (1-10).")
    memory.add_argument(
        "--system-internal",
        action="store_true",
        help="Include system-internal contextual memories.",
    )

    set_join_date = subparsers.add_parser("set-join-date", help=COMMAND_HELP["set-join-date"])
    set_join_date.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    set_join_date.add_argument("date", help="Join date in YYYY-MM-DD format.")
    set_join_date.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    clear_join_date = subparsers.add_parser("clear-join-date", help=COMMAND_HELP["clear-join-date"])
    clear_join_date.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    clear_join_date.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    set_birthday = subparsers.add_parser("set-birthday", help=COMMAND_HELP["set-birthday"])
    set_birthday.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    set_birthday.add_argument("month", type=int, help="Birth month (1-12).")
    set_birthday.add_argument("day", type=int, help="Birth day (1-31).")
    set_birthday.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    clear_birthday = subparsers.add_parser("clear-birthday", help=COMMAND_HELP["clear-birthday"])
    clear_birthday.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    clear_birthday.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    set_profile_url = subparsers.add_parser("set-profile-url", help=COMMAND_HELP["set-profile-url"])
    set_profile_url.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    set_profile_url.add_argument("url", help="Profile URL.")
    set_profile_url.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    clear_profile_url = subparsers.add_parser("clear-profile-url", help=COMMAND_HELP["clear-profile-url"])
    clear_profile_url.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    clear_profile_url.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    set_poap_address = subparsers.add_parser("set-poap-address", help=COMMAND_HELP["set-poap-address"])
    set_poap_address.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    set_poap_address.add_argument("poap_address", help="POAP address.")
    set_poap_address.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    clear_poap_address = subparsers.add_parser("clear-poap-address", help=COMMAND_HELP["clear-poap-address"])
    clear_poap_address.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    clear_poap_address.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    set_note = subparsers.add_parser("set-note", help=COMMAND_HELP["set-note"])
    set_note.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    set_note.add_argument("note", help="Free-form member note.")
    set_note.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    clear_note = subparsers.add_parser("clear-note", help=COMMAND_HELP["clear-note"])
    clear_note.add_argument("member", help="Player tag, in-game name, alias, or Discord handle.")
    clear_note.add_argument("--preview", action="store_true", help="Show the change without writing it.")

    for name in (
        "heartbeat",
        "system-signals",
        "poap-kings-sync",
        "poap-kings-data-sync",
        "poap-kings-site-sync",
        "poap-kings-home-sync",
        "poap-kings-members-sync",
        "poap-kings-roster-bios-sync",
        "poap-kings-promotion-sync",
        "promotion",
        "player-intel",
        "clanops-review",
        "weekly-recap",
    ):
        sub = subparsers.add_parser(name, help=COMMAND_HELP[name])
        sub.add_argument(
            "--preview",
            action="store_true",
            help="Do not post to Discord or push site commits. Preview Discord posts to stdout instead.",
        )

    for legacy_name in (
        "site-data",
        "site-content",
        "site-publish",
        "home-message",
        "members-message",
        "roster-bios",
        "promote-content",
    ):
        sub = subparsers.add_parser(legacy_name, help=argparse.SUPPRESS)
        sub.add_argument(
            "--preview",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    return parser


def _render_help(parser: argparse.ArgumentParser) -> str:
    return (
        admin_commands.render_admin_help(
            mention_prefix="@Elixir do",
            slash_prefix="/elixir",
            cli_prefix="venv/bin/python scripts/elixir_do.py",
        )
        + "\n\nBuilt-in argparse help:\n"
        + parser.format_help().rstrip()
    )


class _PreviewChannel:
    def __init__(self, channel_id: int, name: str):
        self.id = channel_id
        self.name = name.lstrip("#")
        self.type = "text"

    async def send(self, content: str):
        print(f"\n--- preview #{self.name} ({self.id}) ---")
        print(content)


class _ChannelLookup:
    def __init__(self, channels_by_id: dict[int, object]):
        self._channels_by_id = channels_by_id

    def get_channel(self, channel_id: int):
        return self._channels_by_id.get(channel_id)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@asynccontextmanager
async def _job_runtime(preview: bool):
    import discord
    import prompts
    from runtime import jobs as runtime_jobs
    from runtime.app import TOKEN

    channel_ids = [channel["id"] for channel in prompts.discord_channel_configs()]
    stack = ExitStack()
    client = None
    try:
        if preview:
            channels = {
                channel["id"]: _PreviewChannel(channel["id"], channel["name"])
                for channel in prompts.discord_channel_configs()
            }
            stack.enter_context(
                patch("runtime.jobs.poap_kings_site.commit_and_push", side_effect=lambda *args, **kwargs: None)
            )
        else:
            if not TOKEN:
                raise RuntimeError("DISCORD_TOKEN is not configured")
            client = discord.Client(intents=discord.Intents.none())
            await client.login(TOKEN)
            channels = {}
            for channel_id in channel_ids:
                channels[channel_id] = await client.fetch_channel(channel_id)

        bot_lookup = _ChannelLookup(channels)
        stack.enter_context(patch.object(runtime_jobs, "bot", bot_lookup))
        yield
    finally:
        stack.close()
        if client is not None:
            await client.close()


async def _run_job(job_name: str, preview: bool):
    job_map = {
        "heartbeat": elixir._heartbeat_tick,
        "poap-kings-data-sync": elixir._site_data_refresh,
        "poap-kings-site-sync": elixir._site_content_cycle,
        "promotion": elixir._promotion_content_cycle,
        "player-intel": elixir._player_intel_refresh,
        "clanops-review": elixir._clanops_weekly_review,
        "weekly-recap": elixir._weekly_clan_recap,
    }
    async with _job_runtime(preview=preview):
        await job_map[job_name]()


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


async def _run_poap_kings_sync(preview: bool):
    import elixir

    if preview:
        print("Preview mode: `poap-kings-sync` suppresses remote site publishing.")
        return
    await elixir._site_content_cycle()
    members_text = await admin_commands.dispatch_admin_command("poap-kings-members-sync", preview=False, short=False, args={})
    if members_text:
        print(members_text)
    print("Ran `poap-kings-sync` to publish the current POAP KINGS site bundle.")


async def _run_home_message(preview: bool):
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
    print(text)
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"home": payload},
            "Elixir POAP KINGS home message update",
        )


async def _run_members_message(preview: bool):
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
    print(text)
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"members": payload},
            "Elixir POAP KINGS members page weekly recap update",
        )


async def _run_roster_bios(preview: bool):
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
    for member in roster_payload.get("members", []):
        item = bios_by_tag.get(member["tag"], {}) or bios_by_tag.get("#" + member["tag"], {})
        if item:
            member["bio"] = item.get("bio", "")
            member["highlight"] = item.get("highlight", "general")
    print(roster_payload.get("intro", ""))
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"roster": roster_payload},
            "Elixir POAP KINGS roster bio update",
        )


async def _run_promote_content(preview: bool):
    import elixir
    from integrations.poap_kings import site as poap_kings_site

    clan, war, roster = await _load_site_context()
    promote = await asyncio.to_thread(
        elixir.elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=roster,
    )
    if not promote:
        raise RuntimeError("promotion content generation returned nothing")
    print(json.dumps(promote, indent=2))
    if not preview:
        await asyncio.to_thread(
            poap_kings_site.publish_site_content,
            {"promote": promote},
            "Elixir POAP KINGS promotion content update",
        )


async def _print_clan_status(short: bool):
    import elixir

    clan, war = await elixir._load_live_clan_context()
    if short:
        print(elixir._build_clan_status_short_report(clan, war))
    else:
        print(elixir._build_clan_status_report(clan, war))


async def _dispatch(args, parser: argparse.ArgumentParser) -> int:
    command = args.command or "help"
    if command == "help":
        print(_render_help(parser))
        return 0
    dispatch_args = {
        key: value
        for key, value in vars(args).items()
        if key not in {"command", "preview", "short"} and value is not None
    }
    text = await admin_commands.dispatch_admin_command(
        command,
        preview=getattr(args, "preview", False),
        short=getattr(args, "short", False),
        args=dispatch_args,
    )
    print(text)
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_dispatch(args, parser))
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
