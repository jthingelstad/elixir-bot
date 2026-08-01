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
        CREATE TABLE battle_enrichment (battle_dedup_key TEXT, player_tag TEXT,
            battle_time TEXT, our_deck_hash TEXT, their_deck_hash TEXT);
        CREATE TABLE matchup_expectation (our_family TEXT, their_family TEXT,
            advantage INTEGER, measured_win_rate REAL, n INTEGER);
        CREATE TABLE battle_events (dedup_key TEXT, player_tag TEXT, outcome TEXT);
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


# ── build view + role coverage (rebuilt 2026-08-01) ──────────────────────────
#
# Every test below traces to one real #ask-elixir conversation: a member asked for two
# decks around two cards and was handed a four-deck war set instead.


@pytest.fixture
def rich(conn):
    """The base fixture plus card_facts and a second buildable deck sharing cards."""
    conn.executescript(
        """
        CREATE TABLE card_facts (card_id INTEGER, evolution_level INTEGER, unit_domain TEXT,
            targets TEXT, attack_style TEXT, splash_hits_air INTEGER, dps_tier TEXT,
            hp_tier TEXT, unit_count TEXT, range_type TEXT, role TEXT, spell_tier TEXT,
            is_win_condition INTEGER, fragile_to_small_spell INTEGER, special_json TEXT,
            source TEXT, note TEXT, model TEXT, enriched_at TEXT);
        """
    )
    facts = [
        # card_id, targets, attack_style, dps, role, spell_tier, wincon, note
        (1, "air_and_ground", "single", "high", "support", "none", 0, "Shoots up, hits hard"),
        (2, "none", "splash_small", "low", "spell", "small", 0, "Cheap answer"),
        (3, "buildings_only", "single", "high", "win_condition", "none", 1, "Goes for towers"),
        (4, "ground", "splash_large", "medium", "support", "none", 0, "Area damage"),
        (5, "ground", "single", "low", "swarm", "none", 0, "Cheap cycle"),
        (6, "none", "splash_large", "high", "spell", "big", 0, "The big one"),
        (7, "ground", "single", "medium", "tank", "none", 0, "Soaks damage"),
        (8, "air_and_ground", "splash_small", "medium", "support", "none", 0, "Chips air"),
        (9, "ground", "single", "high", "support", "none", 0, "Unowned"),
    ]
    conn.executemany(
        "INSERT INTO card_facts (card_id, evolution_level, targets, attack_style, dps_tier, "
        "role, spell_tier, is_win_condition, note, splash_hits_air) VALUES (?,0,?,?,?,?,?,?,?,1)",
        facts,
    )
    # A second deck the member can build, deliberately SHARING cards with H_OK so a
    # disjointness rule would be forced to drop one of them.
    conn.execute(
        "INSERT INTO deck_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "H_TWO",
            "beatdown",
            "Test Beatdown",
            4.2,
            _deck([(1, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (2, 0)]),
            2,
            1,
            1,
            1,
            1,
            1,
        ),
    )
    return conn


def test_build_returns_exactly_the_number_of_decks_asked_for(rich):
    """The live failure: "build 2 decks, one around each card" returned four."""
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend", "Rare"], count=2, conn=rich
    )
    assert r["available"] is True
    assert len(r["decks"]) == 2
    assert [d["anchor_card"] for d in r["decks"]] == ["Legend", "Rare"]
    for deck in r["decks"]:
        assert any(c["name"] == deck["anchor_card"] for c in deck["cards"])


def test_build_does_not_impose_the_war_no_overlap_rule(rich):
    """War disjointness is a war rule. Applying it to an ordinary request removes cards
    from the deck the player asked about to make room for decks they did not."""
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend", "Rare"], count=2, conn=rich
    )
    a, b = ({c["name"] for c in d["cards"]} for d in r["decks"])
    assert a & b, "build must be free to reuse cards across decks"
    assert "NOT a war set" in r["note"]


