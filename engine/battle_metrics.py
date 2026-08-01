"""Pure per-battle computed metrics for Battle Intelligence Feature 1
(docs/plans/battle-intelligence-1-data.md §2). No model, no DB, no I/O.

All formulas verified against 12,433 live 1v1 battles (2026-07-30):
- The princess-HP JSON array lists ONLY surviving towers; a destroyed king
  reports ``king_tower_hp = 0`` (not NULL), so the standing term needs no
  COALESCE. ``standing == 3 - crowns_against`` held in 99.99% of battles.
- ``closeness`` band cuts come from the real ``|hp_margin|`` quartiles
  (p25 4200 / p50 5800 / p75 7700), not a priori guesses.
"""

from __future__ import annotations

import json


def _standing_and_hp(king_hp, princess_json) -> tuple[int | None, float | None]:
    """``(standing_towers, total_hp)`` for one side, or ``(None, None)`` when the
    tower fields are absent. ``standing`` counts a live king (hp>0) plus the
    surviving princess towers (the array holds only survivors)."""
    if king_hp is None or princess_json is None:
        return None, None
    try:
        arr = json.loads(princess_json) if isinstance(princess_json, str) else princess_json
    except TypeError, ValueError:
        return None, None
    if not isinstance(arr, list):
        return None, None
    princess = [x for x in arr if isinstance(x, (int, float))]
    king = king_hp or 0
    standing = (1 if king > 0 else 0) + len(princess)
    return standing, king + sum(princess)


def hp_margin(our_king, our_princess_json, opp_king, opp_princess_json) -> int | None:
    """``(our_standing - opp_standing) * 3000 + (our_hp - opp_hp)``. Positive =
    the member finished ahead. NULL if either side's tower fields are absent.
    The 3000 is a coarse tower-worth constant — only ``closeness`` reads it, and
    only as a band."""
    s1, h1 = _standing_and_hp(our_king, our_princess_json)
    s2, h2 = _standing_and_hp(opp_king, opp_princess_json)
    if s1 is None or s2 is None:
        return None
    return int((s1 - s2) * 3000 + (h1 - h2))


def closeness_band(margin) -> int | None:
    """0 stomp .. 3 squeaker, from ``|hp_margin|`` (data-driven quartile cuts).
    NULL when ``hp_margin`` is NULL."""
    if margin is None:
        return None
    a = abs(margin)
    if a < 4200:
        return 3  # squeaker
    if a < 5800:
        return 2
    if a < 7700:
        return 1
    return 0  # stomp


def _avg_level(cards, max_levels=None) -> float | None:
    """Mean card level on the DISPLAY scale when ``max_levels`` is supplied.

    API levels are rarity-relative, so averaging them raw makes the number depend
    on a deck's rarity MIX rather than its strength: eight maxed commons average
    16, eight maxed legendaries average 8, and both decks are equally maxed.
    ``max_levels`` maps card_id -> the card's rarity max, which is what turns the
    average into something two decks can be compared on.
    """
    from engine.normalize import card_display_level

    levels = []
    for c in cards or []:
        if not isinstance(c, dict) or not isinstance(c.get("level"), (int, float)):
            continue
        cap = (max_levels or {}).get(c.get("id"))
        levels.append(card_display_level(c["level"], cap) if cap else c["level"])
    return sum(levels) / len(levels) if levels else None


def level_gap(member_cards, opponent_cards, *, is_ranked=False, max_levels=None) -> float | None:
    """``avg(member card levels) - avg(opponent card levels)`` from the two decks,
    on the display scale. **NULL for ranked** (Path of Legends normalizes every
    card to level 11, so the stored account levels are fictional in-battle — the
    tool reports reason ``levels_normalized``). Deck-scoped, never an
    account-strength claim (plan Feature 4).

    This was "rarity-naive by design" and the design was wrong. Averaging
    rarity-relative levels measures a deck's rarity MIX as much as its strength, so
    a deck of maxed legendaries (8s) looked four levels weaker than a deck of maxed
    commons (16s) when both are maxed. Measured across 8,000 real battles: 53% of
    level gaps were off by a full level or more, the worst by 5.88, and 23% —
    nearly one battle in four — crossed the +/-2.0 line that decisive_factor uses
    to name card levels as the reason someone lost. One reads +2.62 raw against a
    true +0.38. Card levels rank FIRST in that ladder, so a wrong gap does not
    merely mislabel a battle, it outranks every other explanation for it.

    Pass ``max_levels`` (card_id -> rarity max) to get the corrected number;
    without it the old rarity-relative behaviour is preserved for callers that
    genuinely have no catalog.
    """
    if is_ranked:
        return None
    a = _avg_level(member_cards, max_levels)
    b = _avg_level(opponent_cards, max_levels)
    if a is None or b is None:
        return None
    return round(a - b, 2)


def discipline_delta(opponent_elixir_leaked, elixir_leaked) -> float | None:
    """``opponent_elixir_leaked - elixir_leaked``. Positive = the member wasted
    less elixir. NULL if either is NULL."""
    if opponent_elixir_leaked is None or elixir_leaked is None:
        return None
    return round(opponent_elixir_leaked - elixir_leaked, 2)
