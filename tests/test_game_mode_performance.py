"""Per-mode performance — the question the grouped rollups could not answer.

2026-08-04, #ask-elixir: a member asked three times in four minutes how he was
doing in Ken's C.H.A.O.S Draft League. Elixir answered, truthfully, that its
tools bucketed it into overall Events/Challenges tracking. The data held 134 of
his battles at 57% — better than his Ranked record. The gap was the capability
layer, not the model and not the data.

Six special events shared one `special_event` bucket, the busiest of them with
557 clan battles. Reporting that bucket is reporting "sports" instead of the
score.
"""

from __future__ import annotations

import itertools

import capabilities.game_modes as gm

_counter = itertools.count()


def _battle(conn, tag, mode, outcome, when="2026-08-03T12:00:00Z"):
    conn.execute(
        "INSERT INTO battle_events (dedup_key, player_tag, battle_time, outcome, "
        "game_mode_name, mode_group, observed_at) VALUES (?, ?, ?, ?, ?, 'special_event', ?)",
        (f"{tag}:{mode}:{when}:{outcome}:{next(_counter)}", tag, when, outcome, mode, when),
    )


def _seed(conn):
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES ('#AAA','Aaqib','Aaqib','2026-01-01','2026-08-04')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES ('#BBB','Sniper','Sniper','2026-01-01','2026-08-04')"
    )
    for _ in range(6):
        _battle(conn, "#AAA", "Chaos_1v1_Draft", "W")
    for _ in range(4):
        _battle(conn, "#AAA", "Chaos_1v1_Draft", "L")
    for _ in range(9):
        _battle(conn, "#BBB", "Chaos_1v1_Draft", "W")
    _battle(conn, "#BBB", "Chaos_1v1_Draft", "L")
    # A second special event that must NOT bleed into the first's numbers.
    for _ in range(5):
        _battle(conn, "#AAA", "Crazy_Arena", "L")
    # A near-miss name that a substring matcher would wrongly prefer.
    for _ in range(3):
        _battle(conn, "#BBB", "Draft_Competitive", "W")
    conn.commit()


def test_member_phrasing_resolves_to_the_right_mode(engine_conn):
    """The exact words the member used. A substring matcher got this wrong.

    "Ken's C.H.A.O.S Draft League" tokenized to `schaos` — the possessive `s`
    joined the acronym run — so it missed Chaos_1v1_Draft and matched
    Draft_Competitive on the shared word "draft".
    """
    _seed(engine_conn)
    for phrasing in (
        "chaos",
        "C.H.A.O.S Draft League",
        "Ken's C.H.A.O.S Draft League",
        "chaos draft",
        "Chaos_1v1_Draft",
    ):
        matches = gm.resolve_game_mode(phrasing, conn=engine_conn)
        assert matches and matches[0] == "Chaos_1v1_Draft", f"{phrasing!r} -> {matches}"


def test_reports_the_members_record_and_rank(engine_conn):
    _seed(engine_conn)
    result = gm.get_game_mode_performance("chaos", player_tag="#AAA", conn=engine_conn)
    assert result["resolved"] is True
    assert result["mode"] == "Chaos_1v1_Draft"
    member = result["member"]
    assert (member["wins"], member["losses"]) == (6, 4)
    assert member["win_rate"] == 60.0
    assert member["rank"] == 2 and member["ranked_of"] == 2


def test_other_modes_do_not_bleed_in(engine_conn):
    """The bug being fixed: one bucket for every special event."""
    _seed(engine_conn)
    result = gm.get_game_mode_performance("chaos", player_tag="#AAA", conn=engine_conn)
    # 20 Chaos battles seeded in total; the 5 Crazy_Arena losses must be absent.
    assert result["clan"]["battles"] == 20
    assert result["member"]["battles"] == 10


def test_leaderboard_ranks_by_rate_then_volume(engine_conn):
    _seed(engine_conn)
    board = gm.get_game_mode_performance("chaos", conn=engine_conn)["leaderboard"]
    assert [entry["name"] for entry in board] == ["Sniper", "Aaqib"]
    assert board[0]["win_rate"] == 90.0


def test_unresolved_offers_real_modes_instead_of_denying(engine_conn):
    """Never tell a member a mode does not exist — show them the real names."""
    _seed(engine_conn)
    result = gm.get_game_mode_performance("quadruple backflip", conn=engine_conn)
    assert result["resolved"] is False
    names = {mode["mode"] for mode in result["available_modes"]}
    assert "Chaos_1v1_Draft" in names


def test_the_tool_is_actually_reachable():
    """A tool missing from _SHARED_TOOL_NAMES is defined but never offered.

    That is exactly how raise_clan_chat_relay ended up dead: a full definition
    and a live executor that no workflow could ever call.
    """
    from agent.tool_defs import TOOLS

    assert "get_game_mode_performance" in {tool["name"] for tool in TOOLS}


def test_listing_every_mode_for_discovery(engine_conn):
    """Members cannot ask about a mode whose name they do not know.

    The API names are unguessable (`Challenge_AllCards_EventDeck_NoSet`), so
    browsing has to be a first-class question, not just the consolation prize
    for a failed lookup.
    """
    _seed(engine_conn)
    result = gm.list_game_modes(conn=engine_conn)
    assert result["listing"] is True
    by_mode = {m["mode"]: m for m in result["modes"]}
    assert "Chaos_1v1_Draft" in by_mode and "Crazy_Arena" in by_mode
    chaos = by_mode["Chaos_1v1_Draft"]
    assert chaos["battles"] == 20 and chaos["players"] == 2
    assert chaos["clan_win_rate"] == 75.0  # 15W of 20
    assert chaos["label"] == "C.H.A.O.S Draft"
    # Busiest first, so a browsing member sees what the clan actually plays.
    assert result["modes"][0]["battles"] >= result["modes"][-1]["battles"]


def test_omitting_the_mode_routes_to_the_listing(engine_conn):
    _seed(engine_conn)
    assert gm.get_game_mode_performance(conn=engine_conn).get("listing") is True
    assert gm.get_game_mode_performance("  ", conn=engine_conn).get("listing") is True