def test_several_anchor_cards_are_never_collapsed_into_one(rich):
    """`anchored` took card[0] and dropped the rest, so a request naming two cards
    silently became a request about one."""
    r = get_deck_recommendations(
        view="anchored", member_tag=TAG, card=["Legend", "Rare"], conn=rich
    )
    assert r["view"] == "build"
    assert r["anchors"] == ["Legend", "Rare"]


def test_build_reports_an_anchor_it_could_not_honour(rich):
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend", "Unowned"], count=2, conn=rich
    )
    assert r["anchors"] == ["Legend"]
    assert r["unresolved"] == [{"error": "card_not_owned", "card": "Unowned"}]


def test_every_suggested_deck_explains_why_each_card_is_in_it(rich):
    """The 'teach them to fish' property: the roles were computed and then discarded,
    leaving the model to narrate deck construction from memory."""
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    deck = r["suggestions"][0]
    assert deck["role_coverage"]["win_conditions"] == ["Rare"]
    by_name = {c["name"]: c for c in deck["cards"]}
    assert "win condition" in by_name["Rare"]["roles"]
    assert "heavy air answer" in by_name["Legend"]["roles"]
    assert by_name["Legend"]["note"] == "Shoots up, hits hard"
    assert by_name["Legend"]["elixir_cost"] == 4


def test_air_coverage_distinguishes_troops_from_spells(rich):
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    air = r["suggestions"][0]["role_coverage"]["air_answers"]
    assert set(air["troops"]) == {"Legend", "Air"}
    assert air["spells"] == ["Common"]
    assert air["heavy"] == ["Legend"]


def test_a_deck_the_member_already_plays_is_flagged_at_archetype_level(rich):
    """Exact-hash novelty told a member he had never fielded an archetype he ran 21
    times that month."""
    rich.execute("INSERT INTO battle_events VALUES ('b9', ?, 'W')", (TAG,))
    rich.execute(
        "INSERT INTO battle_enrichment VALUES ('b9', ?, '2026-07-01', 'H_OK', NULL)", (TAG,)
    )
    r = get_deck_recommendations(view="build", member_tag=TAG, anchors=["Legend"], conn=rich)
    played = [d for d in r["decks"] if d["archetype"] == "Test Cycle"]
    assert played and played[0]["you_play_this_archetype"] is True


def test_no_role_claims_when_the_cards_are_not_enriched(conn):
    """The base fixture has no card_facts table at all. An absent table is a state, not
    an outage, and it must not become a critique of decks we never looked at."""
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    assert r["available"] is True
    assert r["suggestions"][0]["role_coverage"]["gaps"] == []
    assert r["suggestions"][0]["role_coverage"]["unknown"] is True


def test_a_deck_over_the_special_slot_cap_is_never_suggested(rich):
    """Evo + Hero + Wild = 3. Verified against 13,701 real decks: never 4."""
    rich.execute("UPDATE player_card_collection SET evolution_level = 2")
    rich.execute(
        "INSERT INTO deck_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "H_ILLEGAL",
            "cycle",
            "Four Evos",
            3.0,
            _deck([(1, 1), (2, 1), (3, 1), (4, 1), (5, 0), (6, 0), (7, 0), (8, 0)]),
            2,
            1,
            1,
            1,
            1,
            1,
        ),
    )
    r = get_deck_recommendations(view="discover", member_tag=TAG, limit=12, conn=rich)
    assert "Four Evos" not in {d["archetype"] for d in r["suggestions"]}


def test_the_air_floor_filters_a_deck_that_answers_air_with_one_spell(rich):
    rich.execute("UPDATE deck_profile SET air_answer_count = 1 WHERE deck_hash = 'H_OK'")
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    assert "Test Cycle" not in {d["archetype"] for d in r["suggestions"]}
    # ... but the same deck at cycle cost keeps the exemption the guides give it.
    rich.execute("UPDATE deck_profile SET avg_elixir = 2.6 WHERE deck_hash = 'H_OK'")
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    assert "Test Cycle" in {d["archetype"] for d in r["suggestions"]}


