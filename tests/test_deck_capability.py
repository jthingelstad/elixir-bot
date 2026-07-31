from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import elixir_agent
from capabilities.decks import get_deck_intelligence
from storage.cards import list_deck_battle_history


def _cards(names):
    costs = {
        "Hog Rider": 4,
        "Musketeer": 4,
        "Cannon": 3,
        "Fireball": 4,
        "The Log": 2,
        "Ice Golem": 2,
        "Skeletons": 1,
        "Ice Spirit": 1,
        "Earthquake": 3,
        "Golem": 8,
        "Night Witch": 4,
        "Baby Dragon": 4,
        "Tornado": 3,
        "Lightning": 6,
        "Lumberjack": 4,
        "Mega Minion": 3,
    }
    card_ids = {name: 1000 + index for index, name in enumerate(sorted(costs))}
    return [
        {"id": card_ids[name], "name": name, "elixir_cost": costs[name], "level": 15}
        for name in names
    ]


HOG = _cards(
    [
        "Hog Rider",
        "Musketeer",
        "Cannon",
        "Fireball",
        "The Log",
        "Ice Golem",
        "Skeletons",
        "Ice Spirit",
    ]
)
HOG_EQ = _cards(
    [
        "Hog Rider",
        "Musketeer",
        "Cannon",
        "Earthquake",
        "The Log",
        "Ice Golem",
        "Skeletons",
        "Ice Spirit",
    ]
)
GOLEM = _cards(
    [
        "Golem",
        "Night Witch",
        "Baby Dragon",
        "Tornado",
        "Lightning",
        "Lumberjack",
        "Mega Minion",
        "Skeletons",
    ]
)


def _row(tag, at, cards, outcome="W"):
    return {
        "player_tag": tag,
        "player_name": "Gem" if tag == "#GEM" else "Stone",
        "battle_time": at,
        "outcome": outcome,
        "trophy_change": 30 if outcome == "W" else -30,
        "mode_group": "ladder",
        "cards": cards,
    }


class _Source:
    def __init__(self, rows):
        self.rows = rows

    def list_deck_battle_history(self, tag=None, **_kwargs):
        return [row for row in self.rows if tag is None or row["player_tag"] == tag]

    def get_member_current_deck(self, _tag, **_kwargs):
        return {"fetched_at": "2026-07-15T12:00:00Z", "cards": HOG_EQ}

    def lookup_member_cards(self, _tag, **_kwargs):
        return {
            "cards": [
                {
                    "name": "Cannon",
                    "level": 13,
                    "king_tower_gap": 2,
                    "levels_to_max": 3,
                },
                {
                    "name": "Hog Rider",
                    "level": 15,
                    "king_tower_gap": 0,
                    "levels_to_max": 1,
                },
            ]
        }


def test_member_deck_intelligence_explains_primary_variant_and_limits():
    rows = [
        _row("#GEM", "20260715T120000.000Z", HOG_EQ),
        _row("#GEM", "20260715T110000.000Z", HOG),
        _row("#GEM", "20260715T100000.000Z", HOG, "L"),
        _row("#GEM", "20260715T090000.000Z", HOG),
    ]

    result = get_deck_intelligence(
        view="member", player_tag="#GEM", scope="ladder", source=_Source(rows)
    )

    assert result["available"] is True
    assert result["primary_deck"]["battles"] == 3
    assert result["primary_deck"]["archetype"]["label"] == "Hog Cycle"
    assert result["primary_deck"]["archetype"]["family"] == "cycle"
    assert result["primary_deck"]["archetype"]["win_conditions"] == ["Hog Rider"]
    assert result["stability"] == {
        "label": "stable",
        "primary_deck_share": 0.75,
        "distinct_decks": 2,
    }
    assert result["variants"][0]["added"] == ["Earthquake"]
    assert result["variants"][0]["removed"] == ["Fireball"]
    assert result["recent_change"]["interpretation"].startswith("observed association")
    assert result["upgrade_bottlenecks"][0]["name"] == "Cannon"
    assert result["evidence_limits"]["opponent_decks_captured"] is True


