"""lastSeen roster-badge awareness (architecture §13.6): Elixir ingests the CR
API lastSeen so it knows who wears the in-game idle badge — recorded, never used
as an engagement/kick signal (battling stays the kick clock)."""
from __future__ import annotations

import sqlite3

import db
from storage.roster import _ensure_last_seen_api_column
from storage.war_analytics import _in_game_idle_days

LAST_SEEN = "20260701T151811.000Z"


def test_snapshot_ingests_last_seen_api(engine_conn):
    db.snapshot_members(
        [{"tag": "#TR1", "name": "TR", "role": "member", "trophies": 6001,
          "lastSeen": LAST_SEEN}],
        conn=engine_conn,
    )
    cur = engine_conn.execute(
        "SELECT last_seen_api FROM player_current_state WHERE player_tag='#TR1'"
    ).fetchone()
    assert cur["last_seen_api"] == LAST_SEEN
    daily = engine_conn.execute(
        "SELECT last_seen_api FROM player_daily_metrics WHERE player_tag='#TR1'"
    ).fetchone()
    assert daily["last_seen_api"] == LAST_SEEN


def test_snapshot_without_last_seen_is_null(engine_conn):
    db.snapshot_members(
        [{"tag": "#NO1", "name": "NoSeen", "role": "member", "trophies": 5000}],
        conn=engine_conn,
    )
    cur = engine_conn.execute(
        "SELECT last_seen_api FROM player_current_state WHERE player_tag='#NO1'"
    ).fetchone()
    assert cur["last_seen_api"] is None


def test_ensure_last_seen_api_column_adds_when_missing():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE player_current_state (player_tag TEXT PRIMARY KEY, observed_at TEXT)"
    )
    _ensure_last_seen_api_column(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(player_current_state)")}
    assert "last_seen_api" in cols
    # idempotent — a second call is a no-op, not an error
    _ensure_last_seen_api_column(conn)


def test_in_game_idle_days_from_last_seen():
    d = _in_game_idle_days(LAST_SEEN, now="2026-07-08T15:18:11Z")
    assert 6.9 < d < 7.1
    assert _in_game_idle_days(None) is None
    assert _in_game_idle_days("") is None
