#!/usr/bin/env python3
"""Dev test — exercise the site content pipeline without Discord.

Usage:
    source venv/bin/activate
    python scripts/dev/site_content.py [step]

Steps:
    data      — fetch CR API, build roster + clan data, write JSON (no commit)
    home      — generate home message via LLM
    members   — generate members message via LLM
    roster    — generate roster bios via LLM
    promote   — generate promote content via LLM
    all       — run everything (default)

Set DRY_RUN=1 to skip git commit/push.
"""

import json
import os
import sys
import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("dev_test")

import cr_api
import db
import site_content
import elixir_agent


DRY_RUN = os.getenv("DRY_RUN", "1") == "1"


def step_data():
    """Fetch CR API and build data files."""
    log.info("Fetching clan data from CR API...")
    try:
        clan = cr_api.get_clan()
    except Exception as e:
        log.error("CR API failed: %s", e)
        return None

    member_count = len(clan.get("memberList", []))
    log.info("Got %d members from API", member_count)

    # Build and write clan data
    clan_stats = site_content.build_clan_data(clan)
    log.info("Clan stats: %s", json.dumps(clan_stats, indent=2))
    site_content.write_content("clan", clan_stats)

    # Build and write roster data (with card data)
    roster_data = site_content.build_roster_data(clan, include_cards=True)
    log.info("Roster: %d members, updated=%s", len(roster_data["members"]), roster_data["updated"])
    for m in roster_data["members"][:3]:
        cards = m.get("favorite_cards", [])
        if cards:
            log.info("  %s favorite cards: %s", m["name"], ", ".join(c["name"] for c in cards[:3]))
    site_content.write_content("roster", roster_data)

    log.info("Wrote elixir-clan.json and elixir-roster.json")
    return clan


def step_home(clan=None):
    """Generate home page message."""
    if not clan:
        clan = _get_clan()
    war = _get_war()
    roster = site_content.load_current("roster")

    prev = site_content.load_current("home")
    prev_msg = prev.get("message", "") if prev else ""
    log.info("Previous home message: %s", prev_msg[:80] if prev_msg else "(none)")

    log.info("Generating home message...")
    text = elixir_agent.generate_home_message(clan, war, prev_msg, roster_data=roster)
    if text:
        log.info("Home message: %s", text)
        from datetime import datetime, timezone
        site_content.write_content("home", {
            "message": text,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    else:
        log.warning("LLM returned nothing for home message")


def step_members(clan=None):
    """Generate members page message."""
    if not clan:
        clan = _get_clan()
    war = _get_war()
    roster = site_content.load_current("roster")

    prev = site_content.load_current("members")
    prev_msg = prev.get("message", "") if prev else ""

    log.info("Generating members message...")
    text = elixir_agent.generate_members_message(clan, war, prev_msg, roster_data=roster)
    if text:
        log.info("Members message: %s", text)
        from datetime import datetime, timezone
        site_content.write_content("members", {
            "message": text,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    else:
        log.warning("LLM returned nothing for members message")


def step_roster(clan=None):
    """Generate roster bios."""
    if not clan:
        clan = _get_clan()
    war = _get_war()

    roster = site_content.load_current("roster")

    log.info("Generating roster bios (this uses tools, may take a moment)...")
    bios_result = elixir_agent.generate_roster_bios(clan, war, roster_data=roster)
    if bios_result:
        # Handle plaintext fallback wrapping — content may be a JSON string
        if "intro" not in bios_result and "content" in bios_result:
            import json as _json
            try:
                bios_result = _json.loads(bios_result["content"])
            except (ValueError, TypeError):
                pass
        log.info("Roster intro: %s", bios_result.get("intro", "")[:100])
        member_bios = bios_result.get("members", {})
        log.info("Got bios for %d members", len(member_bios))
        for tag, mc in list(member_bios.items())[:3]:
            log.info("  %s: %s [%s]", tag, mc.get("bio", "")[:60], mc.get("highlight", ""))

        # Merge into existing roster data
        roster_data = site_content.load_current("roster")
        if roster_data:
            roster_data["intro"] = bios_result.get("intro", "")
            for m in roster_data["members"]:
                mc = member_bios.get(m["tag"], {}) or member_bios.get("#" + m["tag"], {})
                if mc:
                    m["bio"] = mc.get("bio", "")
                    m["highlight"] = mc.get("highlight", "general")
            site_content.write_content("roster", roster_data)
    else:
        log.warning("LLM returned nothing for roster bios")


def step_promote(clan=None):
    """Generate promote content."""
    if not clan:
        clan = _get_clan()

    roster = site_content.load_current("roster")

    log.info("Generating promote content...")
    promote = elixir_agent.generate_promote_content(clan, roster_data=roster)
    if promote:
        for channel in ["message", "social", "email", "discord", "reddit"]:
            body = promote.get(channel, {}).get("body", promote.get(channel, {}).get("title", ""))
            log.info("  %s: %s", channel, body[:80] if body else "(empty)")
        site_content.write_content("promote", promote)
    else:
        log.warning("LLM returned nothing for promote content")


def _get_clan():
    try:
        return cr_api.get_clan()
    except Exception:
        log.warning("CR API failed, using empty clan data")
        return {}


def _get_war():
    try:
        return cr_api.get_current_war()
    except Exception:
        return {}


def main():
    step = sys.argv[1] if len(sys.argv) > 1 else "all"

    if DRY_RUN:
        log.info("DRY_RUN=1 — will write files but skip git commit/push")

    steps = {
        "data": lambda: step_data(),
        "home": lambda: step_home(),
        "members": lambda: step_members(),
        "roster": lambda: step_roster(),
        "promote": lambda: step_promote(),
    }

    if step == "all":
        clan = step_data()
        if clan:
            step_home(clan)
            step_members(clan)
            step_roster(clan)
            step_promote(clan)
        if not DRY_RUN:
            site_content.commit_and_push("Elixir dev test")
        log.info("Done! Check ../poapkings.com/src/_data/elixir-*.json")
    elif step in steps:
        steps[step]()
        log.info("Done!")
    else:
        print(f"Unknown step: {step}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
