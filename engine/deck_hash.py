"""Form-aware deck identity — the pure hash and card-form helpers shared by
Battle Intelligence Features 1-3 (docs/plans/battle-intelligence-1-data.md §2).

Card form (base / Evolution / Hero) is part of card identity: CR overloaded
``evolution_level`` as a FORM discriminator when Heroes shipped (``1`` = Evo,
``2`` = Hero; confirmed 14 Hero cards == the 14 catalog ``hero_icon_url`` cards).
A deck's evo/hero choices are strategy, so base Knight, Evo Knight, and Hero
Knight are three distinct cards and three distinct decks. ``star_level`` is
cosmetic and never part of identity.

Reads the stored ``deck_json`` shape, whose keys are snake_case
(``id``/``evolution_level``/``star_level``), verified against live data.

Pure functions only. No DB, no I/O.
"""

from __future__ import annotations

import hashlib
from typing import Iterable


def card_form(evolution_level) -> str:
    """``'base' | 'evo' | 'hero'`` from a card's ``evolution_level``.

    NULL/absent/0 -> base, 1 -> evo, >=2 -> hero. The ``>=2`` fold treats the
    rare level-3 tier (Wizard/Knight, ~20 plays) as ``hero`` until its semantics
    are confirmed via the cr-api-doc-audit skill (plan §2).
    """
    if not isinstance(evolution_level, int) or evolution_level <= 0:
        return "base"
    if evolution_level == 1:
        return "evo"
    return "hero"


def _identity_pairs(cards: Iterable[dict]) -> list[tuple[int, int]]:
    """Sorted ``(card_id, evolution_level)`` pairs — the form-aware identity of
    a deck's cards. ``evolution_level`` normalized to 0 for base (None/absent);
    ``star_level`` excluded (cosmetic); cards without an ``id`` skipped."""
    pairs = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        cid = card.get("id")
        if cid is None:
            continue
        evo = card.get("evolution_level")
        evo = evo if isinstance(evo, int) and evo > 0 else 0
        pairs.append((int(cid), evo))
    return sorted(pairs)


def deck_hash(cards: Iterable[dict]) -> str | None:
    """16-hex ``sha256`` over the sorted ``(card_id, evolution_level)`` pairs.

    Form-aware: decks running Evo Knight, Hero Knight, or base Knight hash
    differently. Order-independent (pairs are sorted) and ``star_level``-blind.
    Returns None for a card list with no identifiable cards.
    """
    pairs = _identity_pairs(cards)
    if not pairs:
        return None
    payload = ";".join(f"{cid}:{evo}" for cid, evo in pairs)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
