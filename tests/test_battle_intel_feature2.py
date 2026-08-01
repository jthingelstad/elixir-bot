"""Feature 2 end-to-end: deck_profile (rules), measured matchup matrix,
expected_advantage/performance fill, and the matchup/deck tool views."""

import json
import sqlite3

from capabilities.battle_intel import get_battle_intelligence
from db.schema import build_database
from storage.battle_intel import enrich_battles, rebuild_deck_intel

_IDS = {}  # stable, distinct card id per name (real decks have distinct ids)


def _deck(*names):
    for n in names:
        _IDS.setdefault(n, 1000 + len(_IDS))
    return json.dumps(
        [{"id": _IDS[n], "name": n, "level": 11, "evolution_level": None} for n in names]
    )


HOGS = _deck(
    "Royal Hogs", "Musketeer", "Cannon", "Fireball", "Ice Spirit", "Skeletons", "Log", "Bats"
)
GOLEM = _deck(
    "Golem", "Baby Dragon", "Witch", "Lightning", "Tornado", "Mega Minion", "Skeletons", "Bats"
)


def _battle(conn, key, deck, opp, outcome):
    conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, outcome, "
        "mode_group, is_competitive, is_ranked, deck_json, opponent_deck_json) "
        "VALUES (?, '#M', ?, ?, ?, 'ladder', 1, 0, ?, ?)",
        (key, f"2026-07-20T00:00:{int(key[1:]):02d}Z", "2026-07-20T01:00:00Z", outcome, deck, opp),
    )


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_feature2_end_to_end(tmp_path):
    conn = _db(tmp_path)
    # Both directions, because a matchup rate is only meaningful once the clan's own
    # strength can be cancelled against the opposite cell. HOGS (bridge spam) beats
    # GOLEM (beatdown) 3 of 4; GOLEM loses to HOGS 3 of 4. A genuine +2 matchup.
    for i, oc in enumerate(["W", "W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    for i, oc in enumerate(["L", "L", "L", "W"]):
        _battle(conn, f"c{i}", GOLEM, HOGS, oc)
    conn.commit()

    enrich_battles(100, conn=conn)
    result = rebuild_deck_intel(conn=conn)
    assert result["profiled"] == 2  # HOGS + GOLEM
    assert result["matchup_cells"] == 2  # both directions
    assert result["expected_filled"] == 8

    # deck_profile families
    fams = dict(conn.execute("SELECT archetype, family FROM deck_profile").fetchall())
    assert fams["Royal Hogs Bridge Spam"] == "bridge spam"
    assert fams["Golem Beatdown"] == "beatdown"

    # bridge spam vs beatdown: (0.75 + (1 - 0.25)) / 2 = 0.75 -> advantage +2
    cell = conn.execute(
        "SELECT advantage, measured_win_rate, n, basis FROM matchup_expectation "
        "WHERE our_family='bridge spam' AND their_family='beatdown'"
    ).fetchone()
    assert cell["n"] == 4
    assert cell["measured_win_rate"] == 0.75
    assert cell["advantage"] == 2
    assert "symmetrized" in cell["basis"]
    # ...and the opposite cell is its mirror image, never independently favourable.
    back = conn.execute(
        "SELECT advantage, measured_win_rate FROM matchup_expectation "
        "WHERE our_family='beatdown' AND their_family='bridge spam'"
    ).fetchone()
    assert back["measured_win_rate"] == 0.25
    assert back["advantage"] == -2

    # the loss when favored is an underperformance; the win when disadvantaged an upset
    perf = dict(
        conn.execute(
            "SELECT performance, COUNT(*) FROM battle_enrichment GROUP BY performance"
        ).fetchall()
    )
    assert perf.get(-1) == 1  # the L while favored (bridge spam side)
    assert perf.get(1) == 1  # the W while disadvantaged (beatdown side)
    assert perf.get(0) == 6


def test_a_family_is_never_favoured_against_itself(tmp_path):
    """The invariant that proves the correction. A mirror matchup is 50/50 by
    construction, so any deviation is the clan being better than the opponents it
    meets — not a property of the deck. Anchoring on 0.50 instead of the clan
    baseline stored control-vs-control at 58.4% and banded it +1, and 781 losses in
    mirror matchups were then flagged as underperformance."""
    conn = _db(tmp_path)
    # A lopsided mirror: the clan wins 4 of 5 bridge-spam-vs-bridge-spam games.
    for i, oc in enumerate(["W", "W", "W", "W", "L"]):
        _battle(conn, f"m{i}", HOGS, HOGS, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    cell = conn.execute(
        "SELECT advantage, measured_win_rate FROM matchup_expectation "
        "WHERE our_family='bridge spam' AND their_family='bridge spam'"
    ).fetchone()
    assert cell["measured_win_rate"] == 0.5, "a mirror can never be anything but even"
    assert cell["advantage"] == 0
    # ...so none of those four wins is an upset and the loss is not a failure.
    perf = [r[0] for r in conn.execute("SELECT performance FROM battle_enrichment")]
    assert set(perf) == {0}


def test_both_directions_of_a_matchup_can_never_both_be_favoured(tmp_path):
    """The contradiction that exposed the bug in production: cycle vs control and
    control vs cycle were BOTH stored at +1."""
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    for i, oc in enumerate(["W", "W", "W", "L"]):  # clan wins a lot on BOTH sides
        _battle(conn, f"c{i}", GOLEM, HOGS, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    cells = {
        (r["our_family"], r["their_family"]): r["advantage"]
        for r in conn.execute("SELECT * FROM matchup_expectation")
    }
    fwd = cells[("bridge spam", "beatdown")]
    rev = cells[("beatdown", "bridge spam")]
    assert fwd == rev == 0, "clan strength on both sides is not a matchup edge"
    assert fwd + rev == 0, "advantages must be equal and opposite"


def test_restating_expectations_corrects_a_stale_snapshot(tmp_path):
    """A battle's expected_advantage is a snapshot of a cell that moves. If the
    fill only touched unset rows, a corrected matrix would correct nothing."""
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    for i, oc in enumerate(["L", "L", "L", "W"]):
        _battle(conn, f"c{i}", GOLEM, HOGS, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)
    conn.execute("UPDATE battle_enrichment SET expected_advantage = -99, performance = 1")
    conn.commit()

    rebuild_deck_intel(conn=conn)
    stale = conn.execute(
        "SELECT COUNT(*) FROM battle_enrichment WHERE expected_advantage = -99"
    ).fetchone()[0]
    assert stale == 0


def test_matchup_and_deck_views(tmp_path):
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    m = get_battle_intelligence(view="matchup", our_family="bridge spam", conn=conn)
    assert m["available"] is True
    assert m["cells"][0]["their_family"] == "beatdown"

    d = get_battle_intelligence(view="deck", member_tag="#M", conn=conn)
    assert d["decks"][0]["archetype"] == "Royal Hogs Bridge Spam"
    assert d["decks"][0]["battles"] == 3

    bad = get_battle_intelligence(view="matchup", our_family="nonsense", conn=conn)
    assert bad["error"] == "unknown_family"


def test_deck_profile_cards_json_preserves_card_form(tmp_path):
    """deck_profile identity must match deck_hash's, form included.

    _profile_row read evolution_level off the CATALOG-ENRICHED card, which drops it, so
    every stored deck was base-form. 983 of 1,678 member decks (59%) actually run an Evo
    or Hero. The damage was silent and severe: the Evo ownership gate never fired, so
    get_deck_recommendations offered decks needing Evolutions the member does not own
    (King Thing's buildable count fell 9,046 -> 5,164 once fixed), and deck facts were
    scored against base-form cards.
    """
    import json

    from engine.deck_hash import _identity_pairs
    from storage.battle_intel import _profile_row

    deck = [
        {"id": 26000000 + i, "name": f"C{i}", "elixir_cost": 3, "evolution_level": None}
        for i in range(8)
    ]
    deck[0]["evolution_level"] = 1  # an Evolution
    deck[1]["evolution_level"] = 2  # a Hero
    row = _profile_row(json.dumps(deck), {})
    assert row is not None
    stored = [tuple(p) for p in json.loads(row[4])]
    assert stored == _identity_pairs(deck), "cards_json must match deck_hash identity"
    assert (26000000, 1) in stored, "Evolution form was dropped"
    assert (26000001, 2) in stored, "Hero form was dropped"


def test_coaching_describes_the_deck_the_way_deck_intelligence_does(tmp_path):
    """The seam. "Why do I lose to beatdown?" used to be answered with three bare
    integers while the recommendation views next door named cards and gaps."""
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "L", "L", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    for i, oc in enumerate(["W", "W", "L", "W"]):
        _battle(conn, f"c{i}", GOLEM, HOGS, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    r = get_battle_intelligence(view="coaching", member_tag="#M", conn=conn)
    shape = r["primary_deck_shape"]
    assert shape["archetype"]
    assert shape["family"]
    # Either real coverage, or nothing — never a fabricated critique.
    coverage = shape.get("role_coverage")
    if coverage is not None:
        assert "gaps" in coverage


def test_a_matchup_record_is_the_members_own_not_an_archetype_verdict(tmp_path):
    """Adjusted for who plays which archetype, matchup effects average 3.2 points
    against 22 for card levels — so a record here is a fact about this member and
    their deck, never evidence that an archetype is strong. No expectation is
    attached, because there is no honest one to attach."""
    conn = _db(tmp_path)
    for i in range(20):
        _battle(conn, f"b{i}", HOGS, GOLEM, "W" if i % 4 else "L")
    for i in range(20):
        _battle(conn, f"c{i}", GOLEM, HOGS, "L" if i % 4 else "W")
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    r = get_battle_intelligence(view="coaching", member_tag="#M", limit=100, conn=conn)
    rec = {m["their_family"]: m for m in r["matchup_record"]}
    assert set(rec) == {"beatdown", "bridge spam"}
    for m in rec.values():
        assert m["wins"] + m["losses"] >= _MATCHUP_FLOOR_FOR_TEST
        assert m["enough_games"] is True
        assert "expected_win_rate" not in m, "no archetype expectation may be stated"
        assert "vs_expected" not in m
    assert "upsets" not in r and "underperformances" not in r


def test_a_thin_matchup_is_reported_without_a_grade(tmp_path):
    """Three games against an archetype is not a pattern, and a delta computed off
    it reads as a finding."""
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "L", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    r = get_battle_intelligence(view="coaching", member_tag="#M", conn=conn)
    thin = r["matchup_record"][0]
    assert thin["enough_games"] is False
    assert "structural_notes" not in thin


_MATCHUP_FLOOR_FOR_TEST = 12


def test_no_view_states_an_archetype_matchup_verdict_to_a_member(tmp_path):
    """The finding this whole layer was cut down to respect.

    Player-adjusted (each player's rate in a matchup minus their own baseline), the
    20 family cells with enough players average 3.2 points of lift and top out near
    6. Card levels span 22 points and player skill 34. Siege looked like the one
    real archetype effect at 40.5% into beatdown, then failed to clear a four-player
    floor at all — that number was who plays siege, not siege. Supercell balances
    cards, so a durable archetype edge does not get to persist.

    So: a member's own record against an archetype is fair game, and 'this archetype
    beats that one' is not. If a future change reintroduces an expectation or an
    advantage into a member-facing view, this test is the argument it has to beat.
    """
    conn = _db(tmp_path)
    for i in range(20):
        _battle(conn, f"b{i}", HOGS, GOLEM, "W" if i % 4 else "L")
    for i in range(20):
        _battle(conn, f"c{i}", GOLEM, HOGS, "L" if i % 4 else "W")
    conn.commit()
    enrich_battles(100, conn=conn)
    rebuild_deck_intel(conn=conn)

    coaching = get_battle_intelligence(view="coaching", member_tag="#M", limit=100, conn=conn)
    banned = {"expected_win_rate", "vs_expected", "expected_advantage", "advantage"}
    for entry in coaching["matchup_record"]:
        assert not (banned & set(entry)), f"archetype verdict leaked: {entry}"
    assert banned.isdisjoint(coaching)

    # ...and no battle is explained BY the matchup any more.
    factors = set(coaching["decisive_factors"])
    assert "matchup" not in factors


def test_decisive_factor_no_longer_blames_the_matchup():
    from engine.card_roles import decisive_factor

    assert (
        decisive_factor(
            level_gap=0.0,
            level_ok=True,
            closeness=1,
            discipline_delta=0.0,
            performance=-1,  # would previously have returned "matchup"
            air="stressed",
            wincon="countered",
        )
        == "even_game"
    )
