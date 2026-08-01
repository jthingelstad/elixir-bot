"""Feature 2 end-to-end: deck profiles, derived facts, and battle-intel views."""

import asyncio
import json
import sqlite3
from unittest.mock import patch

from capabilities.battle_intel import get_battle_intelligence
from db.schema import build_database
from runtime.jobs._battle_intel import _battle_intel_stage_b
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


def test_stage_b_is_deck_profiling_only(tmp_path):
    """The family matchup matrix Stage B used to rebuild was dropped in v32:
    adjusted for who plays which archetype it measured ~3 points, against 22 for
    card levels."""
    conn = _db(tmp_path)
    for i, oc in enumerate(["W", "W", "W", "L"]):
        _battle(conn, f"b{i}", HOGS, GOLEM, oc)
    conn.commit()
    enrich_battles(100, conn=conn)

    assert rebuild_deck_intel(conn=conn) == {"profiled": 2}
    fams = dict(conn.execute("SELECT archetype, family FROM deck_profile").fetchall())
    assert fams["Royal Hogs Bridge Spam"] == "bridge spam"
    assert fams["Golem Beatdown"] == "beatdown"

    d = get_battle_intelligence(view="deck", member_tag="#M", conn=conn)
    assert d["decks"][0]["archetype"] == "Royal Hogs Bridge Spam"


def test_scheduled_stage_b_accepts_deck_profile_only_result():
    """The hourly wrapper must match rebuild_deck_intel's post-matrix contract."""
    with (
        patch(
            "runtime.jobs._battle_intel.battle_intel.rebuild_deck_intel",
            return_value={"profiled": 2},
        ) as mock_profiles,
        patch(
            "runtime.jobs._battle_intel.battle_intel.rebuild_interpreted",
            return_value={"deck_facts": 3, "battle_tags": 4},
        ) as mock_interpreted,
        patch("runtime.jobs._battle_intel.runtime_status.mark_job_start") as mock_start,
        patch("runtime.jobs._battle_intel.runtime_status.mark_job_success") as mock_success,
        patch("runtime.jobs._battle_intel.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(_battle_intel_stage_b())

    mock_profiles.assert_called_once_with()
    mock_interpreted.assert_called_once_with()
    mock_start.assert_called_once_with("battle_intel_stage_b")
    mock_success.assert_called_once_with(
        "battle_intel_stage_b",
        "profiled +2, deck_facts +3, battle_tags +4",
    )
    mock_failure.assert_not_called()


def test_the_matchup_matrix_is_gone_for_good(tmp_path):
    """Table and view both. Named here so a reintroduction has to argue with the
    measurement rather than quietly re-land."""
    conn = _db(tmp_path)
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'matchup_expectation'"
    ).fetchone()
    assert get_battle_intelligence(view="matchup", member_tag="#M", conn=conn)["error"] == (
        "unsupported_view"
    )


def test_the_retired_prose_layer_left_nothing_behind(tmp_path):
    """Feature 3's job, feature flag and grading script were removed long before its
    schema was. It outlived them holding 97 rows of commentary out of 13,348, while
    coaching_note and verdict never held a single row in their entire lifetime."""
    conn = _db(tmp_path)
    _battle(conn, "b0", HOGS, GOLEM, "W")
    conn.commit()
    enrich_battles(100, conn=conn)

    columns = {r[1] for r in conn.execute("PRAGMA table_info(battle_enrichment)")}
    assert columns.isdisjoint(
        {
            "commentary",
            "coaching_note",
            "verdict",
            "loss_nature",
            "notable",
            "confidence",
            "model",
            "prompt_version",
            "input_hash",
        }
    )
    assert columns.isdisjoint({"expected_advantage", "performance"})
    assert columns.isdisjoint({"air_matchup", "wincon_pressure", "spell_bait_exposed"})

    battle = get_battle_intelligence(view="battle", member_tag="#M", conn=conn)["battles"][0]
    assert not ({"commentary", "vs_expectation", "expected_advantage", "notable"} & set(battle))


def test_decisive_factor_cannot_be_handed_a_matchup_at_all():
    """Stronger than checking a return value: the parameters are gone, so a caller
    reintroducing archetype or air coverage as a cause fails loudly."""
    import inspect

    from engine.card_roles import decisive_factor

    assert set(inspect.signature(decisive_factor).parameters) == {
        "level_gap",
        "level_ok",
        "closeness",
        "discipline_delta",
    }