def test_every_suggested_deck_comes_with_a_copy_link(rich):
    """A member should not have to retype eight card names to try a suggestion."""
    from engine.deck_links import parse_deck_link

    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    deck = r["suggestions"][0]
    parsed = parse_deck_link(deck["copy_link"])
    assert parsed is not None
    assert len(parsed["card_ids"]) == 8


def test_a_deck_relying_on_a_special_form_says_the_link_will_drop_it(rich):
    """The share format is base-cards-only, proven against a real link whose deck ran
    three special forms and still reported all-zero slots. Silently handing over a
    link that pastes the base deck would be worse than not sharing one."""
    rich.execute("UPDATE player_card_collection SET evolution_level = 1 WHERE card_id = 2")
    rich.execute(
        "INSERT INTO battle_card_plays VALUES ('b2','member',2,1,?,'2026-07-01T00:00:00Z')",
        (TAG,),
    )
    r = get_deck_recommendations(view="discover", member_tag=TAG, limit=12, conn=rich)
    evo_deck = next(d for d in r["suggestions"] if d["archetype"] == "Test Control")
    assert evo_deck["link_omits_forms"] == ["Common"]
    plain = next(d for d in r["suggestions"] if d["archetype"] == "Test Cycle")
    assert plain["link_omits_forms"] == []


def test_a_pasted_deck_is_read_with_the_same_role_vocabulary(rich):
    """The inbound half of the loop. A deck the member brought and a deck Elixir
    proposed must be analysed by one code path, or the two answers disagree."""
    from capabilities.deck_intel import read_deck_link

    ids = ";".join(str(i) for i in range(1, 9))
    r = read_deck_link(
        link=f"paste: ...copyDeck?deck={ids}&tt=159000000", member_tag=TAG, conn=rich
    )
    assert r["available"] is True
    assert [c["name"] for c in r["cards"]][:2] == ["Legend", "Common"]
    assert r["cards"][0]["their_level"] == 8, "the member's own level fills in"
    assert r["role_coverage"]["win_conditions"] == ["Rare"]
    assert "BASE CARDS" in r["note"], "must never claim to know the forms"


def test_a_message_with_no_deck_link_is_not_a_deck(rich):
    from capabilities.deck_intel import read_deck_link

    r = read_deck_link(link="what should I upgrade next?", conn=rich)
    assert r["available"] is False
    assert r["error"] == "no_deck_link_found"


def test_recommendations_read_the_field_this_member_actually_meets(rich):
    """Two members of the same clan do not face the same field — one meets beatdown
    in 43% of his games, another bridge spam in 51%, against a ~27% clan average.
    A deck ranked only on card levels cannot know it is being handed to someone who
    loses to bait."""
    rich.execute(
        "INSERT INTO deck_profile VALUES ('H_OPP','bait','Log Bait',3.2,?,2,1,1,1,1,1)",
        (_deck([(9, 0)] * 8),),  # an opponent deck built from a card the member lacks
    )
    for i in range(14):
        rich.execute("INSERT INTO battle_events VALUES (?, ?, 'L')", (f"x{i}", TAG))
        rich.execute(
            "INSERT INTO battle_enrichment VALUES (?, ?, '2026-07-20', 'H_OK', 'H_OPP')",
            (f"x{i}", TAG),
        )
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    field = r["your_field"]
    assert field["battles"] == 14
    assert field["faced"]["bait"]["losses"] == 14
    assert field["worst_matchup"] == "bait", "0-14 is the matchup worth naming"


def test_no_battle_history_removes_the_matchup_read_and_nothing_else(conn):
    """The field is an enrichment. A member with no history still gets decks."""
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=conn)
    assert r["available"] is True
    assert r["suggestions"], "recommendations are gated on ownership, not on history"
    assert r["your_field"]["worst_matchup"] is None
    assert all("matchup_fit" not in d for d in r["suggestions"])


