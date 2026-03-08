#!/usr/bin/env python3
"""Administrative control surface for Elixir jobs and reports.

Usage examples:
    venv/bin/python scripts/elixir_do.py help
    venv/bin/python scripts/elixir_do.py status
    venv/bin/python scripts/elixir_do.py schedule
    venv/bin/python scripts/elixir_do.py clan-status
    venv/bin/python scripts/elixir_do.py heartbeat --preview
    venv/bin/python scripts/elixir_do.py site-data --preview
    venv/bin/python scripts/elixir_do.py site-content
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

import discord
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv()

import prompts
import elixir
import site_content
from runtime import jobs as runtime_jobs
from runtime.app import TOKEN


COMMAND_HELP = {
    "help": "Show this operator help page.",
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elixir_do.py",
        description="Run Elixir admin jobs and reports on demand.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("help", help=COMMAND_HELP["help"])
    subparsers.add_parser("status", help=COMMAND_HELP["status"])
    subparsers.add_parser("schedule", help=COMMAND_HELP["schedule"])

    clan_status = subparsers.add_parser("clan-status", help=COMMAND_HELP["clan-status"])
    clan_status.add_argument("--short", action="store_true", help="Print the short clan status report.")

    for name in (
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
    ):
        sub = subparsers.add_parser(name, help=COMMAND_HELP[name])
        sub.add_argument(
            "--preview",
            action="store_true",
            help="Do not post to Discord or push site commits. Preview Discord posts to stdout instead.",
        )

    return parser


def _render_help(parser: argparse.ArgumentParser) -> str:
    lines = [
        "**Elixir Do**",
        "Run Elixir jobs and reports on demand.",
        "",
        "Commands:",
    ]
    for name, description in COMMAND_HELP.items():
        lines.append(f"- `{name}`: {description}")
    lines.extend([
        "",
        "Preview mode:",
        "- Add `--preview` to job commands to suppress Discord sends and site git pushes.",
        "- Preview mode still runs the job logic and will print would-be Discord posts.",
        "",
        "Examples:",
        "- `venv/bin/python scripts/elixir_do.py promotion --preview`",
        "- `venv/bin/python scripts/elixir_do.py heartbeat --preview`",
        "- `venv/bin/python scripts/elixir_do.py site-content`",
        "",
        "Built-in argparse help:",
        parser.format_help().rstrip(),
    ])
    return "\n".join(lines)


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
                patch("runtime.jobs.site_content.commit_and_push", side_effect=lambda *args, **kwargs: None)
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
        "site-data": elixir._site_data_refresh,
        "site-content": elixir._site_content_cycle,
        "promotion": elixir._promotion_content_cycle,
        "player-intel": elixir._player_intel_refresh,
        "clanops-review": elixir._clanops_weekly_review,
    }
    async with _job_runtime(preview=preview):
        await job_map[job_name]()


async def _load_site_context():
    clan, war = await elixir._load_live_clan_context()
    roster = await asyncio.to_thread(site_content.load_current, "roster")
    if roster is None and clan.get("memberList"):
        roster = await asyncio.to_thread(site_content.build_roster_data, clan, True)
        await asyncio.to_thread(site_content.write_content, "roster", roster)
    return clan, war, roster


async def _run_site_publish(preview: bool):
    if preview:
        print("Preview mode: site publish skipped.")
        return
    ok = await asyncio.to_thread(site_content.commit_and_push, "Elixir manual site publish")
    if not ok:
        raise RuntimeError("site publish failed")
    print("Site content published.")


async def _run_home_message(preview: bool):
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
    print(text)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir home message update")


async def _run_members_message(preview: bool):
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
    print(text)
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir members message update")


async def _run_roster_bios(preview: bool):
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
    await asyncio.to_thread(site_content.write_content, "roster", roster_payload)
    print(roster_payload.get("intro", ""))
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir roster bio update")


async def _run_promote_content(preview: bool):
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
    print(json.dumps(promote, indent=2))
    if not preview:
        await asyncio.to_thread(site_content.commit_and_push, "Elixir promotion content update")


async def _print_clan_status(short: bool):
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
    if command == "status":
        print(elixir._build_status_report())
        return 0
    if command == "schedule":
        print(elixir._build_schedule_report())
        return 0
    if command == "clan-status":
        await _print_clan_status(short=args.short)
        return 0
    if command == "site-publish":
        await _run_site_publish(preview=args.preview)
        return 0
    if command == "home-message":
        await _run_home_message(preview=args.preview)
        return 0
    if command == "members-message":
        await _run_members_message(preview=args.preview)
        return 0
    if command == "roster-bios":
        await _run_roster_bios(preview=args.preview)
        return 0
    if command == "promote-content":
        await _run_promote_content(preview=args.preview)
        return 0
    await _run_job(command, preview=getattr(args, "preview", False))
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
