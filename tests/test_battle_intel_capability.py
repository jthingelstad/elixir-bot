"""Tests for the get_battle_intelligence capability views + n>=30 claim floor."""

import sqlite3

from capabilities.battle_intel import get_battle_intelligence
from db.schema import build_database


def _db(tmp_path):
    path = tmp_path / "t.db"
    build_database(str(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # a catalog card to resolve by name
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, card_type, synced_at, first_seen_at) "
        "VALUES (26000059, 'Royal Hogs', 'troop', 'x', 'x')"
    )
    return conn


def _plays(conn, n, side, wins, *, evo=None):
    for i in range(n):
        conn.execute(
            "INSERT OR IGNORE INTO battle_card_plays "
            "(battle_dedup_key, side, card_id, evolution_level, player_tag, battle_time, "
            " outcome, is_competitive) VALUES (?, ?, 26000059, ?, '#M', ?, ?, 1)",
            (
                f"{side}-{evo}-{i}",
                side,
                evo,
                f"2026-07-20T00:00:{i:02d}Z",
                "W" if i < wins else "L",
            ),
        )
    conn.commit()


def test_card_view_applies_n30_floor(tmp_path):
    conn = _db(tmp_path)
    _plays(conn, 40, "member", 24)  # 60% over n=40 -> reported
    _plays(conn, 10, "opponent", 3)  # n=10 -> insufficient
    r = get_battle_intelligence(view="card", member_tag="#M", card="Royal Hogs", conn=conn)
    playing = {e["card"]: e for e in r["playing"]}
    facing = {e["card"]: e for e in r["facing"]}
    assert playing["Royal Hogs"]["win_rate"] == 0.6
    assert playing["Royal Hogs"]["insufficient_sample"] is False
    assert facing["Royal Hogs"]["win_rate"] is None
    assert facing["Royal Hogs"]["insufficient_sample"] is True


def test_card_view_is_form_aware(tmp_path):
    conn = _db(tmp_path)
    _plays(conn, 30, "member", 15, evo=None)  # base
    _plays(conn, 30, "member", 27, evo=1)  # Evo — distinct card, different rate
    r = get_battle_intelligence(view="card", member_tag="#M", card="Royal Hogs", conn=conn)
    labels = {e["card"]: e["win_rate"] for e in r["playing"]}
    assert labels["Royal Hogs"] == 0.5
    assert labels["Evo Royal Hogs"] == 0.9


def test_unknown_card_and_missing_tag(tmp_path):
    conn = _db(tmp_path)
    assert (
        get_battle_intelligence(view="card", card="Nonexistent", conn=conn)["error"]
        == "unknown_card"
    )
    assert get_battle_intelligence(view="battle", conn=conn)["error"] == "member_tag_required"


def test_unsupported_view(tmp_path):
    conn = _db(tmp_path)
    assert get_battle_intelligence(view="bogus", conn=conn)["error"] == "unsupported_view"


def test_newcomer_cards_maxed_is_rarity_aware(tmp_path):
    """Card levels are rarity-relative (legendary maxes at 8, common at 16). The old
    `level >= 14` test was wrong BOTH ways: blind to maxed epics/legendaries (33 of one
    member's 72 maxed cards were invisible) and counting a common at 14/16 as maxed
    (13 reported where 10 were real). This is the first-impression welcome surface."""
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO card_catalog (card_id, name, card_type, max_level, synced_at, "
        "first_seen_at) VALUES (901, 'Leg', 'troop', 8, 'x', 'x'), "
        "(902, 'Com', 'troop', 16, 'x', 'x'), (903, 'Epi', 'troop', 11, 'x', 'x')"
    )
    conn.executemany(
        "INSERT INTO player_card_collection (player_tag, card_id, level, observed_at) "
        "VALUES (?, ?, ?, 'x')",
        [("#M", 901, 8), ("#M", 902, 14), ("#M", 903, 11)],
    )
    conn.execute(
        "INSERT INTO player_current_state (player_tag, trophies, best_trophies, "
        "observed_at) VALUES ('#M', 6000, 6100, 'x')"
    )
    r = get_battle_intelligence(view="newcomer", member_tag="#M", conn=conn)
    # maxed legendary (8/8) + maxed epic (11/11); the common at 14/16 is NOT maxed
    assert r["cards_maxed"] == 2
    assert "cards_at_14_plus" not in r
    conn.close()


def test_nemesis_flags_a_worst_card_that_is_still_a_winning_matchup(tmp_path):
    """ "Worst" is a ranking, not a verdict. This view reported a 58.3% card as a nemesis;
    only the model's own scepticism kept it out of an answer."""
    conn = _db(tmp_path)
    _plays(conn, 40, "opponent", 30)  # member wins 30 of 40 while FACING the card
    r = get_battle_intelligence(view="nemesis", member_tag="#M", conn=conn)
    assert r["nemeses"], "expected the card to clear the n>=30 floor"
    assert r["nemeses"][0]["losing_matchup"] is False
    assert r["any_losing_matchup"] is False
    assert r["cards_evaluated"] == 1
    conn.close()


def test_nemesis_separates_no_weaknesses_from_no_evidence(tmp_path):
    """An empty nemeses list has two opposite meanings and the caller cannot tell
    them apart without cards_evaluated.

    A member with 40 lifetime battles can never put a card over n=30, so
    any_losing_matchup=false was reading as "you have no weaknesses" -- a
    compliment earned by playing too little. cards_evaluated=0 is the tell.
    """
    conn = _db(tmp_path)
    _plays(conn, 20, "opponent", 8)  # 20 battles: nothing can clear the n>=30 floor
    r = get_battle_intelligence(view="nemesis", member_tag="#M", conn=conn)
    assert r["nemeses"] == []
    assert r["cards_evaluated"] == 0, "no card was judged, so nothing may be concluded"
    assert r["any_losing_matchup"] is False
    assert r["sample_floor"] == 30
    assert "cards_evaluated FIRST" in r["note"]
    conn.close()


def test_coaching_omits_factors_that_do_not_predict_outcome():
    """air_matchup/wincon_pressure are flat vs outcome across 12,687 clan battles, and
    their lopsided tallies read as findings ("wincon countered in 118 of 135 battles")."""
    import inspect

    from capabilities import battle_intel

    src = inspect.getsource(battle_intel._coaching_view)
    assert "air_matchups=" not in src
    assert "wincon_pressure=" not in src
