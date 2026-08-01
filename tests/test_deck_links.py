"""Clash Royale deck share links — generation and parsing.

The reference string in these tests is a REAL link copied from a 2026 client
(King Thing's primary deck, 2026-08-01). Every claim about the format is checked
against it rather than against documentation, because the published sources do not
describe the slots/tt/id parameters at all.
"""

from engine.deck_links import build_deck_link, looks_like_a_deck_link, parse_deck_link

# Witch, Barbarian Barrel, Royal Hogs, Skeleton Army, Arrows, Inferno Dragon,
# Cannon, Ronin — all resolved against our own card_catalog.
REAL = (
    "King Thing wants to share a Clash Royale deck: "
    "https://link.clashroyale.com/en?clashroyale://copyDeck"
    "?deck=26000007;28000015;26000059;26000012;28000001;26000037;27000000;26000106"
    "&slots=0;0;0;0;0;0;0;0&tt=159000000&id=20JJJ2CCRU"
)
CARDS = [26000007, 28000015, 26000059, 26000012, 28000001, 26000037, 27000000, 26000106]


def test_parses_a_real_link_out_of_the_sentence_it_arrives_in():
    """The client prefixes '<name> wants to share a Clash Royale deck: ', so the
    payload is never alone on the line."""
    got = parse_deck_link(REAL)
    assert got["card_ids"] == CARDS
    assert got["tower_troop_id"] == 159000000
    assert got["shared_by_tag"] == "#20JJJ2CCRU"


def test_card_order_is_preserved():
    """Deck order carries meaning to the player looking at their own list."""
    assert parse_deck_link(REAL)["card_ids"][0] == 26000007  # Witch, first slot


def test_the_older_web_form_and_the_bare_uri_both_parse():
    """Three shapes circulate: the nested 2026 form, the long-standing
    link.clashroyale.com/deck/en form, and a bare clashroyale:// URI."""
    joined = ";".join(str(c) for c in CARDS)
    for variant in (
        f"https://link.clashroyale.com/deck/en?deck={joined}",
        f"clashroyale://copyDeck?deck={joined}",
    ):
        assert parse_deck_link(variant)["card_ids"] == CARDS


def test_a_link_never_carries_evolution_or_hero_forms():
    """The load-bearing limitation. That real link reports all-zero slots, and the
    same eight cards in the same order in that player's battle log ran Evo Witch,
    Hero Barbarian Barrel and Evo Royal Hogs. Nothing in the link says so, so a
    parsed deck must never be treated as knowing which cards are evolved."""
    assert parse_deck_link(REAL)["forms_known"] is False


def test_a_partial_deck_is_not_a_deck():
    """Seven ids is someone's truncated paste. Filling in the eighth would put a card
    in their deck that they never shared."""
    assert parse_deck_link("...?deck=26000007;28000015;26000059") is None
    assert parse_deck_link("no link here at all") is None
    assert parse_deck_link("") is None
    assert parse_deck_link(None) is None


def test_round_trips_through_generation():
    link = build_deck_link(CARDS, tower_troop_id=159000000)
    back = parse_deck_link(link)
    assert back["card_ids"] == CARDS
    assert back["tower_troop_id"] == 159000000


def test_generated_links_match_the_shape_a_real_client_emits():
    link = build_deck_link(CARDS)
    assert link.startswith("https://link.clashroyale.com/en?clashroyale://copyDeck?deck=")
    assert "&slots=0;0;0;0;0;0;0;0" in link
    # tt is ALWAYS emitted. Links without it arrived intact and did nothing when
    # tapped: a tower troop is part of a deck, so an 8-card payload without one is
    # an incomplete deck. Tower Princess is the fallback every account owns.
    assert "&tt=159000000" in link


def test_we_never_claim_to_be_the_sharer():
    """'id' is the human who copied the deck. Elixir is not one, so it is omitted
    rather than filled with the member's tag."""
    assert "&id=" not in build_deck_link(CARDS, tower_troop_id=159000000)


def test_a_bad_deck_size_generates_nothing():
    assert build_deck_link(CARDS[:7]) is None
    assert build_deck_link([*CARDS, 26000000]) is None
    assert build_deck_link([]) is None


def test_looks_like_a_deck_link_is_the_routing_precheck():
    assert looks_like_a_deck_link(REAL) is True
    assert looks_like_a_deck_link("what should I upgrade next?") is False


def test_slot_hungry_cards_are_listed_first():
    """A deck copied with a Champion sixth in the list arrived with only seven
    cards. The game equips evolutions the player owns as it walks the deck, and by
    the time it reached the Champion there was no slot left. Cards that need one of
    the three special slots now claim their seats first."""
    link = build_deck_link(CARDS, slot_first=[CARDS[5], CARDS[7]])
    ids = [int(x) for x in link.split("?deck=")[1].split("&")[0].split(";")]
    assert ids[:2] == [CARDS[5], CARDS[7]]
    assert sorted(ids) == sorted(CARDS), "reordering must never change the deck"
    assert len(ids) == 8


def test_ordering_is_untouched_when_nothing_needs_a_slot():
    link = build_deck_link(CARDS)
    ids = [int(x) for x in link.split("?deck=")[1].split("&")[0].split(";")]
    assert ids == CARDS
