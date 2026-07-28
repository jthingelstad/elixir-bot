"""The opponent's deck must survive ingest (#216).

`battle_events` recorded `opponent_tag` but not the opponent's cards, so the
question Elixir most wants to answer about a loss -- "what are you losing TO?"
-- had no data behind it. `get_member_recent_losses` documented itself as
aggregating opponent cards and took a `top_cards` argument it never used.

This is not recoverable after the fact: a tag can be re-scouted, but the deck an
opponent brought to one specific battle exists only in that battle log entry.
Losing it again would be silent -- the reader returned a confident-looking
result with an empty card list -- so the capture is pinned here at the source.
"""

from __future__ import annotations

import json

from engine.ingest import extract_battles

_OPPONENT_CARDS = [
    {"id": 26000000, "name": "Knight", "level": 14},
    {"id": 26000001, "name": "Archers", "level": 13},
]
_TEAM_CARDS = [{"id": 28000000, "name": "Fireball", "level": 14}]


def _battle(**over) -> dict:
    battle = {
        "battleTime": "20260728T120000.000Z",
        "type": "PvP",
        "team": [{"tag": "#ME", "crowns": 1, "cards": _TEAM_CARDS}],
        "opponent": [{"tag": "#OPP", "crowns": 3, "cards": _OPPONENT_CARDS}],
    }
    battle.update(over)
    return battle


def test_ingest_captures_both_decks_not_just_the_polled_player():
    row = extract_battles("#ME", [_battle()])[0]

    assert [c["name"] for c in json.loads(row["deck_json"])] == ["Fireball"]
    assert [c["name"] for c in json.loads(row["opponent_deck_json"])] == ["Knight", "Archers"]


def test_opponent_deck_keeps_id_and_level_for_card_lookups():
    row = extract_battles("#ME", [_battle()])[0]

    assert json.loads(row["opponent_deck_json"])[0] == {
        "id": 26000000,
        "name": "Knight",
        "level": 14,
    }


def test_opponent_deck_is_written_to_battle_events():
    """Guards the column list, not just the extractor: a field that never
    reaches _INSERT_COLUMNS is silently dropped between the two."""
    from engine.ingest import _INSERT_COLUMNS

    assert "opponent_deck_json" in _INSERT_COLUMNS


def test_missing_opponent_cards_degrade_to_null_not_an_exception():
    """Boat battles and PvE have no opponent deck at all."""
    row = extract_battles("#ME", [_battle(opponent=[{"tag": "#OPP", "crowns": 0}])])[0]

    assert row["opponent_deck_json"] is None


def test_polled_player_listed_second_still_attributes_decks_correctly():
    """CR may order a 2v2 team with the polled player second. The opponent deck
    must not become the teammate's."""
    row = extract_battles(
        "#ME",
        [
            _battle(
                team=[
                    {"tag": "#BUDDY", "crowns": 1, "cards": [{"id": 1, "name": "Golem"}]},
                    {"tag": "#ME", "crowns": 1, "cards": _TEAM_CARDS},
                ]
            )
        ],
    )[0]

    assert [c["name"] for c in json.loads(row["deck_json"])] == ["Fireball"]
    assert [c["name"] for c in json.loads(row["opponent_deck_json"])] == ["Knight", "Archers"]


def test_losses_faced_counts_battles_not_card_copies():
    """War duels store 2-3 sub-decks under a single battle, so the same card can
    appear twice for ONE loss. `losses_faced` is documented as a battle count,
    so it must dedupe within a battle."""
    import storage.player as player

    duel_deck = json.dumps(
        [
            {"id": 1, "name": "Knight", "level": 14},
            {"id": 2, "name": "Archers", "level": 14},
            # second sub-deck of the same duel, re-using Knight
            {"id": 1, "name": "Knight", "level": 14},
            {"id": 3, "name": "Zap", "level": 14},
        ]
    )

    seen = player._deck_card_modes(duel_deck)
    assert seen.count(("Knight", None)) == 2, "fixture must contain the duplicate"
    assert len(set(seen)) == 3, "deduped, a duel counts Knight once per battle"


# ── v17: the whole battle record ─────────────────────────────────────────────

