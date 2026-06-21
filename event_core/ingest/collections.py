"""Collections ingest: raw /players payload -> PlayerCollections.observe_*.

The JSON serialization here is a byte-for-byte mirror of the legacy
storage.player.snapshot_player_profile path so parity against the frozen legacy
snapshots is exact:

  - cards / support_cards: `_normalize_cards_for_storage` then
    `json.dumps(..., default=str, ensure_ascii=False)`, defaulting to "[]".
    Hash = sha256 of [cards_json, support_cards_json] (the legacy cards slide).
  - badges / achievements: the RAW payload arrays (NOT normalized),
    `json.dumps(payload.get("badges") or [], default=str, ensure_ascii=False)`.

These helpers are intentionally re-implemented locally (copied from
db.__init__ / storage.player) rather than imported, so the event_core slice has
no dependency on the legacy db/storage packages. They must stay in lockstep with
the legacy versions while parity is being proven.
"""
from __future__ import annotations

import hashlib
import json


# --- legacy card-level helpers (mirror of db._card_level / storage._card_display_max_level) ---
def _card_level(card: dict) -> int | None:
    level = card.get("level")
    if not isinstance(level, int):
        return None
    max_level = card.get("maxLevel")
    if not isinstance(max_level, int) or max_level <= 0 or max_level > 16:
        return level
    return level + max(0, 16 - max_level)


def _card_display_max_level(card: dict) -> int | None:
    max_level = card.get("maxLevel")
    if not isinstance(max_level, int) or max_level <= 0:
        return None
    if max_level > 16:
        return max_level
    return max_level + max(0, 16 - max_level)


def _normalize_cards_for_storage(cards: list[dict] | None) -> list[dict]:
    """Mirror of storage.player._normalize_cards_for_storage."""
    normalized = []
    for raw_card in cards or []:
        if not isinstance(raw_card, dict):
            continue
        card = dict(raw_card)
        raw_level = card.get("level")
        raw_max_level = card.get("maxLevel")
        display_level = _card_level(card)
        display_max_level = _card_display_max_level(card)
        if isinstance(raw_level, int):
            card["api_level"] = raw_level
        if isinstance(raw_max_level, int):
            card["api_max_level"] = raw_max_level
        if display_level is not None:
            card["level"] = display_level
        if display_max_level is not None:
            card["maxLevel"] = display_max_level
        if isinstance(card.get("level"), int) and isinstance(card.get("maxLevel"), int):
            card["levels_to_max"] = max(0, card["maxLevel"] - card["level"])
            card["is_max_level"] = card["level"] >= card["maxLevel"]
        normalized.append(card)
    return normalized


def _json_or_none(data) -> str | None:
    """Mirror of db._json_or_none."""
    if data is None:
        return None
    return json.dumps(data, default=str, ensure_ascii=False)


def _hash_payload(payload) -> str:
    """Mirror of db._hash_payload."""
    data = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def build_cards_observation(payload: dict) -> tuple[str, str, str]:
    """Return (cards_json, support_cards_json, content_hash) for the card slide.

    Matches legacy member_card_collection_snapshots exactly:
      cards_json_value      = _json_or_none(_normalize(cards)) or "[]"
      support_cards_json    = _json_or_none(_normalize(supportCards)) or "[]"
      content_hash          = _hash_payload([cards_json_value, support_cards_json_value])
    """
    cards = _normalize_cards_for_storage(payload.get("cards") or [])
    support_cards = _normalize_cards_for_storage(payload.get("supportCards") or [])
    cards_json_value = _json_or_none(cards) or "[]"
    support_cards_json_value = _json_or_none(support_cards) or "[]"
    content_hash = _hash_payload([cards_json_value, support_cards_json_value])
    return cards_json_value, support_cards_json_value, content_hash


def build_badges_observation(payload: dict) -> tuple[str, str]:
    """Return (badges_json, content_hash). Matches player_profile_snapshots.badges_json.

    Legacy serializes the RAW badges array (no normalization):
      _json_or_none(player_data.get("badges") or [])
    The legacy profile content_hash spans the whole profile tuple, so for the
    badges *slide* we hash the badges_json string itself (deterministic dedup of
    this collection's content), which is what makes the PlayerCollections badge
    timeline change only when badges change.
    """
    badges_json = _json_or_none(payload.get("badges") or [])
    content_hash = _hash_payload(badges_json)
    return badges_json, content_hash


def build_achievements_observation(payload: dict) -> tuple[str, str]:
    """Return (achievements_json, content_hash). Matches achievements_json column."""
    achievements_json = _json_or_none(payload.get("achievements") or [])
    content_hash = _hash_payload(achievements_json)
    return achievements_json, content_hash


def ingest_player_collections(app, payload: dict, observed_at: str) -> dict:
    """Ingest one raw /players payload's collections.

    Returns a dict of which collections changed state (emitted an event):
    {"cards": bool, "badges": bool, "achievements": bool}.
    """
    tag = payload.get("tag")
    if not tag:
        return {"cards": False, "badges": False, "achievements": False}

    cards_json, support_cards_json, cards_hash = build_cards_observation(payload)
    badges_json, badges_hash = build_badges_observation(payload)
    achievements_json, achievements_hash = build_achievements_observation(payload)

    return app.observe_player_collections(
        tag,
        cards_json=cards_json,
        support_cards_json=support_cards_json,
        cards_hash=cards_hash,
        badges_json=badges_json,
        badges_hash=badges_hash,
        achievements_json=achievements_json,
        achievements_hash=achievements_hash,
        observed_at=observed_at,
    )
