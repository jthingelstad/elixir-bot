"""site_content.py — JSON content management for poapkings.com website.

Responsible for writing, validating, and committing
Elixir-owned JSON data files to the poapkings.com repository.
"""

import json
import logging
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone

import jsonschema

import time

import cr_api
import db

log = logging.getLogger("site_content")

POAPKINGS_REPO = os.path.expanduser(
    os.getenv("POAPKINGS_REPO_PATH", os.path.join(os.path.dirname(__file__), "..", "poapkings.com"))
)
DATA_DIR = os.path.join(POAPKINGS_REPO, "src", "_data")
SCHEMA_DIR = os.path.join(DATA_DIR, "schemas")

CONTENT_FILES = {
    "clan": "elixirClan.json",
    "home": "elixirHome.json",
    "members": "elixirMembers.json",
    "roster": "elixirRoster.json",
    "promote": "elixirPromote.json",
}

ROLE_MAP = {
    "leader": "Leader",
    "coLeader": "Co-Leader",
    "elder": "Elder",
    "member": "Member",
}
CARD_STATS_MEMBER_LIST_LIMIT = 5


def validate_against_schema(content_type, data):
    """Validate data against its JSON schema. Returns True if valid, raises on error."""
    # Schema filename matches the data filename (camelCase) minus .json + .schema.json
    content_filename = CONTENT_FILES.get(content_type, "")
    schema_name = content_filename.replace(".json", ".schema.json") if content_filename else ""
    schema_path = os.path.join(SCHEMA_DIR, schema_name)
    if not os.path.exists(schema_path):
        log.warning("Schema file not found: %s", schema_path)
        return True
    with open(schema_path, "r") as f:
        schema = json.load(f)
    jsonschema.validate(instance=data, schema=schema)
    return True


def write_content(content_type, data):
    """Validate against schema and write JSON to _data/. Returns True on success."""
    if content_type not in CONTENT_FILES:
        raise ValueError(f"Unknown content type: {content_type}")
    try:
        validate_against_schema(content_type, data)
    except jsonschema.ValidationError as e:
        log.error("Schema validation failed for %s: %s", content_type, e.message)
        return False

    path = os.path.join(DATA_DIR, CONTENT_FILES[content_type])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    log.info("Wrote %s", CONTENT_FILES[content_type])
    return True


def load_current(content_type):
    """Load existing JSON for a content type. Returns dict or None."""
    if content_type not in CONTENT_FILES:
        return None
    path = os.path.join(DATA_DIR, CONTENT_FILES[content_type])
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def commit_and_push(message="Elixir content update"):
    """Git add changed elixir-* files, commit, and push. Returns True on success."""
    try:
        # Stage all elixir-* data files
        for filename in CONTENT_FILES.values():
            filepath = os.path.join("src", "_data", filename)
            full_path = os.path.join(POAPKINGS_REPO, filepath)
            if os.path.exists(full_path):
                subprocess.run(
                    ["git", "add", filepath],
                    cwd=POAPKINGS_REPO, check=True, capture_output=True,
                )

        # Check if there's anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=POAPKINGS_REPO, capture_output=True,
        )
        if result.returncode == 0:
            log.info("No changes to commit")
            return True

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=POAPKINGS_REPO, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=POAPKINGS_REPO, check=True, capture_output=True,
        )
        log.info("Committed and pushed: %s", message)
        return True
    except subprocess.CalledProcessError as e:
        log.error("git error: %s", e)
        return False


# ── Data building ────────────────────────────────────────────────────────────

def build_clan_data(clan_data):
    """Extract dynamic clan stats from CR API clan data for elixir-clan.json."""
    member_list = clan_data.get("memberList", [])
    count = len(member_list)
    total_trophies = sum(m.get("trophies", 0) for m in member_list)
    avg_level = round(sum(m.get("expLevel", 0) for m in member_list) / count, 1) if count else 0

    return {
        "memberCount": clan_data.get("members", count),
        "clanScore": clan_data.get("clanScore", 0),
        "clanWarTrophies": clan_data.get("clanWarTrophies", 0),
        "donationsPerWeek": clan_data.get("donationsPerWeek", 0),
        "totalTrophies": total_trophies,
        "avgLevel": avg_level,
        "minTrophies": clan_data.get("requiredTrophies", 0),
        "clanLeague": _league_name(clan_data.get("warLeague", {})),
        "clanStatus": _type_name(clan_data.get("type", "open")),
    }