_FULL_BATTLE = {
    "battleTime": "20260728T120000.000Z",
    "type": "PvP",
    "deckSelection": "collection",
    "modifiers": [{"tag": "#CLAN", "modifiers": ["Knight3"]}],
    "team": [
        {
            "tag": "#ME",
            "name": "Me",
            "crowns": 1,
            "cards": _TEAM_CARDS,
            "supportCards": [{"id": 159000000, "name": "Tower Princess", "level": 14}],
            "elixirLeaked": 4.25,
            "kingTowerHitPoints": 2400,
            "princessTowersHitPoints": [1200, 0],
            "globalRank": 204,
            "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "badgeId": 16000107},
        }
    ],
    "opponent": [
        {
            "tag": "#OPP",
            "name": "Rival",
            "crowns": 3,
            "cards": _OPPONENT_CARDS,
            "supportCards": [{"id": 159000003, "name": "Dagger Duchess", "level": 12}],
            "elixirLeaked": 1.5,
            "kingTowerHitPoints": 0,
            "princessTowersHitPoints": [3052],
            "startingTrophies": 9100,
            "trophyChange": 30,
            "clan": {"tag": "#RIVAL", "name": "Rivals", "badgeId": 16000001},
        }
    ],
}


def test_every_captured_battle_field_round_trips():
    """One assertion per fact the audit found being dropped. Each was parsed
    into scope and discarded; a regression here is silent, because the reader
    returns a confident empty answer rather than failing."""
    row = extract_battles("#ME", [_FULL_BATTLE])[0]

    assert json.loads(row["support_cards_json"])[0]["name"] == "Tower Princess"
    assert json.loads(row["opponent_support_cards_json"])[0]["name"] == "Dagger Duchess"
    assert row["elixir_leaked"] == 4.25
    assert row["opponent_elixir_leaked"] == 1.5
    assert row["king_tower_hp"] == 2400
    assert row["opponent_king_tower_hp"] == 0
    assert json.loads(row["princess_towers_hp_json"]) == [1200, 0]
    assert row["global_rank"] == 204
    assert row["clan_tag"] == "#J2RGCRVG"
    assert row["opponent_name"] == "Rival"
    assert row["opponent_clan_tag"] == "#RIVAL"
    assert row["opponent_clan_name"] == "Rivals"
    assert row["opponent_clan_badge_id"] == 16000001
    assert row["opponent_starting_trophies"] == 9100
    assert row["opponent_trophy_change"] == 30
    assert json.loads(row["modifiers_json"])[0]["modifiers"] == ["Knight3"]


def test_boat_battle_result_fields_are_captured():
    row = extract_battles(
        "#ME",
        [
            {
                **_FULL_BATTLE,
                "type": "boatBattle",
                "boatBattleSide": "attacker",
                "boatBattleWon": True,
                "newTowersDestroyed": 2,
                "prevTowersDestroyed": 1,
                "remainingTowers": 0,
            }
        ],
    )[0]

    assert row["boat_battle_side"] == "attacker"
    assert row["boat_battle_won"] == 1
    assert (row["new_towers_destroyed"], row["prev_towers_destroyed"]) == (2, 1)
    assert row["remaining_towers"] == 0


def test_duel_rounds_keep_each_round_deck_separately():
    """A duel's top-level `cards` is every round concatenated, so the individual
    decks exist ONLY here. Shape matches what war_analytics already expects."""
    row = extract_battles(
        "#ME",
        [
            {
                **_FULL_BATTLE,
                "type": "riverRaceDuel",
                "team": [
                    {
                        **_FULL_BATTLE["team"][0],
                        "rounds": [
                            {"crowns": 1, "elixirLeaked": 2.0, "cards": _TEAM_CARDS},
                            {"crowns": 0, "elixirLeaked": 3.0, "cards": _OPPONENT_CARDS},
                        ],
                    }
                ],
            }
        ],
    )[0]

    rounds = json.loads(row["rounds_json"])
    assert len(rounds) == 2
    assert [c["name"] for c in rounds[0]["cards"]] == ["Fireball"]
    assert [c["name"] for c in rounds[1]["cards"]] == ["Knight", "Archers"]
    assert rounds[1]["elixir_leaked"] == 3.0


def test_every_insert_column_is_also_enriched_on_dedup():
    """A column in the INSERT but not the enrich UPDATE never backfills on a
    re-poll -- the bug that left deck_json NULL on already-seen battles."""
    from engine.ingest import _ENRICH_COLUMNS, _INSERT_COLUMNS

    identity = {
        "dedup_key",
        "player_tag",
        "battle_time",
        "observed_at",
        "battle_type",
        "opponent_tag",
        "crowns_for",
        "crowns_against",
        "game_mode_id",
        "game_mode_name",
        "mode_group",
        "outcome",
        "is_war",
        "is_ladder",
        "is_ranked",
        "is_competitive",
        "is_special_event",
        "trophy_change",
        "starting_trophies",
        "deck_selection",
        "is_hosted_match",
        "tournament_tag",
        "event_tag",
        "season_id",
        "section_index",
        "war_day_index",
    }
    missing = set(_INSERT_COLUMNS) - set(_ENRICH_COLUMNS) - identity
    assert not missing, f"columns that would never backfill on re-poll: {sorted(missing)}"