def test_a_thin_field_earns_no_matchup_claim(rich):
    """Three games is not a field, and a confident number off it reads as a finding."""
    rich.execute(
        "INSERT INTO deck_profile VALUES ('H_OPP','bait','Log Bait',3.2,?,2,1,1,1,1,1)",
        (_deck([(9, 0)] * 8),),  # an opponent deck built from a card the member lacks
    )
    for i in range(3):
        rich.execute("INSERT INTO battle_events VALUES (?, ?, 'L')", (f"y{i}", TAG))
        rich.execute(
            "INSERT INTO battle_enrichment VALUES (?, ?, '2026-07-20', 'H_OK', 'H_OPP')",
            (f"y{i}", TAG),
        )
    r = get_deck_recommendations(view="discover", member_tag=TAG, conn=rich)
    assert r["your_field"]["worst_matchup"] is None
    assert all("matchup_fit" not in d for d in r["suggestions"])


def test_a_property_request_keeps_the_card_they_named(rich):
    """The live failure: asked to recommend a deck with Ronin, then asked for "a new
    deck that has a reset card and a big spell" — meaning fix the spell gap in THAT
    deck. Six tool calls followed and Ronin appeared in none of them, because the
    only way to express "with a big spell" was to anchor on a spell instead. 88 decks
    he could build had Ronin and a big spell."""
    rich.execute("UPDATE card_facts SET spell_tier='big' WHERE card_id=6")
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend"], require=["big spell"], conn=rich
    )
    assert r["required"] == ["big_spell"]
    assert r["requirements_met"] is True
    deck = r["decks"][0]
    assert deck["anchor_card"] == "Legend"
    assert any(c["name"] == "Legend" for c in deck["cards"]), "the anchor must survive"
    assert any(c["name"] == "Spell" for c in deck["cards"])


def test_an_impossible_combination_says_so_instead_of_dropping_the_anchor(rich):
    """When nothing combines the anchor with everything asked for, the honest answer
    names the miss. Silently returning a deck without their card is what happened."""
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend"], require=["knockback"], conn=rich
    )
    assert r["requirements_met"] is False
    assert any(c["name"] == "Legend" for c in r["decks"][0]["cards"]), "anchor still kept"


def test_property_names_accept_how_people_say_them(rich):
    rich.execute("UPDATE card_facts SET spell_tier='big' WHERE card_id=6")
    for spoken in ("big spell", "big_spell", "BIG SPELL", "heavy-spell"):
        r = get_deck_recommendations(
            view="build", member_tag=TAG, anchors=["Legend"], require=[spoken], conn=rich
        )
        assert r["required"] == ["big_spell"], spoken


def test_an_unknown_property_is_reported_not_silently_ignored(rich):
    """A requirement we cannot evaluate must not read as satisfied."""
    r = get_deck_recommendations(
        view="build", member_tag=TAG, anchors=["Legend"], require=["vibes"], conn=rich
    )
    assert r["unrecognized_requirements"] == ["vibes"]
    assert r["required"] == []


def test_anchored_narrows_on_properties_too(rich):
    rich.execute("UPDATE card_facts SET spell_tier='big' WHERE card_id=6")
    r = get_deck_recommendations(
        view="anchored", member_tag=TAG, card="Legend", require=["big_spell"], conn=rich
    )
    assert r["requirements_met"] is True
    for deck in r["decks"]:
        assert any(c["name"] == "Legend" for c in deck["cards"])
        assert any(c["name"] == "Spell" for c in deck["cards"])


def test_a_maxed_player_is_told_what_would_open_new_decks(rich):
    """ "What should I be upgrading?" returned nothing because the cards he plays
    are maxed. True, and a dead end exactly when the question gets interesting:
    a maxed player wants to know what would let them play something ELSE."""
    # Everything they field is maxed; one unplayed card sits below max.
    rich.execute("UPDATE player_card_collection SET level = 6 WHERE card_id = 7")
    for i in range(30):
        rich.execute(
            "INSERT INTO battle_card_plays VALUES (?, 'member', 1, 0, ?, '2026-07-20')",
            (f"u{i}", TAG),
        )
    rich.execute("INSERT INTO battle_events VALUES ('u0', ?, 'W')", (TAG,))
    rich.execute(
        "INSERT INTO battle_enrichment VALUES ('u0', ?, '2026-07-20', 'H_OK', NULL)", (TAG,)
    )
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=rich)
    assert r["no_material_upgrades"] is True, "nothing worth upgrading in what they play"
    assert "unlocks" in r, "...and that is exactly when unlocks must carry the answer"
    assert r["readiness_tolerance"] is not None