def _league_name(war_league):
    """Extract league name from warLeague object or return string as-is."""
    if not war_league:
        return "Unranked"
    if isinstance(war_league, dict):
        return war_league.get("name", "Unranked")
    return str(war_league)


def _type_name(clan_type):
    """Convert API clan type to display name."""
    return {
        "open": "Open",
        "inviteOnly": "Invite Only",
        "closed": "Closed",
    }.get(clan_type, clan_type.title() if clan_type else "Open")


def aggregate_card_usage(battle_log, player_tag):
    """Aggregate card usage from a player's battle log.

    Returns top 8 cards sorted by frequency:
    [{"name": "Hog Rider", "icon_url": "https://...", "usage_pct": 64}, ...]
    """
    if not battle_log:
        return []

    # Only count standard 8-card competitive battles — skip duels (24 cards),
    # boat battles (12 cards), and friendlies which don't reflect real preferences.
    SKIP_TYPES = {"friendly", "boatBattle", "riverRaceDuel"}

    clean_tag = "#" + player_tag.lstrip("#")
    card_counts = {}
    card_icons = {}
    card_members = {}
    total_battles = 0

    for battle in battle_log:
        if battle.get("type", "") in SKIP_TYPES:
            continue
        team = battle.get("team", [])
        for player in team:
            if player.get("tag") == clean_tag:
                cards = player.get("cards", [])
                if len(cards) != 8:
                    break  # non-standard deck size, skip
                total_battles += 1
                for card in cards:
                    name = card.get("name", "")
                    if not name:
                        continue
                    card_counts[name] = card_counts.get(name, 0) + 1
                    icon = card.get("iconUrls", {}).get("medium", "")
                    if icon:
                        card_icons[name] = icon
                break

    if total_battles == 0:
        return []

    sorted_cards = sorted(card_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    return [
        {
            "name": name,
            "icon_url": card_icons.get(name, ""),
            "usage_pct": round(count / total_battles * 100),
        }
        for name, count in sorted_cards
    ]


def extract_current_deck(player_data):
    """Extract current deck card names from a player profile.

    Returns list of card name strings.
    """
    if not player_data:
        return []
    return [card.get("name", "") for card in player_data.get("currentDeck", []) if card.get("name")]


def extract_current_deck_icons(player_data):
    """Extract current deck icon URLs keyed by card name."""
    if not player_data:
        return {}
    icons = {}
    for card in player_data.get("currentDeck", []):
        name = card.get("name", "")
        if not name:
            continue
        icon = card.get("iconUrls", {}).get("medium", "")
        if icon:
            icons[name] = icon
    return icons


def build_card_stats(members):
    """Aggregate clan-wide current-deck stats from enriched roster members.

    Returns cards sorted by how many current decks contain them.
    `avg_pct` is retained for schema/template compatibility and now represents
    the percent of member current decks containing the card.
    """
    card_member_count = Counter()
    card_icons = {}
    card_members = {}
    deck_count = 0

    for m in members:
        current_deck = m.get("current_deck", []) or []
        if not current_deck:
            continue
        deck_count += 1
        deck_icons = m.get("_current_deck_icons", {}) or {}
        seen = set()
        favorite_card_icons = {
            c.get("name", ""): c.get("icon_url", "")
            for c in m.get("favorite_cards", [])
            if c.get("name")
        }
        for card in current_deck:
            name = card.get("name", "") if isinstance(card, dict) else str(card)
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            card_member_count[name] += 1
            card_members.setdefault(name, []).append(
                {
                    "name": m.get("name", "Unknown"),
                    "clan_rank": m.get("clan_rank"),
                }
            )
            icon = deck_icons.get(name) or favorite_card_icons.get(name, "")
            if icon:
                card_icons[name] = icon

    if not card_member_count or deck_count == 0:
        return []

    # Sort by member_count desc, then card name asc for stable output.
    cards = sorted(
        card_member_count.keys(),
        key=lambda n: (-card_member_count[n], n.lower()),
    )
    def _sort_card_members(items):
        return sorted(
            items,
            key=lambda item: (
                item.get("clan_rank") if item.get("clan_rank") is not None else 999,
                (item.get("name") or "").lower(),
            ),
        )

    return [
        {
            "name": name,
            "icon_url": card_icons.get(name, ""),
            "member_count": card_member_count[name],
            "avg_pct": round(card_member_count[name] / deck_count * 100),
            "members": [
                item["name"]
                for item in _sort_card_members(card_members.get(name, []))[:CARD_STATS_MEMBER_LIST_LIMIT]
            ],
        }
        for name in cards
    ]


def build_roster_data(clan_data, include_cards=False, conn=None):
    """Build roster data from CR API + V2 member metadata.

    include_cards: if True, fetch battle logs and player profiles to add
        favorite_cards and current_deck per member (~15s extra for API calls).

    Returns dict for elixir-roster.json (without bios — those get added
    during the evening content cycle).
    """
    close = conn is None
    conn = conn or db.get_connection()
    try:
        member_list = clan_data.get("memberList", [])
        metadata = db.get_member_metadata_map(conn=conn)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        members = []
        for m in member_list:
            tag = m.get("tag", "").lstrip("#")
            role = ROLE_MAP.get(m.get("role", "member"), "Member")

            arena = m.get("arena", {})
            arena_name = arena.get("name", "") if isinstance(arena, dict) else ""

            extra = metadata.get(tag, {})

            member = {
                "name": m.get("name", "Unknown"),
                "tag": tag,
                "role": role,
                "exp_level": m.get("expLevel", 0),
                "trophies": m.get("trophies", 0),
                "arena": arena_name,
                "clan_rank": m.get("clanRank", 0),
                "donations": m.get("donations", 0),
                "donations_received": m.get("donationsReceived", 0),
                "last_seen": m.get("lastSeen", ""),
                "note": extra.get("note", ""),
                "profile_url": extra.get("profile_url", ""),
                "poap_address": extra.get("poap_address", ""),
                "date_joined": extra.get("joined_date"),
            }
            members.append(member)

        # Fetch card data if requested
        if include_cards:
            for member in members:
                tag = member["tag"]
                try:
                    battle_log = cr_api.get_player_battle_log(tag)
                    if battle_log:
                        db.snapshot_player_battlelog(tag, battle_log, conn=conn)
                    member["favorite_cards"] = aggregate_card_usage(battle_log, tag)
                except Exception:
                    cached = db.get_member_signature_cards("#" + tag, conn=conn)
                    member["favorite_cards"] = (cached or {}).get("cards", [])

                try:
                    player_data = cr_api.get_player(tag)
                    member["current_deck"] = extract_current_deck(player_data)
                    member["_current_deck_icons"] = extract_current_deck_icons(player_data)
                    if player_data:
                        snapshot_payload = dict(player_data)
                        snapshot_payload.setdefault("tag", "#" + tag)
                        snapshot_payload.setdefault("name", member["name"])
                        db.snapshot_player_profile(snapshot_payload, conn=conn)
                except Exception:
                    cached = db.get_member_current_deck("#" + tag, conn=conn)
                    cached_cards = (cached or {}).get("cards") or []
                    member["current_deck"] = [c.get("name", "") for c in cached_cards if c.get("name")]
                    member["_current_deck_icons"] = {
                        c.get("name", ""): c.get("iconUrls", {}).get("medium", "")
                        for c in cached_cards
                        if c.get("name")
                    }

                time.sleep(0.3)

        # Preserve existing bio/highlight fields from the current file
        existing = load_current("roster")
        if existing:
            existing_by_tag = {m["tag"]: m for m in existing.get("members", [])}
            for member in members:
                prev = existing_by_tag.get(member["tag"], {})
                for field in ("bio", "highlight"):
                    if prev.get(field) and not member.get(field):
                        member[field] = prev[field]

        # Sort known join dates first, oldest to newest; unknown tenure sorts last.
        members.sort(key=lambda m: (m["date_joined"] is None, m["date_joined"] or "", m["name"].lower()))

        result = {"updated": now, "members": members}
        if existing and existing.get("intro"):
            result["intro"] = existing["intro"]
        if include_cards:
            result["card_stats"] = build_card_stats(members)
        for member in members:
            member.pop("_current_deck_icons", None)
        return result
    finally:
        if close:
            conn.close()