# ── why the loss happened, not just that it did ──────────────────────────────


def test_surviving_towers_reads_the_api_omission_convention():
    """The API OMITS destroyed princess towers rather than zeroing them, so the
    list length is the survivor count and NULL means both fell. Verified against
    12k battles: 0 crowns conceded -> 2 entries, 1 -> 1."""
    import storage.player as player

    assert player._surviving_towers("[2088,155]") == [2088, 155]
    assert player._surviving_towers("[3052]") == [3052]
    assert player._surviving_towers(None) == []
    assert player._surviving_towers("not json") == []


def _loss(crowns_for, crowns_against, opp_towers, leaked=None, opp_leaked=None):
    return {
        "crowns_for": crowns_for,
        "crowns_against": crowns_against,
        "opponent_princess_towers_hp_json": opp_towers,
        "elixir_leaked": leaked,
        "opponent_elixir_leaked": opp_leaked,
    }


def test_margin_separates_a_winnable_loss_from_a_sweep():
    """Both of these are outcome='L' and nothing before v17 could tell them
    apart: one finished with the opponent's tower at 90 HP, the other was 0-3."""
    import storage.player as player

    margin = player._loss_margin(
        [
            _loss(0, 1, "[3000,90]"),  # near miss — one hit short
            _loss(0, 3, None),  # swept, no towers left standing
            _loss(1, 2, "[2500]"),  # lost by one crown, not close on HP
        ]
    )

    assert margin["one_crown_losses"] == 2  # 0-1 and 1-2, not the 0-3
    assert margin["near_miss_losses"] == 1
    assert margin["closest_tower_hp"] == 90
    assert margin["losses_with_tower_data"] == 2  # the swept loss has no data


def test_margin_reports_no_tower_data_rather_than_zero():
    import storage.player as player

    margin = player._loss_margin([_loss(0, 3, None)])

    assert margin["closest_tower_hp"] is None
    assert margin["losses_with_tower_data"] == 0
    assert margin["near_miss_losses"] == 0


def test_elixir_compares_against_the_same_battle():
    """Comparing to the opponent controls for game length — a long game leaks
    more for both sides, so the absolute number alone would mislead."""
    import storage.player as player

    elixir = player._elixir_discipline(
        [
            _loss(0, 1, None, leaked=10.0, opp_leaked=2.0),
            _loss(0, 1, None, leaked=1.0, opp_leaked=8.0),
            _loss(0, 1, None, leaked=4.0, opp_leaked=None),  # opponent missing
        ]
    )

    assert elixir["leaked_more_than_opponent"] == 1
    assert elixir["battles_compared"] == 2  # the unpaired battle is excluded
    assert elixir["avg_leaked"] == 5.0  # all three count for own average
    assert elixir["avg_opponent_leaked"] == 5.0


def test_elixir_block_declares_that_lower_is_better():
    """Every field in this block is a fault count, not a score. The polarity is
    not inferable from the field names alone, and a model that reads it
    backwards would praise a member for wasting elixir."""
    import storage.player as player

    elixir = player._elixir_discipline([_loss(0, 1, None, leaked=9.0, opp_leaked=1.0)])

    assert elixir["lower_is_better"] is True
    assert elixir["leaked_more_than_opponent"] == 1, "9.0 wasted vs 1.0 is the WORSE player"


# ── wins: the mirror of the losses read ──────────────────────────────────────


def _win(crowns_for, crowns_against, own_towers):
    return {
        "crowns_for": crowns_for,
        "crowns_against": crowns_against,
        "princess_towers_hp_json": own_towers,
    }


