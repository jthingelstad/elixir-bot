"""Shared clan game-mode capability contract and cross-window behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from capabilities.game_modes import get_clan_game_mode_windows, get_clan_game_modes


def _cr_time(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_player(conn, tag: str, name: str, league: int, rating: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', 'x', 'x', 1)"
    )
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'x', 'x')",
        (tag, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
        "VALUES (?, '#J2RGCRVG', 'x', 'test')",
        (tag,),
    )
    conn.execute(
        "INSERT INTO player_current_state "
        "(player_tag, observed_at, ranked_league, ranked_trophies) VALUES (?, 'x', ?, ?)",
        (tag, league, rating),
    )


def _seed_battle(
    conn,
    key: str,
    player_tag: str,
    mode_group: str,
    *,
    outcome: str = "W",
    teammate_tag: str | None = None,
    age: timedelta = timedelta(),
) -> None:
    conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, game_mode_name, mode_group, "
        "outcome, is_ranked, teammate_tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            player_tag,
            _cr_time(age),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "Path of Legend" if mode_group == "ranked" else "2v2",
            mode_group,
            outcome,
            int(mode_group == "ranked"),
            teammate_tag,
        ),
    )


def test_capability_combines_activity_leaders_ranked_state_and_duos(engine_conn):
    _seed_player(engine_conn, "#A", "Alpha", 6, 1800)
    _seed_player(engine_conn, "#B", "Bravo", 4, 1200)
    _seed_battle(engine_conn, "r1", "#A", "ranked")
    _seed_battle(engine_conn, "r2", "#A", "ranked", outcome="L")
    _seed_battle(engine_conn, "r3", "#B", "ranked")
    _seed_battle(engine_conn, "d1", "#A", "two_v_two", teammate_tag="#B")
    _seed_battle(engine_conn, "d2", "#A", "two_v_two", outcome="L", teammate_tag="#B")

    result = get_clan_game_modes(days=7, limit=10, conn=engine_conn)

    assert result["capability"] == "clan_game_modes"
    assert result["contract_version"] == 1
    assert result["sources"] == [
        "battle_events",
        "player_current_state",
        "game_mode_contexts",
    ]
    assert result["modes"]["ranked"]["battles"] == 3
    assert result["modes"]["ranked"]["top_members"][0]["member_ref"] == "Alpha"
    assert result["ranked"]["standings"][0]["member_ref"] == "Alpha"
    assert result["ranked"]["standings"][0]["rating"] == 1800
    assert result["duos"] == [{"player": "Alpha", "teammate": "Bravo", "battles": 2, "wins": 1}]


def test_multi_window_capability_uses_the_same_contract(engine_conn):
    _seed_player(engine_conn, "#A", "Alpha", 6, 1800)
    _seed_battle(engine_conn, "recent", "#A", "ranked")
    _seed_battle(engine_conn, "older", "#A", "ranked", age=timedelta(days=20))

    result = get_clan_game_mode_windows(windows=(7, 28), conn=engine_conn)

    assert result["contract_version"] == 1
    assert result["windows"]["7d"]["modes"]["ranked"]["battles"] == 1
    assert result["windows"]["28d"]["modes"]["ranked"]["battles"] == 2


def test_capability_keeps_special_events_distinct_with_per_event_context(engine_conn):
    _seed_player(engine_conn, "#A", "Alpha", 1, 100)
    _seed_player(engine_conn, "#B", "Bravo", 1, 100)
    db.upsert_game_mode_contexts_from_events(
        [
            {"eventTag": "#EVENT_A", "title": "Draft Festival"},
            {"eventTag": "#EVENT_B", "title": "Mirror Festival"},
        ],
        conn=engine_conn,
    )

    battles = [
        ("a1", "#A", "#EVENT_A", "DraftMode", "W"),
        ("a2", "#A", "#EVENT_A", "DraftMode", "W"),
        ("a3", "#A", "#EVENT_A", "DraftMode", "L"),
        ("a4", "#B", "#EVENT_A", "DraftMode", "L"),
        # One event tag may span more than one underlying battle mode. It is
        # still one event in the clan activity read.
        ("a5", "#A", "#EVENT_A", "MirrorBattle", "W"),
        ("b1", "#B", "#EVENT_B", "DraftMode", "W"),
        ("b2", "#B", "#EVENT_B", "DraftMode", "L"),
    ]
    for key, tag, event_tag, mode_name, outcome in battles:
        engine_conn.execute(
            "INSERT INTO battle_events "
            "(dedup_key, player_tag, battle_time, observed_at, game_mode_id, "
            "game_mode_name, mode_group, outcome, is_special_event, event_tag) "
            "VALUES (?, ?, ?, ?, 72000999, ?, 'special_event', ?, 1, ?)",
            (key, tag, _cr_time(), _cr_time(), mode_name, outcome, event_tag),
        )
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, game_mode_id, "
        "game_mode_name, mode_group, outcome, is_special_event, event_tag) "
        "VALUES ('a-old', '#B', ?, ?, 72000999, 'DraftMode', "
        "'special_event', 'L', 1, '#EVENT_A')",
        (_cr_time(timedelta(days=8)), _cr_time()),
    )

    result = get_clan_game_modes(days=7, limit=10, top_members=1, conn=engine_conn)
    events = {event["event_tag"]: event for event in result["events"]["activity"]}

    assert set(events) == {"#EVENT_A", "#EVENT_B"}
    assert events["#EVENT_A"]["event_name"] == "Draft Festival"
    assert events["#EVENT_A"]["members_active"] == 2
    assert events["#EVENT_A"]["battles"] == 5
    assert events["#EVENT_A"]["share_of_clan_battles"] == 0.7143
    assert events["#EVENT_A"]["share_of_special_event_battles"] == 0.7143
    assert events["#EVENT_A"]["previous_window_battles"] == 1
    assert events["#EVENT_A"]["battle_change"] == 4
    assert events["#EVENT_A"]["current_to_previous_ratio"] == 5.0
    assert events["#EVENT_A"]["new_in_window"] is False
    assert events["#EVENT_A"]["top_members"][0]["member_ref"] == "Alpha"
    # The smaller simultaneous event still gets its own leader instead of being
    # crowded out by the busiest event's global leaderboard.
    assert events["#EVENT_B"]["top_members"][0]["member_ref"] == "Bravo"
    assert events["#EVENT_B"]["previous_window_battles"] == 0
    assert events["#EVENT_B"]["new_in_window"] is True
