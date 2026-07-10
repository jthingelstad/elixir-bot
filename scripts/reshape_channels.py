#!/usr/bin/env python3
"""One-off Discord channel reshape for the brain-posting transition (2026-07-10).

Uses Elixir's DISCORD_TOKEN (Manage-Channels) against the REST API. Two modes,
both idempotent and logged; nothing is deleted.

    python scripts/reshape_channels.py create-elixir [--dry-run]
    python scripts/reshape_channels.py archive       [--dry-run]

- create-elixir: create #elixir under the same category as #announcements and
  print its channel ID (paste into prompts/DISCORD.md). Skips if it exists.
- archive: move the 5 deprecated channels under an "Archived" category and lock
  them read-only for @everyone (deny SEND_MESSAGES). Records each channel's
  original parent_id to scratch/reshape_channels_rollback.json for exact undo.

Run `create-elixir` first (transition step 0). Run `archive` at cutover, AFTER
ELIXIR_AWARENESS_LIVE=1 (the engine has stopped posting to these channels).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://discord.com/api/v10"
GUILD_ID = "1474760692992180429"
EVERYONE_ROLE_ID = GUILD_ID  # @everyone role id == guild id
ANNOUNCEMENTS_ID = "1474760975851982959"
SEND_MESSAGES = 1 << 11  # 0x800

# Deprecated ELIXIR-OWNED auto-posting channels → archive (name is only for the
# log). NOTE: #clan-chat is deliberately NOT here — it's the members' own main
# chat channel, not Elixir-owned. "Folding it into #ask-elixir" means Elixir
# stops answering there (the `general` lane), NOT archiving the channel.
DEPRECATED = {
    "1482352067573059675": "river-race",
    "1523195660856459387": "battle-feed",
    "1482352147029950474": "player-highlights",
    "1482352241628414013": "clan-events",
}

ROLLBACK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scratch", "reshape_channels_rollback.json",
)


def _headers() -> dict:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not configured")
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


def _req(method: str, path: str, *, json_body=None):
    """One REST call with basic 429 handling. Returns parsed JSON (or {})."""
    url = f"{API}{path}"
    for _ in range(5):
        resp = requests.request(method, url, headers=_headers(), json=json_body, timeout=30)
        if resp.status_code == 429:
            retry = float(resp.json().get("retry_after", 1.0))
            time.sleep(retry + 0.25)
            continue
        if resp.status_code >= 400:
            sys.exit(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.text else {}
    sys.exit(f"{method} {path}: rate-limited repeatedly")


def _guild_channels() -> list[dict]:
    return _req("GET", f"/guilds/{GUILD_ID}/channels")


def _find_category(channels: list[dict], name: str) -> dict | None:
    for c in channels:
        if c.get("type") == 4 and (c.get("name") or "").lower() == name.lower():
            return c
    return None


def create_elixir(dry_run: bool) -> None:
    channels = _guild_channels()
    existing = next((c for c in channels if (c.get("name") or "") == "elixir"
                     and c.get("type") == 0), None)
    if existing:
        print(f"#elixir already exists: {existing['id']}")
        return
    parent_id = next((c.get("parent_id") for c in channels
                      if c.get("id") == ANNOUNCEMENTS_ID), None)
    body = {
        "name": "elixir",
        "type": 0,
        "topic": "Elixir's commentary & updates — the brain's voice.",
        "parent_id": parent_id,
    }
    if dry_run:
        print(f"[dry-run] would POST /guilds/{GUILD_ID}/channels {body}")
        return
    created = _req("POST", f"/guilds/{GUILD_ID}/channels", json_body=body)
    print(f"created #elixir: {created['id']}  (parent {parent_id})")
    print("→ add this ID to prompts/DISCORD.md under `## #elixir` (Lane: elixir)")


def archive(dry_run: bool) -> None:
    channels = _guild_channels()
    category = _find_category(channels, "Archived")
    by_id = {c["id"]: c for c in channels}

    if category is None:
        if dry_run:
            print("[dry-run] would create category 'Archived'")
            category_id = "<new>"
        else:
            category = _req("POST", f"/guilds/{GUILD_ID}/channels",
                            json_body={"name": "Archived", "type": 4})
            category_id = category["id"]
            print(f"created category 'Archived': {category_id}")
    else:
        category_id = category["id"]
        print(f"using existing 'Archived' category: {category_id}")

    rollback = {}
    for cid, name in DEPRECATED.items():
        chan = by_id.get(cid)
        if chan is None:
            print(f"  skip {name} ({cid}): not found")
            continue
        rollback[cid] = {"name": name, "parent_id": chan.get("parent_id")}
        overwrites = [o for o in (chan.get("permission_overwrites") or [])
                      if o.get("id") != EVERYONE_ROLE_ID]
        overwrites.append({"id": EVERYONE_ROLE_ID, "type": 0, "allow": "0",
                           "deny": str(SEND_MESSAGES)})
        body = {"parent_id": category_id, "permission_overwrites": overwrites}
        if dry_run:
            print(f"  [dry-run] would PATCH {name} ({cid}) → Archived + lock @everyone")
            continue
        _req("PATCH", f"/channels/{cid}", json_body=body)
        print(f"  archived #{name} ({cid})")

    if not dry_run:
        os.makedirs(os.path.dirname(ROLLBACK_PATH), exist_ok=True)
        with open(ROLLBACK_PATH, "w", encoding="utf-8") as f:
            json.dump(rollback, f, indent=2)
        print(f"rollback map written: {ROLLBACK_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["create-elixir", "archive"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.mode == "create-elixir":
        create_elixir(args.dry_run)
    else:
        archive(args.dry_run)


if __name__ == "__main__":
    main()
