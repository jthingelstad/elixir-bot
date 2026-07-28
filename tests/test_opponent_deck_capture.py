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
    assert elixir["losses_compared"] == 2  # the unpaired battle is excluded
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