def test_win_margin_separates_a_sweep_from_a_coin_flip():
    """'On a 5-game streak' is the same sentence whether those were three-crown
    sweeps or games survived at 30 HP. This is what tells them apart."""
    import storage.player as player

    margin = player._win_margin(
        [
            _win(3, 0, "[3000,2900]"),  # dominant, untouched
            _win(1, 0, "[2800,31]"),  # won, but nearly lost a tower
            _win(2, 1, "[2600]"),  # lost a tower, still won
        ]
    )

    assert margin["three_crown_wins"] == 1
    assert margin["narrow_wins"] == 1
    assert margin["closest_own_tower_hp"] == 31
    # The 31-HP win kept both towers AND was narrow. Both are true; the name
    # says what was measured rather than implying dominance.
    assert margin["no_tower_lost_wins"] == 2


def test_tower_pressure_reads_the_side_actually_under_pressure():
    """A loss measures the OPPONENT's towers (how close to winning); a win
    measures the member's OWN (how close to losing). Same maths, opposite
    column -- so the two reads stay comparable."""
    import storage.player as player

    battles = [{"mine": "[100]", "theirs": "[3000]"}]

    near_mine, low_mine, _ = player._tower_pressure(battles, "mine")
    near_theirs, low_theirs, _ = player._tower_pressure(battles, "theirs")

    assert (near_mine, low_mine) == (1, 100)
    assert (near_theirs, low_theirs) == (0, 3000)


def test_wins_read_labels_beaten_cards_as_a_strength():
    """`beaten_cards` is what the member BEAT. Read as a weakness it inverts
    the advice entirely, so the note must say so and the key must not look
    like the losses read's `top_opponent_cards`."""
    import db

    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        db.snapshot_player_battlelog(
            "#PLAYER",
            [
                {
                    "battleTime": f"20260410T12000{i}.000Z",
                    "type": "PvP",
                    "gameMode": {"id": 1, "name": "test"},
                    "arena": {"id": 1, "name": "Arena"},
                    "deckSelection": "collection",
                    "team": [
                        {
                            "tag": "#PLAYER",
                            "crowns": 3,
                            "cards": [{"id": 1, "name": "Hog Rider", "level": 14}],
                            "princessTowersHitPoints": [3000, 2900],
                        }
                    ],
                    "opponent": [
                        {
                            "tag": "#OPP",
                            "crowns": 0,
                            "cards": [{"id": 2, "name": "Golem", "level": 12}],
                        }
                    ],
                }
                for i in range(3)
            ],
            conn=conn,
        )
        out = db.get_member_recent_wins("#PLAYER", scope="overall_10", conn=conn)

        assert out["wins_examined"] == 3
        assert out["current_win_streak"] == 3
        assert out["beaten_cards"][0] == {
            "name": "Golem",
            "played_as": None,
            "wins_over": 3,
        }
        assert "top_opponent_cards" not in out, "must not collide with the losses read"
        assert out["margin"]["three_crown_wins"] == 3
        assert out["margin"]["no_tower_lost_wins"] == 3
        assert "BEAT" in out["note"]
    finally:
        conn.close()


# ── member report depth ──────────────────────────────────────────────────────


def _rep_battle(outcome, opp_cards, *, leaked=None, own_towers=None, opp_towers=None):
    return {
        "outcome": outcome,
        "opponent_deck_json": json.dumps([{"id": i, "name": n} for i, n in enumerate(opp_cards)]),
        "elixir_leaked": leaked,
        "opponent_elixir_leaked": 3.0,
        "princess_towers_hp_json": own_towers,
        "opponent_princess_towers_hp_json": opp_towers,
        "crowns_for": 1 if outcome == "W" else 0,
        "crowns_against": 0 if outcome == "W" else 1,
        "support_cards_json": json.dumps([{"id": 1, "name": "Dagger Duchess"}]),
    }


def test_report_matchups_give_a_card_a_win_loss_record():
    """The deepest thing the record supports: not "you lost to X" but "you are
    1-4 against it"."""
    from runtime import member_report as mr

    battles = [_rep_battle("L", ["Mega Knight"]) for _ in range(4)]
    battles += [_rep_battle("W", ["Mega Knight"])]
    battles += [_rep_battle("W", ["Goblins"]) for _ in range(5)]

    mu = mr._card_matchups(battles, min_faced=4)
    by_name = {c["name"]: c for c in mu["toughest"] + mu["best"]}

    assert by_name["Mega Knight"]["wins"] == 1
    assert by_name["Mega Knight"]["losses"] == 4
    assert mu["toughest"][0]["name"] == "Mega Knight"
    assert mu["best"][0]["name"] == "Goblins"


