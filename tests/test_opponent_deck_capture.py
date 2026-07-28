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
