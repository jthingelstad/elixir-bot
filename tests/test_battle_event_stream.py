"""Phase 1 — battle-grain event stream + game-mode dimension.

These tests pin the shadow-mode contract: battles are projected into
game_event_stream at battle grain with a game_mode family, idempotently, and
are excluded from the prompt-facing (signal-class) readers by default so they
cannot bloat awareness context.
"""
from __future__ import annotations

import db


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_adds_event_class_and_game_mode():
    conn = db.get_connection()
    cols = _columns(conn, "game_event_stream")
    assert "event_class" in cols
    assert "game_mode" in cols
    # existing/new signal rows default to the signal class
    db.record_game_event(
        event_type="member_join",
        source_system="clan_awareness",
        subject_type="member",
        subject_key="#ABC",
        payload={"x": 1},
        conn=conn,
    )
    row = conn.execute(
        "SELECT event_class, game_mode FROM game_event_stream WHERE event_type='member_join'"
    ).fetchone()
    assert row["event_class"] == "signal"
    assert row["game_mode"] is None


def test_record_battle_event_is_classified_and_idempotent():
    conn = db.get_connection()
    kwargs = dict(
        member_tag="#PLAYER1",
        battle_time="20260620T105524.000Z",
        mode_group="ranked",
        battle_type="pathOfLegend",
        game_mode_name="Ranked1v1_NewArena2",
        outcome="win",
        crowns_for=2,
        crowns_against=1,
        trophy_change=30,
        opponent_tag="#OPP1",
    )
    first = db.record_battle_event(conn=conn, **kwargs)
    second = db.record_battle_event(conn=conn, **kwargs)

    assert first["event_key"] == second["event_key"]
    rows = conn.execute(
        "SELECT * FROM game_event_stream WHERE event_class='battle'"
    ).fetchall()
    assert len(rows) == 1  # idempotent — second call is a no-op insert
    ev = rows[0]
    assert ev["event_type"] == "battle_played"
    assert ev["game_mode"] == "ranked"
    assert ev["subject_type"] == "member"
    assert ev["subject_key"] == "#PLAYER1"
    # observed_at is placed at the battle time, not "now"
    assert ev["observed_at"].startswith("2026-06-20T10:55")


def test_battle_events_excluded_from_default_readers():
    conn = db.get_connection()
    # one signal event, one battle event
    db.record_game_event(
        event_type="badge_earned",
        source_system="player_intel",
        subject_type="member",
        subject_key="#SIG",
        payload={},
        conn=conn,
    )
    db.record_battle_event(
        conn=conn,
        member_tag="#BAT",
        battle_time="20260620T105524.000Z",
        mode_group="ladder",
        battle_type="PvP",
    )

    # default readers are signal-only — the prompt-facing guard
    default_recent = db.list_recent_events(conn=conn)
    assert {e["event_type"] for e in default_recent} == {"badge_earned"}

    summary = db.summarize_events_by_window(conn=conn)
    assert summary["90d"]["total"] == 1
    assert "battle_played" not in summary["90d"]["by_type"]

    # battle telemetry is queryable when explicitly requested
    battles = db.list_recent_events(conn=conn, event_class="battle")
    assert {e["event_type"] for e in battles} == {"battle_played"}
    assert battles[0]["game_mode"] == "ladder"

    # event_class=None returns everything
    everything = db.list_recent_events(conn=conn, event_class=None)
    assert len(everything) == 2


def _battle(battle_type, mode_id, mode_name, battle_time, opp_tag="#OPP"):
    return {
        "type": battle_type,
        "battleTime": battle_time,
        "gameMode": {"id": mode_id, "name": mode_name},
        "deckSelection": "collection",
        "leagueNumber": 10,
        "arena": {"id": 54000060, "name": "Spooky Town"},
        "team": [{
            "name": "Tester",
            "tag": "#PLAYERX",
            "crowns": 2,
            "trophyChange": 30,
            "startingTrophies": 9000,
            "cards": [],
            "supportCards": [],
        }],
        "opponent": [{
            "name": "Foe",
            "tag": opp_tag,
            "crowns": 1,
            "cards": [],
            "clan": {"tag": "#FOECLAN"},
        }],
    }


# test_snapshot_player_battlelog_projects_battle_events was removed with F2:
# snapshot_player_battlelog no longer shadow-writes battle-grain rows into
# game_event_stream — battle telemetry now lives in the v5 battle_telemetry
# projection (event_core.ingest.battles).