def test_report_matchup_floor_scales_with_how_much_they_played():
    """A 0-4 record is a story in a 20-battle week and noise in a 300-battle
    one. This clan spans both in the same week."""
    from runtime import member_report as mr

    assert mr._card_matchups([_rep_battle("W", ["A"])] * 20)["min_faced"] == 4
    assert mr._card_matchups([_rep_battle("W", ["A"])] * 332)["min_faced"] == 8


def test_report_elixir_splits_wins_from_losses():
    """The split is what makes it coaching rather than trivia."""
    from runtime import member_report as mr

    e = mr._elixir_profile(
        [
            _rep_battle("W", ["A"], leaked=2.0),
            _rep_battle("W", ["A"], leaked=4.0),
            _rep_battle("L", ["A"], leaked=8.0),
        ]
    )

    assert e["in_wins"] == 3.0
    assert e["in_losses"] == 8.0
    assert e["loss_minus_win_gap"] == 5.0
    assert e["lower_is_better"] is True


def test_report_depth_brief_states_polarity_in_every_line():
    """The model only sees this brief, so a line that can be read backwards is
    a line that will be. Guards the elixir and matchup framing together."""
    from runtime import member_report as mr

    ctx = {
        "battles": {
            "margin": {
                "narrow_wins": 2,
                "near_miss_losses": 3,
                "threshold_hp": 500,
                "three_crown_wins": 0,
                "no_tower_lost_wins": 0,
                "closest_loss_their_tower_hp": 40,
            },
            "elixir": {
                "avg_leaked": 7.0,
                "avg_opponent_leaked": 3.0,
                "in_wins": 4.0,
                "in_losses": 8.0,
                "loss_minus_win_gap": 4.0,
            },
            "matchups": {
                "toughest": [{"name": "Rascals", "played_as": None, "wins": 2, "losses": 16}],
                "best": [{"name": "Skeletons", "played_as": None, "wins": 9, "losses": 2}],
                "min_faced": 8,
            },
            "tower_troop": "Dagger Duchess",
        }
    }
    brief = "\n".join(mr._depth_brief(ctx))

    assert "LOWER IS BETTER" in brief
    assert "do NOT call these dominant" in brief
    assert "NOT a proven cause" in brief, "elixir gap is a correlation"
    assert "these BEAT them" in brief
    assert "never a weakness" in brief, "best matchups must not read as a problem"
    assert "Dagger Duchess" in brief


# ── sparklines ───────────────────────────────────────────────────────────────


def test_tower_sparkline_marks_destroyed_towers_not_zero_height():
    """A destroyed tower is OMITTED by the API, not zeroed. Rendering it as the
    shortest block would read as 'barely alive' — the exact opposite."""
    from runtime import member_report as mr

    assert mr._tower_spark("[3000,3000]", 3000) == "██"
    assert mr._tower_spark("[3000]", 3000) == "█·", "one fell"
    assert mr._tower_spark(None, 3000) == "··", "both fell"


def test_tower_sparkline_orders_healthiest_first():
    """Stable ordering, so a column of these is scannable — the second glyph is
    always the tower closer to falling."""
    from runtime import member_report as mr

    assert mr._tower_spark("[100,3000]", 3000) == mr._tower_spark("[3000,100]", 3000)
    assert mr._tower_spark("[3000,100]", 3000)[1] < mr._tower_spark("[3000,100]", 3000)[0]


def test_sparkline_scales_are_relative_to_the_member():
    """Neither tower HP nor elixir has a universal max — tower HP scales with
    tower level, and elixir runs median ~3 but reaches 300 in duels. A fixed
    ceiling either flattens normal battles or lets outliers own the scale."""
    from runtime import member_report as mr

    battles = [
        {
            "princess_towers_hp_json": "[2000,1000]",
            "opponent_princess_towers_hp_json": "[3000]",
            "elixir_leaked": float(i),
            "opponent_elixir_leaked": 1.0,
        }
        for i in range(20)
    ]
    scale = mr._battle_scale(battles)

    assert scale["own_full_hp"] == 2000
    assert scale["opponent_full_hp"] == 3000
    assert scale["elixir_ceiling"] >= 6.0, "floor keeps a tidy week from reading as noise"
    assert scale["elixir_ceiling"] < 20, "outliers must not own the scale"


def test_sparkline_clamps_rather_than_indexing_out_of_range():
    """A 300-elixir duel must render as a full bar, not crash the report."""
    from runtime import member_report as mr

    assert mr._spark_char(300, 12) == "█"
    assert mr._spark_char(0, 12) == "▁"
    assert mr._spark_char(5, 0) == "▁", "degenerate ceiling must not divide by zero"
