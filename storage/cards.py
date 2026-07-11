"""Card collection, deck, and card-usage queries (member and clan level).

Split out of storage/roster.py, which keeps membership/roster queries.
Everything here reasons about cards: a member's current deck, card
collection and levels, card lookups, and clan-wide card aggregations
(favourites, maxed, recently played, overlooked).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    _canon_tag,
    _card_level,
    managed_connection,
)
from storage._enrichment import _member_reference_fields


def _card_mode_value(card: dict, camel_key: str, snake_key: str) -> int | None:
    value = card.get(camel_key)
    if value is None:
        value = card.get(snake_key)
    return value if isinstance(value, int) and value >= 0 else None


def _card_mode_fields(card: dict) -> dict:
    """Derive Evo/Hero capability and unlock labels for a collection card.

    `maxEvolutionLevel` encodes which alternate modes a card *supports*:
    1 = Evo-capable, 2 = Hero-capable, 3 = both. This mapping is inferred
    empirically from live payloads (it lines up with the presence of
    `evolutionMedium`/`heroMedium`) — it is an interpretation layer for
    player-facing output, not a documented guarantee. `evolutionLevel` here
    means *ownership* (which modes the player has unlocked), distinct from
    how a card was deployed in a battle/deck — for that see `_played_as` in
    db/__init__.py.
    """
    max_evolution_level = _card_mode_value(card, "maxEvolutionLevel", "max_evolution_level")
    evolution_level = _card_mode_value(card, "evolutionLevel", "evolution_level")

    supports_evo = max_evolution_level in {1, 3}
    supports_hero = max_evolution_level in {2, 3}
    evo_unlocked = evolution_level in {1, 3}
    hero_unlocked = evolution_level in {2, 3}

    mode_label = None
    if evo_unlocked and hero_unlocked:
        mode_label = "Evo + Hero"
    elif evo_unlocked:
        mode_label = "Evo"
    elif hero_unlocked:
        mode_label = "Hero"

    return {
        "supports_evo": supports_evo,
        "supports_hero": supports_hero,
        "evo_unlocked": evo_unlocked,
        "hero_unlocked": hero_unlocked,
        "mode_label": mode_label,
        "mode_status_label": f"{mode_label} unlocked" if mode_label else None,
    }

@managed_connection
def get_member_current_deck(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT cs.current_deck_json, NULL AS current_deck_support_cards_json, "
        "cs.observed_at AS fetched_at "
        "FROM player_current_state cs "
        "WHERE cs.player_tag = ? ",
        (_canon_tag(tag),),
    ).fetchone()
    if not row or not row["current_deck_json"]:
        return None
    cards = []
    for raw_card in json.loads(row["current_deck_json"]):
        if isinstance(raw_card, dict):
            cards.append(_normalize_collection_card(raw_card))
    support_cards = []
    for raw_card in json.loads(row["current_deck_support_cards_json"] or "[]"):
        if isinstance(raw_card, dict):
            support_cards.append(_normalize_collection_card(raw_card))
    return {
        "fetched_at": row["fetched_at"],
        "cards": cards,
        "support_cards": support_cards,
    }




def _load_collection_cards(conn, tag: str):
    """v5.1: rebuild CR-shaped card dicts from player_card_collection (row per
    card) x card_catalog — replaces the cards_json snapshot blobs. Support
    cards are not tracked in the projection; callers degrade to []."""
    rows = conn.execute(
        "SELECT pc.card_id, pc.level, pc.count, pc.star_level, pc.evolution_level, "
        "pc.observed_at, cc.name, cc.rarity, cc.max_level, cc.elixir_cost, cc.max_evolution_level "
        "FROM player_card_collection pc "
        "LEFT JOIN card_catalog cc ON cc.card_id = pc.card_id "
        "WHERE pc.player_tag = ?",
        (_canon_tag(tag),),
    ).fetchall()
    fetched_at = None
    cards = []
    for r in rows:
        fetched_at = max(fetched_at or "", r["observed_at"] or "")
        cards.append({
            "id": r["card_id"],
            "name": r["name"] or f"card:{r['card_id']}",
            "level": r["level"],
            "maxLevel": r["max_level"],
            "count": r["count"],
            "rarity": r["rarity"],
            "starLevel": r["star_level"],
            "evolutionLevel": r["evolution_level"],
            "maxEvolutionLevel": r["max_evolution_level"],
            "elixirCost": r["elixir_cost"],
        })
    return fetched_at, cards


def _normalize_collection_card(raw_card: dict) -> dict:
    card = dict(raw_card or {})
    if "api_level" not in card:
        display_level = _card_level(card)
        if display_level is not None:
            card["api_level"] = card.get("level")
            card["level"] = display_level
    max_level = card.get("maxLevel")
    api_max_level = card.get("api_max_level")
    if "api_max_level" not in card and isinstance(max_level, int) and 0 < max_level <= 16:
        card["api_max_level"] = max_level
        card["maxLevel"] = 16
    elif isinstance(api_max_level, int) and isinstance(max_level, int):
        card["maxLevel"] = max_level
    if isinstance(card.get("level"), int) and isinstance(card.get("maxLevel"), int):
        card["levels_to_max"] = max(0, card["maxLevel"] - card["level"])
        card["is_max_level"] = card["level"] >= card["maxLevel"]
    card.update(_card_mode_fields(card))
    return card


def _card_sort_key(card: dict) -> tuple:
    return (
        -(card.get("level") or 0),
        -(_card_mode_value(card, "evolutionLevel", "evolution_level") or 0),
        (card.get("elixirCost") if isinstance(card.get("elixirCost"), int) else 99),
        (card.get("name") or "").lower(),
    )


def _normalize_rarity_filter(value: str | None) -> str | None:
    raw = (value or "").strip().lower().replace("-", "").replace(" ", "")
    if not raw:
        return None
    aliases = {
        "common": "common",
        "commons": "common",
        "rare": "rare",
        "rares": "rare",
        "epic": "epic",
        "epics": "epic",
        "legendary": "legendary",
        "legendaries": "legendary",
        "champion": "champion",
        "champions": "champion",
    }
    # QA L17: return None for anything outside the five real rarities (e.g.
    # 'mythic') so the lookup_member_cards unknown_rarity guard actually fires,
    # instead of passing the bogus value through to a valid-looking 0-match
    # result. Card rarities from the API are always one of the five, so a card's
    # own rarity never trips this.
    return aliases.get(raw)


def _card_reference_for_collection(card: dict, *, card_type: str | None = None) -> dict:
    item = {
        "name": card.get("name"),
        "level": card.get("level"),
        "maxLevel": card.get("maxLevel"),
        "rarity": card.get("rarity"),
        "supports_evo": bool(card.get("supports_evo")),
        "supports_hero": bool(card.get("supports_hero")),
        "evo_unlocked": bool(card.get("evo_unlocked")),
        "hero_unlocked": bool(card.get("hero_unlocked")),
    }
    if card.get("levels_to_max") is not None:
        item["levels_to_max"] = card.get("levels_to_max")
    evolution_level = _card_mode_value(card, "evolutionLevel", "evolution_level")
    if evolution_level is not None:
        item["evolution_level"] = evolution_level
    max_evolution_level = _card_mode_value(card, "maxEvolutionLevel", "max_evolution_level")
    if max_evolution_level is not None:
        item["max_evolution_level"] = max_evolution_level
    if card.get("mode_label"):
        item["mode_label"] = card.get("mode_label")
    if card.get("mode_status_label"):
        item["mode_status_label"] = card.get("mode_status_label")
    if card_type:
        item["card_type"] = card_type
    return item


def _collection_cards_by_rarity(cards: list[dict], support_cards: list[dict]) -> dict:
    combined = [
        (card, "card")
        for card in (cards or [])
        if card.get("name")
    ] + [
        (card, "support")
        for card in (support_cards or [])
        if card.get("name")
    ]
    combined.sort(key=lambda item: _card_sort_key(item[0]))

    grouped: dict[str, list[str]] = {}
    for card, card_type in combined:
        rarity = _normalize_rarity_filter(card.get("rarity")) or "unknown"
        name = card.get("name")
        if not name:
            continue
        if card_type == "support":
            grouped.setdefault(rarity, []).append(f"{name} (support)")
        else:
            grouped.setdefault(rarity, []).append(name)
    return grouped


def _collection_summary_from_cards(cards: list[dict], support_cards: list[dict]) -> dict:
    combined = [card for card in [*(cards or []), *(support_cards or [])] if card.get("name")]
    level_counts: dict[str, int] = {}
    rarity_counts: dict[str, int] = {}
    highest_level = None
    maxed_cards_count = 0
    for card in combined:
        level = card.get("level")
        max_level = card.get("maxLevel")
        rarity = _normalize_rarity_filter(card.get("rarity")) or "unknown"
        rarity_counts[rarity] = rarity_counts.get(rarity, 0) + 1
        if isinstance(level, int):
            highest_level = max(level, highest_level or level)
            level_counts[str(level)] = level_counts.get(str(level), 0) + 1
            if isinstance(max_level, int) and level >= max_level:
                maxed_cards_count += 1

    strongest_cards = []
    sorted_cards = sorted(combined, key=_card_sort_key)
    for card in sorted_cards[:12]:
        strongest_cards.append(_card_reference_for_collection(card))

    return {
        "cards_tracked": len(cards or []),
        "support_cards_tracked": len(support_cards or []),
        "combined_cards_tracked": len(combined),
        "highest_level": highest_level,
        "maxed_cards_count": maxed_cards_count,
        "level_counts": level_counts,
        "rarity_counts": rarity_counts,
        "strongest_cards": strongest_cards,
    }


@managed_connection
def get_member_card_collection(tag: str, limit: Optional[int] = None, min_level: Optional[int] = None, include_support: bool = True, rarity: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    fetched_at, raw_cards = _load_collection_cards(conn, tag)
    if not raw_cards:
        return None
    row = {"fetched_at": fetched_at}

    cards = [
        _normalize_collection_card(raw_card)
        for raw_card in raw_cards
        if isinstance(raw_card, dict) and raw_card.get("name")
    ]
    support_cards = []

    if isinstance(min_level, int):
        cards = [card for card in cards if (card.get("level") or 0) >= min_level]
        support_cards = [card for card in support_cards if (card.get("level") or 0) >= min_level]

    cards.sort(key=_card_sort_key)
    support_cards.sort(key=_card_sort_key)

    total_cards = len(cards)
    total_support_cards = len(support_cards)
    collection_summary = _collection_summary_from_cards(cards, support_cards)
    rarity_key = _normalize_rarity_filter(rarity)
    if rarity_key:
        cards = [
            card for card in cards
            if (_normalize_rarity_filter(card.get("rarity")) or "unknown") == rarity_key
        ]
        support_cards = [
            card for card in support_cards
            if (_normalize_rarity_filter(card.get("rarity")) or "unknown") == rarity_key
        ]
    summary = _collection_summary_from_cards(cards, support_cards)
    cards_by_rarity = _collection_cards_by_rarity(cards, support_cards)
    matching_total_cards = summary.get("combined_cards_tracked", 0)
    if isinstance(limit, int) and limit >= 0:
        cards = cards[:limit]
        support_cards = support_cards[:limit]

    result = {
        "fetched_at": row["fetched_at"],
        "returned_cards": len(cards),
        "returned_support_cards": len(support_cards),
        "total_cards": total_cards,
        "total_support_cards": total_support_cards,
        "summary": summary,
        "cards_by_rarity": cards_by_rarity,
        "cards": cards,
        "support_cards": support_cards,
    }
    if rarity_key:
        result["rarity_filter"] = rarity_key
        result["matching_total_cards"] = matching_total_cards
        result["collection_summary"] = collection_summary
    return result


def _card_count(card: dict) -> int:
    value = card.get("count")
    return value if isinstance(value, int) else 0


def _is_max(card: dict) -> bool:
    level = card.get("level")
    max_level = card.get("maxLevel")
    return isinstance(level, int) and isinstance(max_level, int) and level >= max_level


def _ready_required(card: dict) -> Optional[int]:
    """Cards required to advance this card one level. None if maxed/unknown.

    QA H15/H16: cards_required_to_upgrade is keyed on the RARITY-RELATIVE level
    (the api_level, 1-indexed per rarity), NOT the 1-16 display level. Passing
    the display level indexed into the wrong row (or past the table end -> a
    spurious None), mispricing ~75% of cards and hiding ready-to-upgrade cards
    clan-wide. Use api_level, falling back to level only if it's absent."""
    from cr_knowledge import cards_required_to_upgrade
    if _is_max(card):
        return None
    level = card.get("api_level")
    if level is None:
        level = card.get("level")
    return cards_required_to_upgrade(card.get("rarity"), level)


