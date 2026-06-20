"""Phase 2 — consumption: per-mode pulse + concrete season window.

These pin that Elixir can see per-game-mode battle activity (Path of Legends,
2v2, events, …) and the whole-season war trajectory, derived from the
battle-grain stream and war tables.
"""
from __future__ import annotations

import db


def _seed_member(conn, tag, name):
    conn.execute(
        "INSERT INTO members (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (tag, name, "2026-01-01T00:00:00", "2026-06-20T00:00:00"),
    )
    conn.commit()


def _battle(conn, tag, mode_group, battle_type, mode_name, when, outcome, opp):
    db.record_battle_event(
        conn=conn,
        member_tag=tag,
        battle_time=when,
        mode_group=mode_group,
        battle_type=battle_type,
        game_mode_name=mode_name,
        outcome=outcome,
        opponent_tag=opp,
    )


def test_summarize_battle_modes_counts_winrate_and_top_members():
    conn = db.get_connection()
    _seed_member(conn, "#P1", "Ranko")
    _seed_member(conn, "#P2", "Duo")
    # P1: 3 Path-of-Legends (2W / 1L); P2: 4 2v2 (1W / 3L)
    for i, o in enumerate(["W", "W", "L"]):
        _battle(conn, "#P1", "ranked", "pathOfLegend", "Ranked1v1_NewArena2",
                f"20260620T12{i:02d}00.000Z", o, f"#O{i}")
    for i, o in enumerate(["W", "L", "L", "L"]):
        _battle(conn, "#P2", "two_v_two", "clanMate2v2", "TeamVsTeam",
                f"20260620T13{i:02d}00.000Z", o, f"#Q{i}")

    summary = db.summarize_battle_modes(
        windows=(7, 28), now="2026-06-20T14:00:00", min_battles=1, conn=conn
    )
    modes = summary["7d"]["modes"]

    assert modes["ranked"]["battles"] == 3
    assert modes["ranked"]["wins"] == 2 and modes["ranked"]["losses"] == 1
    assert modes["ranked"]["win_rate"] == round(2 / 3, 3)
    assert modes["ranked"]["label"] == "Ranked"
    assert modes["ranked"]["active_members"] == 1
    top = modes["ranked"]["top_members"][0]
    assert top["tag"] == "#P1" and top["name"] == "Ranko"
    assert top["win_rate"] == round(2 / 3, 3)

    assert modes["two_v_two"]["battles"] == 4
    assert modes["two_v_two"]["label"] == "2v2"
    # modes are ordered by battle volume (2v2 = 4 ahead of ranked = 3)
    assert list(modes.keys())[0] == "two_v_two"

    # 28d window contains the same battles
    assert summary["28d"]["modes"]["ranked"]["battles"] == 3


def test_summarize_battle_modes_min_battles_filters_noise():
    conn = db.get_connection()
    _seed_member(conn, "#P3", "Solo")
    _battle(conn, "#P3", "friendly", "friendly", "Friendly",
            "20260620T120000.000Z", "W", "#Z")
    summary = db.summarize_battle_modes(
        windows=(7,), now="2026-06-20T14:00:00", min_battles=3, conn=conn
    )
    assert summary["7d"]["modes"] == {}  # 1 battle < min_battles


def test_get_season_window_trajectory():
    conn = db.get_connection()
    for sec, rank, fame in ((0, 2, 1800), (1, 1, 2400)):
        conn.execute(
            "INSERT INTO war_races (season_id, section_index, created_date, our_rank, "
            "our_fame, trophy_change, total_clans) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (140, sec, f"2026060{sec + 1}T120000.000Z", rank, fame, 60, 5),
        )
    conn.commit()

    window = db.get_season_window(season_id=140, conn=conn)
    assert window["season_id"] == 140
    assert window["weeks_recorded"] == 2
    traj = window["week_trajectory"]
    assert [w["rank"] for w in traj] == [2, 1]
    assert [w["fame"] for w in traj] == [1800, 2400]
    assert window["start"] is not None and window["end"] is not None


def test_situation_blocks_are_safe_on_empty_db():
    import runtime.situation as sit
    assert isinstance(sit._mode_pulse_block(), dict)
    assert sit._season_window_block() is None  # no war data in a fresh DB
