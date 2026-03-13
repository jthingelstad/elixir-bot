"""POAP KINGS site integration.

Owns POAP KINGS website payload building and publishing.

Legacy local file helpers remain here for compatibility and tests, but the
runtime publishing path should use the explicit GitHub-backed publish helpers.
"""

import base64
import json
import logging
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import jsonschema

import time

import db
import cr_api

log = logging.getLogger("poap_kings.site")

POAPKINGS_REPO = os.path.expanduser(
    os.getenv("POAPKINGS_REPO_PATH", os.path.join(os.path.dirname(__file__), "..", "poapkings.com"))
)
DATA_DIR = os.path.join(POAPKINGS_REPO, "src", "_data")
SCHEMA_DIR = os.path.join(DATA_DIR, "schemas")
GITHUB_API_BASE = "https://api.github.com"

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
PROFILE_BADGE_HIGHLIGHT_NAMES = {
    "YearsPlayed",
    "BattleWins",
    "ClanWarsVeteran",
    "ClanWarWins",
    "LadderTop1000",
    "CollectionLevel",
    "ClanDonations",
    "EmoteCollection",
    "BannerCollection",
    "Classic12Wins",
    "Grand12Wins",
    "2v2",
}
PROFILE_BADGE_PRIORITY = {
    "YearsPlayed": 0,
    "BattleWins": 1,
    "ClanWarsVeteran": 2,
    "ClanWarWins": 3,
    "LadderTop1000": 4,
    "CollectionLevel": 5,
    "ClanDonations": 6,
    "EmoteCollection": 7,
    "BannerCollection": 8,
    "Classic12Wins": 9,
    "Grand12Wins": 10,
    "2v2": 11,
}


def _split_identifier_words(value: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")
    text = text.replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)


def _badge_category(name: str | None) -> str:
    badge_name = str(name or "").strip()
    if not badge_name:
        return "general"
    if badge_name.startswith("Mastery"):
        return "mastery"
    if badge_name in {"Classic12Wins", "Grand12Wins"}:
        return "challenge"
    if badge_name in {"2v2", "RampUp", "SuddenDeath", "Draft", "2xElixir"}:
        return "mode"
    if badge_name in {"EmoteCollection", "BannerCollection", "CollectionLevel", "ClanDonations"}:
        return "collection"
    if badge_name in {"YearsPlayed", "BattleWins", "ClanWarsVeteran", "ClanWarWins", "LadderTop1000"}:
        return "career"
    return "general"