def _enrich_card_for_lookup(card: dict, king_tower_level: Optional[int]) -> dict:
    """Project a normalized card for lookup output: adds count, upgrade
    readiness, and king-tower gap. Keeps the slim shape used in tool output."""
    enriched = dict(card)
    needed = _ready_required(card)
    count = _card_count(card)
    if needed is not None:
        enriched["count"] = count
        enriched["cards_required_for_next_level"] = needed
        enriched["ready_to_upgrade"] = count >= needed
        if not enriched["ready_to_upgrade"] and needed > 0:
            enriched["progress_to_next_level"] = round(count / needed, 2)
    else:
        enriched["count"] = count
        enriched["ready_to_upgrade"] = False
    level = card.get("level")
    if isinstance(king_tower_level, int) and isinstance(level, int):
        enriched["king_tower_gap"] = king_tower_level - level
    return enriched


def _load_collection(conn: sqlite3.Connection, member_tag: str) -> Optional[dict]:
    """Read the latest card-collection snapshot and return normalized cards.

    Returns None when the member has no snapshot yet.
    """
    fetched_at, raw_cards = _load_collection_cards(conn, member_tag)
    if not raw_cards:
        return None
    row = {"fetched_at": fetched_at, "support_cards_json": "[]"}
    cards = [
        _normalize_collection_card(raw_card)
        for raw_card in raw_cards
        if isinstance(raw_card, dict) and raw_card.get("name")
    ]
    support_cards = [
        _normalize_collection_card(raw_card)
        for raw_card in json.loads(row["support_cards_json"] or "[]")
        if isinstance(raw_card, dict) and raw_card.get("name")
    ]
    return {
        "fetched_at": row["fetched_at"],
        "cards": cards,
        "support_cards": support_cards,
    }


