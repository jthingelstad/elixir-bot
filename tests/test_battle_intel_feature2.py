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
    # HOGS (bridge spam) beats GOLEM (beatdown) 3 of 4 -> our advantage
    for i, oc in enumerate(["W", "W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    conn.commit()

    enrich_battles(100, conn=conn)
    result = rebuild_deck_intel(conn=conn)
    assert result["profiled"] == 2  # HOGS + GOLEM
    assert result["matchup_cells"] == 1  # one family pair observed
    assert result["expected_filled"] == 4

    # deck_profile families
    fams = dict(conn.execute("SELECT archetype, family FROM deck_profile").fetchall())
    assert fams["Royal Hogs Bridge Spam"] == "bridge spam"
    assert fams["Golem Beatdown"] == "beatdown"

    # matchup cell: bridge spam vs beatdown, 75% -> advantage +2
    cell = conn.execute(
        "SELECT advantage, measured_win_rate, n FROM matchup_expectation "
        "WHERE our_family='bridge spam' AND their_family='beatdown'"
    ).fetchone()
    assert cell["n"] == 4
    assert cell["measured_win_rate"] == 0.75
    assert cell["advantage"] == 2

    # the loss (advantaged but lost) is an underperformance; wins are as-expected
    perf = dict(
        conn.execute(
            "SELECT performance, COUNT(*) FROM battle_enrichment GROUP BY performance"
        ).fetchall()
    )
    assert perf.get(-1) == 1  # the L when favored
    assert perf.get(0) == 3


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