def _badge_label(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name:
        return None
    overrides = {
        "Classic12Wins": "Classic Challenge 12 Wins",
        "Grand12Wins": "Grand Challenge 12 Wins",
        "2xElixir": "2x Elixir",
        "2v2": "2v2",
    }
    if badge_name in overrides:
        return overrides[badge_name]
    if badge_name.startswith("Mastery") and len(badge_name) > len("Mastery"):
        return f"{_split_identifier_words(badge_name[len('Mastery'):])} Mastery"
    return _split_identifier_words(badge_name)


def _mastery_card_name(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name.startswith("Mastery") or len(badge_name) <= len("Mastery"):
        return None
    return _split_identifier_words(badge_name[len("Mastery"):])


def _normalize_badge(badge: dict) -> dict:
    name = badge.get("name")
    item = {
        "name": name,
        "label": _badge_label(name),
        "category": _badge_category(name),
        "level": badge.get("level"),
        "max_level": badge.get("maxLevel"),
        "progress": badge.get("progress"),
        "target": badge.get("target"),
        "is_one_time": badge.get("level") is None,
    }
    mastery_card = _mastery_card_name(name)
    if mastery_card:
        item["card_name"] = mastery_card
    return item


def _normalize_achievement(achievement: dict) -> dict:
    stars = achievement.get("stars")
    return {
        "name": achievement.get("name"),
        "stars": stars,
        "value": achievement.get("value"),
        "target": achievement.get("target"),
        "info": achievement.get("info"),
        "completion_info": achievement.get("completionInfo"),
        "completed": isinstance(stars, int) and stars >= 3,
    }


def _profile_showcase_fields(player_data: dict | None) -> dict:
    profile = player_data or {}
    badges = profile.get("badges") or []
    achievements = profile.get("achievements") or []
    normalized_badges = [_normalize_badge(badge) for badge in badges if badge.get("name")]
    badge_highlights = [
        badge for badge in normalized_badges
        if badge["name"] in PROFILE_BADGE_HIGHLIGHT_NAMES and badge["category"] != "mastery"
    ]
    badge_highlights.sort(
        key=lambda badge: (
            PROFILE_BADGE_PRIORITY.get(badge["name"], 99),
            {"career": 0, "challenge": 1, "collection": 2, "mode": 3, "general": 4}.get(badge["category"], 9),
            -(badge.get("level") or 0),
            -(badge.get("progress") or 0),
            (badge.get("label") or badge.get("name") or "").lower(),
        )
    )
    mastery_highlights = [badge for badge in normalized_badges if badge["category"] == "mastery"]
    mastery_highlights.sort(
        key=lambda badge: (
            -(badge.get("level") or 0),
            -(badge.get("progress") or 0),
            (badge.get("card_name") or badge.get("label") or badge.get("name") or "").lower(),
        )
    )
    normalized_achievements = [_normalize_achievement(item) for item in achievements if item.get("name")]
    achievement_star_count = sum(int(item.get("stars") or 0) for item in normalized_achievements)
    achievement_completed_count = sum(1 for item in normalized_achievements if item.get("completed"))
    return {
        "badge_count": len(normalized_badges),
        "badge_highlights": badge_highlights[:8],
        "mastery_highlights": mastery_highlights[:5],
        "achievement_star_count": achievement_star_count,
        "achievement_completed_count": achievement_completed_count,
        "achievement_progress": normalized_achievements,
    }


def _latest_profile_showcase_map(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT m.player_tag, p.badges_json, p.achievements_json "
        "FROM members m "
        "JOIN player_profile_snapshots p ON p.snapshot_id = ("
        "  SELECT p2.snapshot_id FROM player_profile_snapshots p2 "
        "  WHERE p2.member_id = m.member_id "
        "  ORDER BY p2.fetched_at DESC, p2.snapshot_id DESC LIMIT 1"
        ")"
    ).fetchall()
    result = {}
    for row in rows:
        result[row["player_tag"].lstrip("#")] = _profile_showcase_fields(
            {
                "badges": json.loads(row["badges_json"] or "[]"),
                "achievements": json.loads(row["achievements_json"] or "[]"),
            }
        )
    return result


def _site_repo() -> str:
    return os.getenv("POAP_KINGS_SITE_REPO", "jthingelstad/poapkings.com")


def _site_branch() -> str:
    return os.getenv("POAP_KINGS_SITE_BRANCH", "main")


def _site_token() -> str:
    return (os.getenv("POAP_KINGS_SITE_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()


def _site_flag_enabled() -> bool:
    return os.getenv("POAP_KINGS_SITE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def site_enabled() -> bool:
    return bool(_site_flag_enabled() and _site_repo() and _site_token())


def target_path(content_type: str) -> str:
    if content_type not in CONTENT_FILES:
        raise ValueError(f"Unknown content type: {content_type}")
    return f"src/_data/{CONTENT_FILES[content_type]}"


def serialize_content(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _repo_parts(repo_slug: str | None = None) -> tuple[str, str]:
    slug = (repo_slug or _site_repo() or "").strip()
    if "/" not in slug:
        raise ValueError("POAP KINGS site repo must be in 'owner/repo' form")
    owner, repo = slug.split("/", 1)
    return owner, repo


def _github_request(method: str, path: str, *, payload=None, expected=(200,), token: str | None = None):
    owner, repo = _repo_parts()
    auth_token = token or _site_token()
    if not auth_token:
        raise RuntimeError("POAP KINGS site publishing is not configured: missing GitHub token")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}{path}"
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {auth_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "elixir-bot",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8") if resp.length != 0 else ""
            if resp.status not in expected:
                raise RuntimeError(f"GitHub API {method} {path} returned {resp.status}")
            if not body:
                return None
            return json.loads(body)
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code in expected and not body:
            return None
        raise RuntimeError(f"GitHub API {method} {path} failed with {exc.code}: {body[:240]}") from exc


def load_published(content_type: str, *, branch: str | None = None):
    if content_type not in CONTENT_FILES:
        return None
    branch_name = branch or _site_branch()
    path = target_path(content_type)
    encoded_path = urlparse.quote(path, safe="/")
    owner, repo = _repo_parts()
    auth_token = _site_token()
    if not auth_token:
        return None
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{encoded_path}?ref={urlparse.quote(branch_name)}"
    req = urlrequest.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {auth_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "elixir-bot",
        },
        method="GET",
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API GET /contents/{encoded_path} failed with {exc.code}: {body[:240]}") from exc

    content = payload.get("content") or ""
    if payload.get("encoding") == "base64":
        raw = base64.b64decode(content.encode("ascii")).decode("utf-8")
    else:
        raw = content
    return json.loads(raw) if raw.strip() else None


def _load_existing_content(content_type: str):
    if site_enabled():
        try:
            published = load_published(content_type)
            if published is not None:
                return published
        except Exception as exc:
            log.warning("POAP KINGS site load fallback for %s: %s", content_type, exc)
    return load_current(content_type)


def publish_site_content(payloads: dict[str, object], message: str = "Elixir POAP KINGS site update") -> bool:
    """Publish one coherent POAP KINGS site bundle to GitHub.

    Returns True when a commit was created, False when nothing changed.
    """
    if not site_enabled():
        raise RuntimeError("POAP KINGS site integration is disabled or missing GitHub configuration")

    branch = _site_branch()
    changed_entries = []
    for content_type, data in (payloads or {}).items():
        if content_type not in CONTENT_FILES:
            raise ValueError(f"Unknown content type: {content_type}")
        serialized = serialize_content(data)
        current = load_published(content_type, branch=branch)
        current_serialized = serialize_content(current) if current is not None else None
        if current_serialized == serialized:
            continue
        blob = _github_request(
            "POST",
            "/git/blobs",
            payload={"content": serialized, "encoding": "utf-8"},
            expected=(201,),
        )
        changed_entries.append(
            {
                "path": target_path(content_type),
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"],
            }
        )

    if not changed_entries:
        log.info("POAP KINGS site publish: no changes")
        return False

    ref = _github_request("GET", f"/git/ref/heads/{branch}")
    parent_commit_sha = ((ref or {}).get("object") or {}).get("sha")
    if not parent_commit_sha:
        raise RuntimeError(f"Could not resolve branch head for {branch}")
    parent_commit = _github_request("GET", f"/git/commits/{parent_commit_sha}")
    base_tree_sha = ((parent_commit or {}).get("tree") or {}).get("sha")
    if not base_tree_sha:
        raise RuntimeError("Could not resolve base tree for POAP KINGS site publish")

    tree = _github_request(
        "POST",
        "/git/trees",
        payload={"base_tree": base_tree_sha, "tree": changed_entries},
        expected=(201,),
    )
    commit = _github_request(
        "POST",
        "/git/commits",
        payload={
            "message": message,
            "tree": tree["sha"],
            "parents": [parent_commit_sha],
        },
        expected=(201,),
    )
    _github_request(
        "PATCH",
        f"/git/refs/heads/{branch}",
        payload={"sha": commit["sha"]},
        expected=(200,),
    )
    log.info("Published POAP KINGS site bundle to %s@%s (%d file(s))", _site_repo(), branch, len(changed_entries))
    return True


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


def _hydrate_member_card_data(member, conn, existing_member=None):
    """Fill member card fields from cached DB data, then prior published data."""
    tag = member["tag"]

    cached_signature = db.get_member_signature_cards("#" + tag, conn=conn)
    cached_cards = (cached_signature or {}).get("cards") or []
    if cached_cards:
        member["favorite_cards"] = cached_cards

    cached_deck = db.get_member_current_deck("#" + tag, conn=conn)
    cached_deck_cards = (cached_deck or {}).get("cards") or []
    if cached_deck_cards:
        member["current_deck"] = [c.get("name", "") for c in cached_deck_cards if c.get("name")]
        member["_current_deck_icons"] = {
            c.get("name", ""): c.get("iconUrls", {}).get("medium", "")
            for c in cached_deck_cards
            if c.get("name")
        }

    existing_member = existing_member or {}
    if existing_member.get("favorite_cards") and not member.get("favorite_cards"):
        member["favorite_cards"] = existing_member["favorite_cards"]
    if existing_member.get("current_deck") and not member.get("current_deck"):
        member["current_deck"] = existing_member["current_deck"]


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

    Returns dict for elixir-roster.json, including any stored generated
    member bios/highlights from shared DB state.
    """
    close = conn is None
    conn = conn or db.get_connection()
    try:
        member_list = clan_data.get("memberList", [])
        metadata = db.get_member_metadata_map(conn=conn)
        profile_showcase = _latest_profile_showcase_map(conn)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = _load_existing_content("roster")
        existing_by_tag = {m["tag"]: m for m in existing.get("members", [])} if existing else {}

        members = []
        for m in member_list:
            tag = m.get("tag", "").lstrip("#")
            role = ROLE_MAP.get(m.get("role", "member"), "Member")

            arena = m.get("arena", {})
            arena_name = arena.get("name", "") if isinstance(arena, dict) else ""

            extra = metadata.get(tag, {})
            showcase = profile_showcase.get(tag, {})

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
                "cr_account_age_days": extra.get("cr_account_age_days"),
                "cr_account_age_years": extra.get("cr_account_age_years"),
                "cr_account_age_updated_at": extra.get("cr_account_age_updated_at"),
                "cr_games_per_day": extra.get("cr_games_per_day"),
                "cr_games_per_day_window_days": extra.get("cr_games_per_day_window_days"),
                "cr_games_per_day_updated_at": extra.get("cr_games_per_day_updated_at"),
                "badge_count": showcase.get("badge_count"),
                "badge_highlights": showcase.get("badge_highlights", []),
                "mastery_highlights": showcase.get("mastery_highlights", []),
                "achievement_star_count": showcase.get("achievement_star_count"),
                "achievement_completed_count": showcase.get("achievement_completed_count"),
                "achievement_progress": showcase.get("achievement_progress", []),
                "bio": extra.get("bio", ""),
                "highlight": extra.get("highlight", ""),
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
                    _hydrate_member_card_data(member, conn, existing_member=existing_by_tag.get(tag))

                try:
                    player_data = cr_api.get_player(tag)
                    member["current_deck"] = extract_current_deck(player_data)
                    member["_current_deck_icons"] = extract_current_deck_icons(player_data)
                    if player_data:
                        snapshot_payload = dict(player_data)
                        snapshot_payload.setdefault("tag", "#" + tag)
                        snapshot_payload.setdefault("name", member["name"])
                        db.snapshot_player_profile(snapshot_payload, conn=conn)
                        showcase = _profile_showcase_fields(snapshot_payload)
                        member["badge_count"] = showcase.get("badge_count")
                        member["badge_highlights"] = showcase.get("badge_highlights", [])
                        member["mastery_highlights"] = showcase.get("mastery_highlights", [])
                        member["achievement_star_count"] = showcase.get("achievement_star_count")
                        member["achievement_completed_count"] = showcase.get("achievement_completed_count")
                        member["achievement_progress"] = showcase.get("achievement_progress", [])
                        refreshed_meta = db.get_member_metadata("#" + tag, conn=conn) or {}
                        member["cr_account_age_days"] = refreshed_meta.get("cr_account_age_days")
                        member["cr_account_age_years"] = refreshed_meta.get("cr_account_age_years")
                        member["cr_account_age_updated_at"] = refreshed_meta.get("cr_account_age_updated_at")
                        member["cr_games_per_day"] = refreshed_meta.get("cr_games_per_day")
                        member["cr_games_per_day_window_days"] = refreshed_meta.get("cr_games_per_day_window_days")
                        member["cr_games_per_day_updated_at"] = refreshed_meta.get("cr_games_per_day_updated_at")
                except Exception:
                    _hydrate_member_card_data(member, conn, existing_member=existing_by_tag.get(tag))

                time.sleep(0.3)
        else:
            for member in members:
                _hydrate_member_card_data(
                    member,
                    conn,
                    existing_member=existing_by_tag.get(member["tag"]),
                )

        # Backfill from existing file when DB-shared generated profiles are not
        # present yet (mainly for older local snapshots).
        if existing:
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
        card_stats = build_card_stats(members)
        if card_stats:
            result["card_stats"] = card_stats
        elif not include_cards and existing and existing.get("card_stats"):
            result["card_stats"] = existing["card_stats"]
        for member in members:
            member.pop("_current_deck_icons", None)
        return result
    finally:
        if close:
            conn.close()


__all__ = [
    "CONTENT_FILES",
    "build_card_stats",
    "build_clan_data",
    "build_roster_data",
    "commit_and_push",
    "extract_current_deck",
    "extract_current_deck_icons",
    "load_current",
    "load_published",
    "publish_site_content",
    "serialize_content",
    "site_enabled",
    "target_path",
    "validate_against_schema",
    "write_content",
    "aggregate_card_usage",
]