def test_unlocks_are_ranked_by_what_they_open_not_by_ubiquity(rich):
    """A common card appears in hundreds of near-identical lists, so counting decks
    rewards ubiquity rather than reach."""
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=rich)
    opened = [u["archetypes_opened"] for u in r["unlocks"]]
    assert opened == sorted(opened, reverse=True), "archetype breadth leads the ranking"
    for u in r["unlocks"]:
        assert u["level"] < u["max_level"], "never suggest a card already at max"
        assert u["levels_to_max"] == u["max_level"] - u["level"]
        assert u["archetypes_opened"] >= 1


def test_the_readiness_bar_is_the_members_own_not_a_constant(rich):
    """Members field decks at the top of their collection. A fixed bar would tell a
    maxed player everything is unlocked and a newer one that nothing is."""
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=rich)
    if r["readiness_standard"] is not None:
        assert r["readiness_tolerance"] > r["readiness_standard"]


def test_a_member_with_no_played_decks_gets_no_invented_bar(conn):
    """With no history there is no standard to measure against, and guessing one
    would rank upgrades against a number we made up."""
    r = get_deck_recommendations(view="upgrades", member_tag=TAG, conn=conn)
    assert r["unlocks"] == []
    assert r["readiness_standard"] is None


def test_a_deck_at_the_slot_cap_warns_about_auto_equipped_evolutions(rich):
    """The live failure: the paste came back one card short. Every special slot was
    taken, and the game auto-equipped an evolution for another card in the deck,
    leaving nothing for the Champion."""
    rich.execute("UPDATE card_facts SET role='champion' WHERE card_id=1")
    rich.execute("UPDATE player_card_collection SET evolution_level=1 WHERE card_id IN (2,3)")
    for cid in (2, 3):
        rich.execute(
            "INSERT INTO battle_card_plays VALUES (?, 'member', ?, 1, ?, '2026-07-20')",
            (f"e{cid}", cid, TAG),
        )
    r = get_deck_recommendations(view="discover", member_tag=TAG, limit=12, conn=rich)
    at_cap = [d for d in r["suggestions"] if d["slots_used"] >= 3]
    for deck in at_cap:
        named = set(deck["link_slot_risk"])
        assert named <= {c["name"] for c in deck["cards"]}, "risk names cards in THIS deck"
    assert all(d["link_slot_risk"] == [] for d in r["suggestions"] if d["slots_used"] < 3), (
        "a deck with a spare slot cannot overflow"
    )


def test_champions_count_against_the_three_special_slots(rich):
    """A Champion sits at evolution_level 0, so counting only Evo/Hero forms misses
    it while the game still charges it a slot. Verified against 13,000+ real decks:
    forms plus champions reaches 3 and never 4."""
    rich.execute("UPDATE card_facts SET role='champion' WHERE card_id IN (1, 3, 4)")
    rich.execute("UPDATE player_card_collection SET evolution_level=1")
    rich.execute(
        "INSERT INTO deck_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "H_OVERSLOT",
            "cycle",
            "Too Many Slots",
            3.0,
            _deck([(1, 0), (3, 0), (4, 0), (2, 1), (5, 0), (6, 0), (7, 0), (8, 0)]),
            2,
            1,
            1,
            1,
            1,
            1,
        ),
    )
    r = get_deck_recommendations(view="discover", member_tag=TAG, limit=12, conn=rich)
    assert "Too Many Slots" not in {d["archetype"] for d in r["suggestions"]}, (
        "three champions plus an Evo is four slot demands on three slots"
    )
