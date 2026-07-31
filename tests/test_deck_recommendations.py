"""Deck Intelligence (``get_deck_recommendations``) — ownership and level gating.

The load-bearing property is that no view can ever suggest a deck the member cannot
actually build. These tests pin that, plus the two measurement traps the feature was
designed around: rarity-relative card levels, and the absence of win rates.
"""

import json
import sqlite3

import pytest

from capabilities.deck_intel import get_deck_recommendations

TAG = "#AAA"


def _deck(cards):
    return json.dumps([[cid, form] for cid, form in cards])


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE card_catalog (card_id INTEGER PRIMARY KEY, name TEXT, elixir_cost INTEGER,
            rarity TEXT, max_level INTEGER, max_evolution_level INTEGER);
        CREATE TABLE player_card_collection (player_tag TEXT, card_id INTEGER, level INTEGER,
            evolution_level INTEGER);
        CREATE TABLE battle_card_plays (battle_dedup_key TEXT, side TEXT, card_id INTEGER,
            evolution_level INTEGER, player_tag TEXT, battle_time TEXT);
        CREATE TABLE battle_enrichment (battle_dedup_key TEXT, our_deck_hash TEXT);
        CREATE TABLE battle_events (dedup_key TEXT, player_tag TEXT);
        CREATE TABLE deck_profile (deck_hash TEXT PRIMARY KEY, family TEXT, archetype TEXT,
            avg_elixir REAL, cards_json TEXT, air_answer_count INTEGER, tank_answer_count INTEGER,
            splash_answer_count INTEGER, has_big_spell INTEGER, has_small_spell INTEGER,
            facts_complete INTEGER);
        """
    )
    # 9 cards across rarities. Legendary maxes at 8, common at 16 — a legendary at 8 is
    # MAXED while a common at 8 is halfway, which is the trap the feature must not fall into.
    cards = [
        (1, "Legend", 4, "legendary", 8, 0),
        (2, "Common", 2, "common", 16, 1),
        (3, "Rare", 3, "rare", 14, 0),
        (4, "Epic", 4, "epic", 11, 0),
        (5, "Cheap", 1, "common", 16, 0),
        (6, "Spell", 3, "rare", 14, 0),
        (7, "Tank", 5, "epic", 11, 0),
        (8, "Air", 3, "rare", 14, 0),
        (9, "Unowned", 4, "legendary", 8, 0),
    ]
    c.executemany("INSERT INTO card_catalog VALUES (?,?,?,?,?,?)", cards)
    # Member owns 1..8 at MAX for their rarity; does not own card 9.
    for cid, _n, _e, _r, mx, _me in cards[:8]:
        c.execute("INSERT INTO player_card_collection VALUES (?,?,?,?)", (TAG, cid, mx, None))
    buildable = [(i, 0) for i in range(1, 9)]
    unbuildable = [(i, 0) for i in range(2, 9)] + [(9, 0)]
    needs_evo = [(1, 0), (2, 1)] + [(i, 0) for i in range(3, 9)]
    c.executemany(
        "INSERT INTO deck_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("H_OK", "cycle", "Test Cycle", 3.0, _deck(buildable), 2, 1, 1, 1, 1, 1),
            ("H_NO", "bait", "Test Bait", 3.2, _deck(unbuildable), 2, 1, 1, 1, 1, 1),
            ("H_EVO", "control", "Test Control", 3.4, _deck(needs_evo), 2, 1, 1, 1, 1, 1),
        ],
    )
    yield c
    c.close()


def test_unowned_card_blocks_a_deck(conn):
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    got = {d["archetype"] for d in r["suggestions"]}
    assert "Test Cycle" in got
    assert "Test Bait" not in got, "recommended a deck containing an unowned card"


def test_unowned_evolution_blocks_a_deck(conn):
    """Owning the base card is not owning its Evolution — the form must be owned too."""
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    assert "Test Control" not in {d["archetype"] for d in r["suggestions"]}


def test_played_evolution_counts_as_owned(conn):
    """The profile's evolutionLevel is unreliable, so a form the member has actually
    fielded must count as owned — otherwise real decks get wrongly filtered out."""
    conn.execute(
        "INSERT INTO battle_card_plays VALUES ('b1','member',2,1,?,'2026-07-01T00:00:00Z')",
        (TAG,),
    )
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    assert "Test Control" in {d["archetype"] for d in r["suggestions"]}


def test_maxed_collection_reports_nothing_to_upgrade(conn):
    """Every owned card is at its rarity max. A raw-level comparison would call the
    legendary at level 8 'weak'; levels_from_max must report zero work to do."""
    conn.execute(
        "INSERT INTO battle_card_plays VALUES ('b1','member',1,0,?,'2026-07-01T00:00:00Z')",
        (TAG,),
    )
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=conn)
    assert r["all_played_cards_maxed"] is True
    assert r["upgrades"] == []


def test_upgrades_rank_by_usage_not_raw_gap(conn):
    """A heavily-played card 1 level down must outrank a barely-played card 5 down."""
    conn.execute("UPDATE player_card_collection SET level=15 WHERE card_id=2")  # common, -1
    conn.execute("UPDATE player_card_collection SET level=6 WHERE card_id=4")  # epic, -5
    for i in range(20):
        conn.execute(
            "INSERT INTO battle_card_plays VALUES (?,'member',2,0,?,'2026-07-01T00:00:00Z')",
            (f"p{i}", TAG),
        )
    conn.execute(
        "INSERT INTO battle_card_plays VALUES ('q1','member',4,0,?,'2026-07-01T00:00:00Z')",
        (TAG,),
    )
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=conn)
    assert r["upgrades"][0]["card"] == "Common"


def test_anchored_only_returns_decks_with_the_anchor(conn):
    r = get_deck_recommendations(view="anchored", member_tag=TAG, card="Legend", conn=conn)
    assert r["available"] is True
    assert r["buildable_decks_with_anchor"] >= 1
    for d in r["decks"]:
        assert any(c["name"] == "Legend" for c in d["cards"])


def test_anchored_rejects_a_card_the_member_does_not_own(conn):
    r = get_deck_recommendations(view="anchored", member_tag=TAG, card="Unowned", conn=conn)
    assert r["available"] is False
    assert r["error"] == "card_not_owned"


def test_war_set_needs_four_disjoint_decks(conn):
    """Only one buildable deck exists here, so a war set is genuinely impossible —
    it must say so rather than return a short or card-sharing set."""
    r = get_deck_recommendations(view="war_set", member_tag=TAG, conn=conn)
    assert r["available"] is False
    assert r["error"] == "no_feasible_set"


def test_no_view_reports_a_win_rate(conn):
    """Clan deck win rates are skill-confounded and non-transferable. If one ever leaks
    into this tool, the model will quote it as evidence a deck is good."""
    for view, kw in (
        ("discover", {}),
        ("upgrades", {}),
        ("anchored", {"card": "Legend"}),
        ("war_set", {}),
    ):
        blob = json.dumps(get_deck_recommendations(view=view, member_tag=TAG, conn=conn, **kw))
        assert "win_rate" not in blob, f"{view} leaked a win rate"


def test_meta_overlay_absent_is_not_an_error(conn):
    """No meta_decks table (pre-v31 or never refreshed) must degrade, not raise."""
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    assert r["available"] is True
    assert r["meta_snapshot_available"] is False


def test_incidental_cards_do_not_become_upgrade_advice(conn):
    """Everyone dabbles, so some played card is always below max. Without a usage floor
    a maxed veteran gets "upgrade Bandit" off 1.8% usage — true, and useless."""
    conn.execute("UPDATE player_card_collection SET level=6 WHERE card_id=1")  # legendary -2
    # 1 play of the under-levelled card against 60 plays of maxed ones
    conn.execute(
        "INSERT INTO battle_card_plays VALUES ('x0','member',1,0,?,'2026-07-01T00:00:00Z')",
        (TAG,),
    )
    for i in range(60):
        conn.execute(
            "INSERT INTO battle_card_plays VALUES (?,'member',3,0,?,'2026-07-01T00:00:00Z')",
            (f"y{i}", TAG),
        )
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=conn)
    assert r["no_material_upgrades"] is True
    assert r["upgrades"] == []
    assert r["incidental_cards_below_max"] == 1
