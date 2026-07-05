"""King Tower Level — computed from card levels (CR 2026 Collection Level update).

Clash Royale removed Experience Level / King's Journey; King Tower Level is now
earned by upgrading a required COUNT of cards to a required level (Supercell blog
"New Collection Levels and Mastery Changes"). It is NOT `min(expLevel, 16)` — that
read a field the game deprecated — and it is NOT any single tower-troop level (the
default Tower Princess happens to track it via auto-upgrade, but alternate tower
troops are leveled independently). We compute it ourselves from the player's card
collection. See memory cr-progression-model-2026.

Validated against King Thing (real King Tower 16): 17 cards at display-level 15+
clears the "14 at 15+" bar → 16. The card level MUST be the display/unified level
(engine.normalize.card_display_level); raw API levels give the wrong answer.
"""

from __future__ import annotations

# (king_tower_level, cards_needed, at_card_level_or_higher) — Supercell's
# "Total Upgrades (New)" table. King Tower Level 1 is the starting level.
KING_TOWER_REQUIREMENTS: tuple[tuple[int, int, int], ...] = (
    (2, 9, 1), (3, 9, 2), (4, 10, 3), (5, 10, 4), (6, 10, 5), (7, 10, 6),
    (8, 10, 7), (9, 10, 8), (10, 10, 9), (11, 10, 10), (12, 11, 11),
    (13, 11, 12), (14, 12, 13), (15, 13, 14), (16, 14, 15),
)

MAX_KING_TOWER_LEVEL = 16


def king_tower_level(card_display_levels: list[int]) -> int:
    """Highest King Tower Level whose card-count requirement is met.

    `card_display_levels`: the player's main-collection card levels on the
    DISPLAY/unified scale (engine.normalize.card_display_level). Tower troops are
    excluded (they do not count toward the requirement). Returns 1 when no
    threshold is met (the starting level)."""
    level = 1
    for kt, needed, at_level in KING_TOWER_REQUIREMENTS:
        if sum(1 for lvl in card_display_levels if lvl is not None and lvl >= at_level) >= needed:
            level = kt
        # thresholds are monotonic (more cards at higher levels) — but we scan
        # all rows rather than break, so a non-monotonic edge can't strand us low.
    return level
