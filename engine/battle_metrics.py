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


def _avg_level(cards) -> float | None:
    levels = [
        c.get("level")
        for c in cards or []
        if isinstance(c, dict) and isinstance(c.get("level"), (int, float))
    ]
    return sum(levels) / len(levels) if levels else None


def level_gap(member_cards, opponent_cards, *, is_ranked=False) -> float | None:
    """``avg(member card levels) - avg(opponent card levels)`` from the two
    decks. **NULL for ranked** (Path of Legends normalizes every card to level
    11, so the stored account levels are fictional in-battle — the tool reports
    reason ``levels_normalized``). Rarity-naive by design; deck-scoped, never an
    account-strength claim (plan Feature 4)."""
    if is_ranked:
        return None
    a = _avg_level(member_cards)
    b = _avg_level(opponent_cards)
    if a is None or b is None:
        return None
    return round(a - b, 2)


def discipline_delta(opponent_elixir_leaked, elixir_leaked) -> float | None:
    """``opponent_elixir_leaked - elixir_leaked``. Positive = the member wasted
    less elixir. NULL if either is NULL."""
    if opponent_elixir_leaked is None or elixir_leaked is None:
        return None
    return round(opponent_elixir_leaked - elixir_leaked, 2)
