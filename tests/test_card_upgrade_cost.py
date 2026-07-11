"""_ready_required must key the upgrade-cost table on the rarity-relative
api_level, not the 1-16 display level (QA H15/H16).

Different rarities share the same display level range but have different
per-rarity upgrade tables; using the display level indexed the wrong row (or
past the table end -> a spurious None), mispricing ~75% of cards and hiding
ready-to-upgrade cards clan-wide.
"""
from __future__ import annotations

from cr_knowledge import cards_required_to_upgrade
from storage.cards import _ready_required


def test_ready_required_uses_api_level_not_display_level():
    # Epic card sitting at display level 11 but rarity-relative api_level 6
    # (e.g. P.E.K.K.A). Cost must reflect epic@6, not epic@11.
    card = {"rarity": "epic", "level": 11, "api_level": 6, "maxLevel": 16, "count": 13}
    assert _ready_required(card) == cards_required_to_upgrade("epic", 6)
    assert _ready_required(card) != cards_required_to_upgrade("epic", 11)


def test_ready_required_falls_back_to_level_when_api_level_absent():
    card = {"rarity": "common", "level": 11, "maxLevel": 16, "count": 100}
    assert _ready_required(card) == cards_required_to_upgrade("common", 11)


def test_ready_required_none_when_maxed():
    card = {"rarity": "common", "level": 16, "api_level": 14, "maxLevel": 16,
            "api_max_level": 14, "count": 0}
    assert _ready_required(card) is None
