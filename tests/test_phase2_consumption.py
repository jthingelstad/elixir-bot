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


def _seed_v5_battles(rows, profiles=()):
    """Seed the v5 projection DB (battle_telemetry + names) for get_elixir_state.

    rows: (tag, battle_time, battle_type, opponent_tag, mode_group, outcome).
    """
    from event_core import config
    from event_core import db as ec_db
    from event_core.ingest.battles import BATTLE_TELEMETRY_DDL

    conn = ec_db.connect(config.PROJECTIONS_DB)
    try:
        conn.execute(BATTLE_TELEMETRY_DDL)
        conn.execute("CREATE TABLE IF NOT EXISTS members (player_tag TEXT UNIQUE, current_name TEXT)")
        for (tag, when, btype, opp, mode_group, outcome) in rows:
            conn.execute(
                "INSERT OR IGNORE INTO battle_telemetry(player_tag,battle_time,battle_type,opponent_tag,"
                "crowns_for,crowns_against,mode_group,outcome,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (tag, when, btype, opp, 1, 0, mode_group, outcome, when),
            )
        for tag, name in profiles:
            conn.execute("INSERT OR IGNORE INTO members(player_tag,current_name) VALUES(?,?)", (tag, name))
        conn.commit()
    finally:
        conn.close()


def _seed_v5_detection(dedup_key, detection_type, subject_tag, when, scope="public"):
    from event_core import config
    from event_core import db as ec_db

    conn = ec_db.connect(config.PROJECTIONS_DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, "
            "detector TEXT, subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO detections(dedup_key,detection_type,detector,subject_tag,occurred_at,scope,payload_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (dedup_key, detection_type, "test", subject_tag, when, scope, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


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


def test_get_elixir_state_game_modes_aspect_is_pullable():
    from agent.tool_exec import _execute_get_elixir_state

    # game_modes reads the v5 battle_telemetry projection; names come from the
    # v5 player_current_profile projection (no elixir.db lookup).
    _seed_v5_battles(
        [
            ("#GM1", f"20260620T12{i:02d}00.000Z", "Ranked1v1_NewArena2", f"#X{i}", "ranked", o)
            for i, o in enumerate(["W", "W", "L", "W"])
        ],
        profiles=[("#GM1", "Climber")],
    )

    # An interactive call can now pull per-mode clan activity on demand.
    result = _execute_get_elixir_state({"aspect": "game_modes"})
    ranked = result["7d"]["modes"]["ranked"]
    assert ranked["battles"] == 4
    assert ranked["label"] == "Ranked"
    assert ranked["top_members"][0]["name"] == "Climber"


def test_get_elixir_state_recent_events_are_signal_grain():
    from agent.tool_exec import _execute_get_elixir_state

    # In the v5 model, recent_events serves signal-grain detections; battles
    # live in battle_telemetry and surface via the game_modes aspect, not
    # recent_events. event_class is vestigial (detections are always signal).
    _seed_v5_detection("det:badge", "badge_earned", "#EC1", "20260620T120000.000Z")
    _seed_v5_battles([("#EC1", "20260620T120000.000Z", "Ranked1v1_NewArena2", "#OPP", "ranked", "W")])

    signal_view = _execute_get_elixir_state({"aspect": "recent_events", "days": 90})
    types = {e["event_type"] for e in signal_view["events"]}
    assert "badge_earned" in types
    assert "battle_played" not in types

    battle_view = _execute_get_elixir_state({"aspect": "recent_events", "days": 90, "event_class": "battle"})
    assert "battle_played" not in {e["event_type"] for e in battle_view["events"]}


def test_get_elixir_state_season_window_aspect_is_reachable():
    from agent.tool_exec import _execute_get_elixir_state
    # public-reachable (before the leadership gate); None when no active war
    assert _execute_get_elixir_state({"aspect": "season_window"}) is None


