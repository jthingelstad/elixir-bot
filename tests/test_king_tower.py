"""King Tower Level calculation (CR 2026 Collection Level model)."""

from engine.king_tower import KING_TOWER_REQUIREMENTS, king_tower_level


def test_starting_level_when_no_cards():
    assert king_tower_level([]) == 1
    assert king_tower_level([1] * 5) == 1  # only 5 cards at L1+, need 9 for KT2


def test_kt2_needs_nine_cards_at_level_1():
    assert king_tower_level([1] * 8) == 1
    assert king_tower_level([1] * 9) == 2


def test_kt16_needs_fourteen_cards_at_level_15():
    assert king_tower_level([15] * 13) == 15  # one short of the 14 needed
    assert king_tower_level([15] * 14) == 16


def test_king_thing_profile_is_16():
    """King Thing's real collection: 17 cards at display 15+ (9 at 16, 8 at 15),
    plenty at every lower tier → King Tower 16 (his true level)."""
    levels = [16] * 9 + [15] * 8 + [14] * 20 + [13] * 20 + [12] * 30 + [11] * 14
    assert king_tower_level(levels) == 16


def test_monotonic_and_capped():
    # a fully-maxed collection tops out at exactly 16, never higher
    assert king_tower_level([16] * 120) == 16
    # a mid-account: 11 cards at L11+ but not 11 at L12+ → KT12
    assert king_tower_level([11] * 11 + [1] * 30) == 12


def test_none_levels_ignored():
    assert king_tower_level([15] * 14 + [None] * 5) == 16


def test_table_shape():
    # sanity: 15 rows (KT2..KT16), counts and thresholds as published
    assert len(KING_TOWER_REQUIREMENTS) == 15
    assert KING_TOWER_REQUIREMENTS[0] == (2, 9, 1)
    assert KING_TOWER_REQUIREMENTS[-1] == (16, 14, 15)