def test_war_view_does_not_mislabel_profile_deck_as_current_war_deck():
    rows = [_row("#GEM", "20260715T120000.000Z", HOG)]

    result = get_deck_intelligence(
        view="member", player_tag="#GEM", scope="war", source=_Source(rows)
    )

    assert result["current_deck"] is None
    assert "Trophy Road" in result["current_deck_note"]


def test_clan_and_card_impact_views_are_explicitly_local():
    rows = [
        _row("#GEM", "20260715T120000.000Z", HOG),
        _row("#STONE", "20260715T110000.000Z", GOLEM, "L"),
    ]
    source = _Source(rows)

    clan = get_deck_intelligence(view="clan", source=source)
    impact = get_deck_intelligence(view="card_impact", cards=["Hog Rider"], source=source)

    assert {item["archetype"] for item in clan["archetype_spread"]} == {
        "cycle",
        "beatdown",
    }
    assert clan["evidence_limits"]["opponent_decks_captured"] is True
    assert impact["affected_member_count"] == 1
    assert impact["affected_members"][0]["player_tag"] == "#GEM"
    assert impact["changes"][0]["source_state"] == "unknown"
    assert impact["evidence_limits"]["global_meta_claims_supported"] is False


def test_balance_impact_preserves_sources_staleness_and_member_filter():
    rows = [
        _row("#GEM", "20260715T120000.000Z", HOG),
        _row("#STONE", "20260715T110000.000Z", GOLEM, "L"),
    ]
    changes = [
        {
            "card": "Hog Rider",
            "direction": "nerf",
            "status": "wip",
            "source_url": "https://royaleapi.com/hog-wip",
            "published_at": "2026-07-10T00:00:00Z",
            "effective_at": "2026-08-01",
        },
        {
            "card": "Executioner",
            "direction": "buff",
            "status": "superseded",
            "source_url": "https://royaleapi.com/old-executioner",
            "published_at": "2025-01-01T00:00:00Z",
        },
    ]

    result = get_deck_intelligence(
        view="card_impact",
        player_tag="#GEM",
        changes=changes,
        source=_Source(rows),
        as_of=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    assert result["player_tag"] == "#GEM"
    assert result["affected_member_count"] == 1
    assert result["affected_members"][0]["matching_changes"][0]["direction"] == "nerf"
    assert result["changes"][0]["source_state"] == "wip"
    assert result["changes"][0]["source_url"] == "https://royaleapi.com/hog-wip"
    assert result["changes"][1]["source_state"] == "stale"
    assert result["changes"][1]["stale"] is True
    assert result["changes_without_member_evidence"][0]["card"] == "Executioner"


def test_balance_impact_includes_current_profile_deck_without_battle_history():
    class CurrentDeckSource(_Source):
        def list_current_member_decks(self, tag=None, **_kwargs):
            decks = [
                {
                    "player_tag": "#GEM",
                    "player_name": "Gem",
                    "observed_at": "2026-07-15T12:00:00Z",
                    "cards": HOG,
                }
            ]
            return [deck for deck in decks if tag is None or deck["player_tag"] == tag]

    result = get_deck_intelligence(
        view="card_impact",
        player_tag="#GEM",
        changes=[
            {
                "card": "Hog Rider",
                "direction": "nerf",
                "status": "wip",
                "source_url": "https://royaleapi.com/hog-wip",
                "published_at": "2026-07-10T00:00:00Z",
            }
        ],
        source=CurrentDeckSource([]),
    )

    assert result["window"]["sample_battles"] == 0
    assert result["affected_member_count"] == 1
    member = result["affected_members"][0]
    assert member["evidence_types"] == ["current_profile_deck"]
    assert member["current_deck_observed_at"] == "2026-07-15T12:00:00Z"
    assert member["win_rate"] is None


def test_deck_tool_routes_through_shared_capability():
    payload = {"capability": "deck_intelligence", "view": "member", "available": True}
    with (
        patch("agent.tool_exec._refresh_member_cache") as refresh,
        patch(
            "agent.tool_exec.deck_capability.get_deck_intelligence",
            return_value=payload,
        ) as capability,
        patch("agent.tool_exec._annotate_roster_status"),
    ):
        result = json.loads(
            elixir_agent._execute_tool(
                "get_deck_intelligence",
                {"view": "member", "member_tag": "#GEM", "scope": "ladder"},
            )
        )

    assert result["capability"] == "deck_intelligence"
    refresh.assert_called_once_with("#GEM", include_battles=True)
    capability.assert_called_once()
    assert capability.call_args.kwargs["source"] is not None


def test_balance_impact_tool_is_leadership_gated():
    public = json.loads(
        elixir_agent._execute_tool(
            "get_deck_intelligence",
            {"view": "card_impact", "cards": ["Hog Rider"]},
            workflow="interactive",
        )
    )
    assert "leadership channels" in public["error"]

    payload = {"capability": "deck_intelligence", "view": "card_impact"}
    with patch(
        "agent.tool_exec.deck_capability.get_deck_intelligence",
        return_value=payload,
    ) as capability:
        result = json.loads(
            elixir_agent._execute_tool(
                "get_deck_intelligence",
                {
                    "view": "card_impact",
                    "changes": [
                        {
                            "card": "Hog Rider",
                            "direction": "nerf",
                            "status": "wip",
                            "source_url": "https://royaleapi.com/hog",
                            "published_at": "2026-07-10T00:00:00Z",
                        }
                    ],
                },
                workflow="clanops",
            )
        )

    assert result == payload
    assert capability.call_args.kwargs["changes"][0]["direction"] == "nerf"


def test_storage_deck_history_enriches_battle_cards(engine_conn):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#GEM', 'Gem', ?, ?)",
        (now, now),
    )
    engine_conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', ?, ?)",
        (now, now),
    )
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES ('#GEM', ?, 'test')",
        (now,),
    )
    for card in HOG:
        engine_conn.execute(
            "INSERT INTO card_catalog "
            "(card_id, name, elixir_cost, card_type, synced_at) VALUES (?, ?, ?, 'troop', ?)",
            (card["id"], card["name"], card["elixir_cost"], now),
        )
    raw_deck = [{"id": card["id"], "name": card["name"], "level": 15} for card in HOG]
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, outcome, mode_group, "
        "is_competitive, is_ladder, deck_json) "
        "VALUES ('battle:deck', '#GEM', ?, ?, 'W', 'ladder', 1, 1, ?)",
        (now, now, json.dumps(raw_deck)),
    )

    rows = list_deck_battle_history("#GEM", scope="ladder", conn=engine_conn)

    assert len(rows) == 1
    assert rows[0]["player_name"] == "Gem"
    assert rows[0]["cards"][0]["elixir_cost"] is not None
    assert {card["name"] for card in rows[0]["cards"]} == {card["name"] for card in HOG}


def test_storage_splits_duel_decks_and_excludes_boat_defense_shape(engine_conn):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.000Z")
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#GEM', 'Gem', ?, ?)",
        (now, now),
    )
    duel = [{"id": card["id"], "name": card["name"], "level": 15} for card in [*HOG, *GOLEM]]
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, outcome, mode_group, "
        "is_competitive, is_war, deck_selection, deck_json) "
        "VALUES ('battle:duel', '#GEM', ?, ?, 'W', 'war', 1, 1, 'warDeckPick', ?)",
        (now, now, json.dumps(duel)),
    )
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, outcome, mode_group, "
        "is_competitive, is_war, deck_selection, deck_json) "
        "VALUES ('battle:boat', '#GEM', ?, ?, 'W', 'war', 1, 1, 'collection', ?)",
        (now, now, json.dumps(duel[:12])),
    )

    rows = list_deck_battle_history("#GEM", scope="war", conn=engine_conn)

    assert len(rows) == 2
    assert all(len(row["cards"]) == 8 for row in rows)
    assert {row["deck_segment"] for row in rows} == {1, 2}
    assert all(row["outcome_granularity"] == "duel_series" for row in rows)
    assert {row["dedup_key"] for row in rows} == {"battle:duel"}
