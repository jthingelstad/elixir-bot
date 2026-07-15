"""Shared clan game-mode capability contract and cross-window behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from capabilities.game_modes import get_clan_game_mode_windows, get_clan_game_modes


def _cr_time(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y%m%dT%H%M%S.000Z")


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
    assert result["duos"] == [
        {"player": "Alpha", "teammate": "Bravo", "battles": 2, "wins": 1}
    ]


def test_multi_window_capability_uses_the_same_contract(engine_conn):
    _seed_player(engine_conn, "#A", "Alpha", 6, 1800)
    _seed_battle(engine_conn, "recent", "#A", "ranked")
    _seed_battle(engine_conn, "older", "#A", "ranked", age=timedelta(days=20))

    result = get_clan_game_mode_windows(windows=(7, 28), conn=engine_conn)

    assert result["contract_version"] == 1
    assert result["windows"]["7d"]["modes"]["ranked"]["battles"] == 1
    assert result["windows"]["28d"]["modes"]["ranked"]["battles"] == 2
