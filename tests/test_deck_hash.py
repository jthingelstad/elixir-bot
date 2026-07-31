"""Unit tests for the form-aware deck-identity helpers (Battle Intelligence F1)."""

from engine.deck_hash import card_form, deck_hash


def test_card_form_maps_evolution_level_to_form():
    assert card_form(None) == "base"
    assert card_form(0) == "base"
    assert card_form(1) == "evo"
    assert card_form(2) == "hero"
    assert card_form(3) == "hero"  # level-3 tier folds to hero until confirmed
    assert card_form(-1) == "base"
    assert card_form("1") == "base"  # non-int is not a form


def _deck(*specs):
    """specs are (id, evolution_level, star_level) tuples."""
    return [
        {"id": cid, "name": f"c{cid}", "level": 11, "evolution_level": evo, "star_level": star}
        for cid, evo, star in specs
    ]


def test_deck_hash_is_order_independent():
    a = _deck((1, None, None), (2, None, None), (3, 1, None))
    b = _deck((3, 1, None), (1, None, None), (2, None, None))
    assert deck_hash(a) == deck_hash(b)


def test_deck_hash_is_form_aware():
    base = _deck((100, None, None))
    evo = _deck((100, 1, None))
    hero = _deck((100, 2, None))
    assert base != evo
    assert len({deck_hash(base), deck_hash(evo), deck_hash(hero)}) == 3


def test_deck_hash_ignores_star_level():
    plain = _deck((7, None, None), (8, 1, None))
    starred = _deck((7, None, 3), (8, 1, 2))
    assert deck_hash(plain) == deck_hash(starred)


def test_deck_hash_treats_absent_and_zero_evolution_as_base():
    absent = [{"id": 5, "name": "x", "level": 11}]  # no evolution_level key
    zero = _deck((5, 0, None))
    assert deck_hash(absent) == deck_hash(zero)


def test_deck_hash_none_for_empty_or_idless():
    assert deck_hash([]) is None
    assert deck_hash(None) is None
    assert deck_hash([{"name": "no id"}]) is None


def test_deck_hash_is_stable_16_hex():
    h = deck_hash(_deck((1, None, None)))
    assert isinstance(h, str) and len(h) == 16
    assert all(ch in "0123456789abcdef" for ch in h)
