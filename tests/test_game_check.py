"""Game-knowledge checker golden cases (confidence plan Pillar 3).

Each case is a live-bug class: fabricated/wrong-level card, seasonal-arena
false arena-up, league mismatch, impossible war day. Clean posts return [].
"""

from engine import game_check as g

# ---- facts-only checks (no catalog needed) ----


def test_card_level_mismatch_flags():
    # "maxed to 15" while the facts say the milestone was 16
    out = g.check_post("just maxed Balloon to level 15", {"card_name": "Balloon", "milestone": 16})
    assert any("15" in f["claim"] for f in out) and out[0]["severity"] == "error"


def test_impossible_card_level_flags():
    out = g.check_post("hit level 18", {"card_name": "Balloon", "milestone": 18})
    assert any("impossible" in f["issue"] for f in out)


def test_clean_maxed_card_passes():
    assert (
        g.check_post("just maxed Balloon to level 16", {"card_name": "Balloon", "milestone": 16})
        == []
    )


def test_seasonal_arena_narrated_as_arena_up_flags():
    # PANCAKES! (id above the road-arena ceiling) is a seasonal-league arena
    out = g.check_post("moved up to PANCAKES!", {"arena_id": 55000000, "arena_name": "PANCAKES!"})
    assert any("SEASONAL" in f["issue"] for f in out)


def test_road_arena_up_passes():
    assert (
        g.check_post("moved up to Boot Camp", {"arena_id": 54000010, "arena_name": "Boot Camp"})
        == []
    )


def test_impossible_war_day_flags():
    out = g.check_post("battle day 5 of 4 — get your decks in", {})
    assert any("impossible war day" in f["issue"] for f in out)


def test_clean_war_day_passes():
    assert g.check_post("battle day 3 of 4 is open", {"war_day_human": "battle day 3 of 4"}) == []


def test_colosseum_finish_line_and_no_count_claims_are_rejected():
    facts = {"is_colosseum_week": True, "finish_line": None}
    findings = g.check_post(
        "We crossed the 5,000 finish line, so the remaining battles do not count.",
        facts,
    )
    assert len(findings) == 2
    assert all(finding["severity"] == "error" for finding in findings)


def test_correct_colosseum_mechanics_pass():
    facts = {"is_colosseum_week": True, "finish_line": None}
    assert (
        g.check_post(
            "Colosseum has no finish line; every battle across all four days counts.",
            facts,
        )
        == []
    )
    assert g.check_post("These battles are not purely about personal chest rewards.", facts) == []


def test_league_mismatch_flags():
    # facts say league 7 (Ultimate Champion) but the copy names Champion
    out = g.check_post("reached Champion league!", {"ranked_league": 7})
    assert any("league 7" in f["issue"] for f in out)


def test_clean_league_passes():
    assert g.check_post("reached Ultimate Champion!", {"ranked_league": 7}) == []


# ---- catalog-dependent checks ----


def _seed_card(conn, name, rarity="common", max_level=14):
    conn.execute(
        "INSERT OR REPLACE INTO card_catalog (card_id, name, elixir_cost, rarity, "
        "max_level, card_type, synced_at) VALUES (?, ?, 3, ?, ?, 'troop', '2026-07-05T00:00:00Z')",
        (abs(hash(name)) % 1_000_000, name, rarity, max_level),
    )
    conn.commit()


def test_fabricated_card_name_flags(engine_conn):
    _seed_card(engine_conn, "Balloon")  # a real card exists → catalog is "populated"
    out = g.check_post(
        "maxed Zorptron to 16", {"card_name": "Zorptron", "milestone": 16}, engine_conn
    )
    assert any("not a card in the catalog" in f["issue"] for f in out)


def test_real_card_passes_catalog(engine_conn):
    _seed_card(engine_conn, "Ice Spirit")
    assert (
        g.check_post(
            "Ice Spirit to 16",
            {"card_name": "Ice Spirit", "milestone": 16},
            engine_conn,
        )
        == []
    )


def test_empty_catalog_skips_card_name_check(engine_conn):
    # no cards seeded → cannot judge names → no false-positive
    assert (
        g.check_post(
            "maxed Whatever to 16",
            {"card_name": "Whatever", "milestone": 16},
            engine_conn,
        )
        == []
    )