# King Tower Level caps at 16. Clash Royale removed Experience Level (2026
# Collection Level update): King Tower Level is now EARNED by upgrading a
# required count of cards to a required level, and we compute it from the card
# collection (engine.king_tower) rather than reading the deprecated `expLevel`.
# See memory cr-progression-model-2026.
KING_TOWER_MAX_LEVEL = 16


def _card_display_levels(conn: sqlite3.Connection, member_tag: str) -> list[int]:
    """The player's main-collection card levels on the display/unified scale.
    Tower troops are excluded — they do not count toward King Tower Level."""
    from engine.normalize import card_display_level

    rows = conn.execute(
        "SELECT pc.level, c.max_level FROM player_card_collection pc "
        "JOIN card_catalog c ON c.card_id = pc.card_id "
        "WHERE pc.player_tag = ? AND c.card_type != 'tower_troop'",
        (_canon_tag(member_tag),),
    ).fetchall()
    out = []
    for row in rows:
        display = card_display_level(row["level"], row["max_level"])
        if display is not None:
            out.append(display)
    return out


def _king_tower_level(conn: sqlite3.Connection, member_tag: str) -> Optional[int]:
    """King Tower Level (1–16), computed from the card collection per the CR
    2026 requirements table (engine.king_tower.king_tower_level) — NOT the
    deprecated `min(expLevel, 16)`. Validated against King Thing → 16. This is
    the right reference for "is this card underleveled vs my tower"."""
    from engine.king_tower import king_tower_level

    levels = _card_display_levels(conn, member_tag)
    if not levels:
        return None
    return king_tower_level(levels)


@managed_connection
def get_member_card_profile(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Compact card-collection digest. Always small (~3KB), always answers
    broad questions ("how am I doing on cards", "what should I upgrade")
    without sending raw card lists.

    Reads *ownership* state from the player_profile_snapshots collection —
    what the player has unlocked — not played-as/deployment state. For "how
    was this card actually played" (e.g. signature card as Evo), read
    `currentDeck` / battle-log `deck_json` instead.
    """
    snapshot = _load_collection(conn, tag)
    if snapshot is None:
        return None
    king_tower = _king_tower_level(conn, tag)
    all_cards = [c for c in snapshot["cards"] + snapshot["support_cards"] if c.get("name")]

    # Aggregate counts.
    totals = {
        "owned": len(all_cards),
        "max_level": sum(1 for c in all_cards if _is_max(c)),
        "level_13_plus": sum(1 for c in all_cards if isinstance(c.get("level"), int) and c["level"] >= 13),
        "level_14_plus": sum(1 for c in all_cards if isinstance(c.get("level"), int) and c["level"] >= 14),
    }

    by_rarity: dict[str, dict] = {}
    for card in all_cards:
        rarity = _normalize_rarity_filter(card.get("rarity")) or "unknown"
        bucket = by_rarity.setdefault(rarity, {"owned": 0, "ready": 0, "maxed": 0})
        bucket["owned"] += 1
        if _is_max(card):
            bucket["maxed"] += 1
        elif _ready_required(card) is not None and _card_count(card) >= _ready_required(card):
            bucket["ready"] += 1

    modes = {
        "evo_unlocked": sum(1 for c in all_cards if c.get("evo_unlocked")),
        "hero_unlocked": sum(1 for c in all_cards if c.get("hero_unlocked")),
        "supports_evo": sum(1 for c in all_cards if c.get("supports_evo")),
        "supports_hero": sum(1 for c in all_cards if c.get("supports_hero")),
    }

    # Top lists. Keep entries minimal to stay under 3KB total.
    def _slim_for_digest(card: dict, *, fields: list[str]) -> dict:
        out = {"name": card.get("name"), "level": card.get("level"), "rarity": card.get("rarity")}
        for field in fields:
            value = card.get(field)
            if value is not None:
                out[field] = value
        return out

    enriched = [_enrich_card_for_lookup(c, king_tower) for c in all_cards]
    ready_top = sorted(
        (c for c in enriched if c.get("ready_to_upgrade")),
        key=lambda c: (-_card_count(c), c.get("name") or ""),
    )[:5]
    closest_top = sorted(
        (c for c in enriched if not _is_max(c) and isinstance(c.get("levels_to_max"), int)),
        key=lambda c: (c["levels_to_max"], -(c.get("level") or 0), c.get("name") or ""),
    )[:5]
    if king_tower is not None:
        gap_top = sorted(
            (c for c in enriched if isinstance(c.get("king_tower_gap"), int) and c["king_tower_gap"] > 0),
            key=lambda c: (-c["king_tower_gap"], c.get("name") or ""),
        )[:5]
    else:
        gap_top = []

    return {
        "member_tag": _canon_tag(tag),
        "fetched_at": snapshot["fetched_at"],
        "king_tower_level": king_tower,
        "king_tower_max": KING_TOWER_MAX_LEVEL,
        "totals": totals,
        "by_rarity": by_rarity,
        "modes": modes,
        "ready_to_upgrade_top": [
            _slim_for_digest(c, fields=["count", "cards_required_for_next_level", "king_tower_gap"])
            for c in ready_top
        ],
        "closest_to_max_top": [
            _slim_for_digest(c, fields=["levels_to_max", "count", "cards_required_for_next_level", "ready_to_upgrade", "king_tower_gap"])
            for c in closest_top
        ],
        "biggest_king_tower_gaps_top": [
            _slim_for_digest(c, fields=["king_tower_gap", "levels_to_max", "ready_to_upgrade"])
            for c in gap_top
        ],
    }


_LOOKUP_FILTER_HINTS = [
    "deck=true (current Trophy Road deck, 8 cards)",
    "mode=war (inferred war decks from recent battles — not authoritative)",
    "rarity=common|rare|epic|legendary|champion (full collection by rarity)",
    "name=<card name> (lookup one card; substring match, case-insensitive)",
    "ready_to_upgrade=true (cards the player has enough copies to level up now)",
    "near_ready=true (at least halfway to a level-up, but not yet ready)",
    "near_max=true (1-2 levels from max)",
    "maxed=true (already at max level)",
    "evo_unlocked=true | hero_unlocked=true | has_special_mode=true",
]


def _filter_required_response() -> dict:
    return {
        "error": "filter_required",
        "available_filters": list(_LOOKUP_FILTER_HINTS),
        "suggest_clarify": (
            "Ask the user which scope they mean before retrying. "
            "'Cards' is ambiguous: it could mean current deck, war decks, full collection, "
            "by rarity, ready-to-upgrade, etc."
        ),
    }


def _matches_filter(card: dict, filt: dict, *, war_card_names: set[str]) -> bool:
    if filt.get("rarity"):
        if (_normalize_rarity_filter(card.get("rarity")) or "unknown") != filt["rarity"]:
            return False
    if filt.get("name"):
        target = filt["name"].lower()
        if target not in (card.get("name") or "").lower():
            return False
    if filt.get("mode") == "war":
        if (card.get("name") or "") not in war_card_names:
            return False
    if filt.get("ready_to_upgrade"):
        needed = _ready_required(card)
        if needed is None or _card_count(card) < needed:
            return False
    if filt.get("near_ready"):
        needed = _ready_required(card)
        if needed is None or _card_count(card) >= needed:
            return False
        if _card_count(card) < (needed * 0.5):
            return False
    if filt.get("near_max"):
        if _is_max(card):
            return False
        levels_to_max = card.get("levels_to_max")
        if not (isinstance(levels_to_max, int) and 1 <= levels_to_max <= 2):
            return False
    if filt.get("maxed") and not _is_max(card):
        return False
    if filt.get("evo_unlocked") and not card.get("evo_unlocked"):
        return False
    if filt.get("hero_unlocked") and not card.get("hero_unlocked"):
        return False
    if filt.get("has_special_mode"):
        if not (card.get("evo_unlocked") or card.get("hero_unlocked")):
            return False
    return True


def _war_card_names(conn: sqlite3.Connection, member_tag: str) -> tuple[set[str], dict]:
    """Names of cards across the player's inferred war decks (mode=war filter),
    plus the reconstruction evidence.

    QA M19: the flat name set alone gives the caller no signal whether it came
    from 16 war battles or 2, or whether it collapses one deck or four. Return
    the evidence (war_battles_seen / distinct_decks / confidence / status)
    alongside so lookup_member_cards can size the reconstruction honestly.
    """
    from storage.war_analytics import reconstruct_member_war_decks
    result = reconstruct_member_war_decks(member_tag, conn=conn)
    names: set[str] = set()
    decks = result.get("decks", []) or []
    for deck in decks:
        for card in deck.get("cards", []) or []:
            name = card.get("name") if isinstance(card, dict) else None
            if name:
                names.add(name)
    evidence = dict(result.get("evidence") or {})
    evidence.setdefault("decks_reconstructed", len(decks))
    coverage = {
        "war_battles_seen": evidence.get("war_battles_seen"),
        "distinct_decks_observed": evidence.get("distinct_decks_observed"),
        "decks_reconstructed": evidence.get("decks_reconstructed"),
        "confidence": result.get("confidence"),
        "status": result.get("status"),
    }
    return names, coverage


@managed_connection
def list_card_owners(
    card_name: str,
    maxed_only: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Clan-wide owners of one card — the 'who has X maxed?' question in ONE
    query (rehearsal 2026-07-04: the per-member tool answered it via 30
    sequential calls and still missed owners past the call budget)."""
    name = (card_name or "").strip()
    if not name:
        return {"error": "card_name required"}
    rows = conn.execute(
        """SELECT COALESCE(p.display_name, p.current_name) AS member, cc.name AS card,
                  pcc.level + MAX(0, 16 - cc.max_level) AS display_level,
                  pcc.evolution_level
           FROM player_card_collection pcc
           JOIN card_catalog cc ON cc.card_id = pcc.card_id
           JOIN players p ON p.player_tag = pcc.player_tag
           JOIN clan_memberships cm ON cm.player_tag = pcc.player_tag
                AND cm.left_at IS NULL
           WHERE cc.name = ? COLLATE NOCASE
           ORDER BY display_level DESC, p.current_name COLLATE NOCASE""",
        (name,),
    ).fetchall()
    owners = [dict(r) for r in rows]
    if maxed_only:
        owners = [o for o in owners if (o["display_level"] or 0) >= 16]
    return {"card": name, "maxed_only": maxed_only, "count": len(owners),
            "owners": owners}


@managed_connection
def lookup_member_cards(
    tag: str,
    filter: Optional[dict] = None,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Filtered query over a member's card collection (ownership state, not
    played-as/deployment — see `get_member_card_profile`).

    Filter is required — a missing or empty filter returns a structured
    `filter_required` error so the agent is prompted to ask the user which
    scope they meant. See _LOOKUP_FILTER_HINTS for the full list.
    """
    if not filter or not any(filter.values()):
        return _filter_required_response()

    filt = dict(filter)
    rarity = filt.get("rarity")
    if rarity:
        filt["rarity"] = _normalize_rarity_filter(rarity)
        if filt["rarity"] is None:
            return {"error": "unknown_rarity", "value": rarity}

    snapshot = _load_collection(conn, tag)
    if snapshot is None:
        return {"error": "no_collection_snapshot", "member_tag": _canon_tag(tag)}

    king_tower = _king_tower_level(conn, tag)

    # deck=true is a special case: pull from current_deck, not collection.
    if filt.get("deck"):
        deck = get_member_current_deck(tag, conn=conn)
        if not deck:
            return {"error": "no_current_deck", "member_tag": _canon_tag(tag)}
        deck_cards = [_enrich_card_for_lookup(c, king_tower) for c in deck.get("cards", [])]
        return {
            "fetched_at": deck.get("fetched_at"),
            "filter_applied": {"deck": True},
            "total_matching": len(deck_cards),
            "returned": len(deck_cards),
            "cards": deck_cards,
        }

    war_names: set[str] = set()
    war_coverage: dict = {}
    if filt.get("mode") == "war":
        war_names, war_coverage = _war_card_names(conn, tag)

    all_cards = [c for c in snapshot["cards"] + snapshot["support_cards"] if c.get("name")]
    matching = [c for c in all_cards if _matches_filter(c, filt, war_card_names=war_names)]

    # Sort: ready-to-upgrade first prioritizes by count surplus; otherwise by level desc.
    if filt.get("ready_to_upgrade"):
        matching.sort(key=lambda c: -(_card_count(c) - (_ready_required(c) or 0)))
    elif filt.get("near_ready"):
        matching.sort(key=lambda c: -(_card_count(c) / max(_ready_required(c) or 1, 1)))
    elif filt.get("near_max"):
        matching.sort(key=lambda c: (c.get("levels_to_max") or 99, -(c.get("level") or 0)))
    else:
        matching.sort(key=_card_sort_key)

    enforced_limit = max(0, min(int(limit) if isinstance(limit, int) else 20, 50))
    total_matching = len(matching)
    matching = matching[:enforced_limit]
    enriched = [_enrich_card_for_lookup(c, king_tower) for c in matching]

    result = {
        "fetched_at": snapshot["fetched_at"],
        "filter_applied": {k: v for k, v in filt.items() if v},
        "total_matching": total_matching,
        "returned": len(enriched),
        "cards": enriched,
    }
    if filt.get("mode") == "war":
        result["war_deck_caveat"] = (
            "War decks are inferred from cards played in recent war battles, not "
            "authoritative — the CR API does not expose them. Phrase carefully."
        )
        # QA M19: size the reconstruction so the caller can weigh it — a flat
        # total_matching hides whether these cards came from 16 war battles or 2,
        # or collapse one deck vs four.
        result["war_coverage"] = war_coverage
    return result


@managed_connection
def get_member_signature_cards(tag: str, mode_scope: str = "overall", conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    # v5.1: usage comes from battle_events deck_json (last 40 battles).
    rows = conn.execute(
        "SELECT deck_json, battle_time FROM battle_events "
        "WHERE player_tag = ? AND deck_json IS NOT NULL "
        "ORDER BY battle_time DESC LIMIT 40",
        (_canon_tag(tag),),
    ).fetchall()
    if not rows:
        return None
    counts: dict[str, int] = {}
    for r in rows:
        try:
            deck = json.loads(r["deck_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for card in deck:
            name = card.get("name") if isinstance(card, dict) else None
            if name:
                counts[name] = counts.get(name, 0) + 1
    cards = [
        {"name": name, "battles_used": used, "usage_rate": round(used / len(rows), 3)}
        for name, used in sorted(counts.items(), key=lambda kv: -kv[1])[:16]
    ]
    return {
        "mode_scope": mode_scope,
        "sample_battles": len(rows),
        "fetched_at": rows[0]["battle_time"],
        "cards": cards,
    }

@managed_connection
def get_members_with_most_level_16_cards(limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    member_rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, cs.clan_rank, cs.role "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "WHERE EXISTS (SELECT 1 FROM clan_memberships cm "
        "  WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    ).fetchall()
    rows = []
    for mr in member_rows:
        fetched_at, raw_cards = _load_collection_cards(conn, mr["tag"])
        d = dict(mr)
        d["fetched_at"] = fetched_at
        d["_cards"] = raw_cards
        rows.append(d)
    result = []
    for row in rows:
        cards = row.pop("_cards")
        level_16_cards = sorted(
            {
                card.get("name")
                for card in cards
                if card.get("name") and _card_level(card) == 16
            }
        )
        item = {
            "tag": row["tag"],
            "name": row["name"],
            "clan_rank": row["clan_rank"],
            "role": row["role"],
            "snapshot_at": row["fetched_at"],
            "level_16_count": len(level_16_cards),
            "cards_tracked": len([card for card in cards if card.get("name")]),
            "level_16_cards": level_16_cards,
        }
        result.append(_member_reference_fields(conn, row["member_id"], item))
    result.sort(
        key=lambda item: (
            -(item.get("level_16_count") or 0),
            -(item.get("cards_tracked") or 0),
            item.get("clan_rank") if item.get("clan_rank") is not None else 999,
            (item.get("name") or "").lower(),
        )
    )
    return result[:limit]


@managed_connection
def get_clan_favourite_card_counts(limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    # v5.1: favourite card lives in the profile baseline payload.
    import json as _json

    counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT sb.payload_json FROM state_baselines sb "
        "WHERE sb.entity_kind = 'player' AND sb.aspect = 'profile' AND EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = sb.entity_tag AND cm.left_at IS NULL)"
    ).fetchall():
        try:
            payload = _json.loads(r["payload_json"])
        except (TypeError, ValueError):
            continue
        name = payload.get("favourite_card") or payload.get("currentFavouriteCard", {}).get("name") if isinstance(payload.get("currentFavouriteCard"), dict) else payload.get("favourite_card")
        if name:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [{"card_name": n, "member_count": c} for n, c in ranked[:limit]]


@managed_connection
def get_clan_most_common_maxed_cards(limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    member_tags = [r["player_tag"] for r in conn.execute(
        "SELECT m.player_tag FROM players m WHERE EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    ).fetchall()]
    rows = []
    for _tag in member_tags:
        _fetched, _cards = _load_collection_cards(conn, _tag)
        rows.append({"cards_json": json.dumps(_cards), "support_cards_json": "[]", "_tag": _tag})
    card_counts: dict[str, int] = {}
    for row in rows:
        for raw in [*json.loads(row["cards_json"] or "[]"), *json.loads(row["support_cards_json"] or "[]")]:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            if _card_level(raw) == 16:
                name = raw["name"]
                card_counts[name] = card_counts.get(name, 0) + 1
    ranked = sorted(card_counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"card_name": name, "member_count": count} for name, count in ranked[:limit]]


@managed_connection
def get_clan_recently_played_cards(days: int = 14, limit: int = 20, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Cards that appeared most often in clan members' recent battle decks."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime(
        "%Y%m%dT%H%M%S.000Z"
    )
    rows = conn.execute(
        "SELECT bf.deck_json "
        "FROM battle_events bf "
        "WHERE bf.battle_time >= ? AND EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = bf.player_tag AND cm.left_at IS NULL)",
        (cutoff,),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for card in json.loads(row["deck_json"] or "[]"):
            if isinstance(card, dict) and card.get("name"):
                counts[card["name"]] = counts.get(card["name"], 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"card_name": name, "battles": count} for name, count in ranked[:limit]]


@managed_connection
def get_clan_rare_maxed_cards(max_owners: int = 2, limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    member_tags = [r["player_tag"] for r in conn.execute(
        "SELECT m.player_tag FROM players m WHERE EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    ).fetchall()]
    rows = []
    for _tag in member_tags:
        _fetched, _cards = _load_collection_cards(conn, _tag)
        rows.append({"cards_json": json.dumps(_cards), "support_cards_json": "[]", "_tag": _tag})
    card_counts: dict[str, int] = {}
    for row in rows:
        for raw in [*json.loads(row["cards_json"] or "[]"), *json.loads(row["support_cards_json"] or "[]")]:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            if _card_level(raw) == 16:
                name = raw["name"]
                card_counts[name] = card_counts.get(name, 0) + 1
    rare = [(name, count) for name, count in card_counts.items() if count <= max_owners]
    rare.sort(key=lambda item: (item[1], item[0].lower()))
    return [{"card_name": name, "member_count": count} for name, count in rare[:limit]]


@managed_connection
def get_clan_overlooked_cards(min_owners: int = 3, min_level: int = 14, battle_days: int = 14, limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Cards that many members have leveled up but almost nobody actually plays."""
    # Step 1: cards owned at min_level+ across the clan, with owner counts
    member_tags = [r["player_tag"] for r in conn.execute(
        "SELECT m.player_tag FROM players m WHERE EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    ).fetchall()]
    owned: dict[str, int] = {}
    for _tag in member_tags:
        _fetched, _cards = _load_collection_cards(conn, _tag)
        for raw in _cards:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            level = _card_level(raw)
            if level is not None and level >= min_level:
                owned[raw["name"]] = owned.get(raw["name"], 0) + 1

    # Step 2: cards actually played in recent battles
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=battle_days)).strftime(
        "%Y%m%dT%H%M%S.000Z"
    )
    battle_rows = conn.execute(
        "SELECT bf.deck_json "
        "FROM battle_events bf "
        "WHERE bf.battle_time >= ? AND EXISTS ("
        "  SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = bf.player_tag AND cm.left_at IS NULL)",
        (cutoff,),
    ).fetchall()
    played: set[str] = set()
    for row in battle_rows:
        for card in json.loads(row["deck_json"] or "[]"):
            if isinstance(card, dict) and card.get("name"):
                played.add(card["name"])

    # Step 3: high-level cards owned by many but played by nobody (or very few)
    overlooked = [
        (name, count)
        for name, count in owned.items()
        if count >= min_owners and name not in played
    ]
    overlooked.sort(key=lambda item: (-item[1], item[0].lower()))
    return [{"card_name": name, "owners_at_level": count} for name, count in overlooked[:limit]]
