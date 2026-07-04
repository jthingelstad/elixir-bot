"""runtime/health.py — the daily engine audit's individual checks."""

import json
import sqlite3

import pytest

from runtime import health


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE tick_history (
            tick_id INTEGER PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            counters_json TEXT NOT NULL
        );
        CREATE TABLE runtime_job_status (
            job_name TEXT PRIMARY KEY,
            status_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE communication_intents (
            intent_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE recognition_ledger (
            ledger_id INTEGER PRIMARY KEY,
            recognition_key TEXT NOT NULL
        );
        CREATE TABLE players (player_tag TEXT PRIMARY KEY);
        CREATE TABLE clan_memberships (
            membership_id INTEGER PRIMARY KEY,
            player_tag TEXT NOT NULL,
            left_at TEXT
        );
        CREATE TABLE poll_state (
            player_tag TEXT PRIMARY KEY,
            last_battlelog_poll TEXT,
            last_profile_poll TEXT
        );
        CREATE TABLE memories (
            memory_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        );
    """)
    return c


def _now(c, offset="0 hours"):
    return c.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", (offset,)
    ).fetchone()[0]


def test_tick_errors_flags_error_counters(conn):
    conn.execute(
        "INSERT INTO tick_history (recorded_at, counters_json) VALUES (?, ?)",
        (_now(conn), json.dumps({"poll_error": 1, "battles_ingested": 0})),
    )
    assert health.check_tick_errors(conn) == ["1 tick(s) with step errors in the last 24h"]


def test_tick_errors_clean_when_counters_healthy(conn):
    conn.execute(
        "INSERT INTO tick_history (recorded_at, counters_json) VALUES (?, ?)",
        (_now(conn), json.dumps({"battles_ingested": 3})),
    )
    conn.execute(
        "INSERT INTO runtime_job_status VALUES ('engine_tick', ?, ?)",
        (json.dumps({"last_failure_at": None}), _now(conn)),
    )
    assert health.check_tick_errors(conn) == []


def test_tick_errors_flags_recent_job_failure(conn):
    conn.execute(
        "INSERT INTO runtime_job_status VALUES ('engine_tick', ?, ?)",
        (json.dumps({"last_failure_at": _now(conn, "-1 hours")}), _now(conn)),
    )
    problems = health.check_tick_errors(conn)
    assert len(problems) == 1 and "recorded a failure" in problems[0]


def test_stuck_intents_only_counts_old_pending_or_failed(conn):
    conn.executemany(
        "INSERT INTO communication_intents (status, created_at) VALUES (?, ?)",
        [
            ("pending", _now(conn, "-3 hours")),     # stuck
            ("failed", _now(conn, "-1 day")),        # stuck
            ("pending", _now(conn, "-10 minutes")),  # fresh — fine
            ("fulfilled", _now(conn, "-1 day")),     # terminal — fine
            ("expired", _now(conn, "-1 day")),       # terminal — fine
        ],
    )
    assert health.check_stuck_intents(conn) == ["2 intent(s) stuck pending/failed for >2h"]


def test_ledger_duplicates(conn):
    conn.executemany(
        "INSERT INTO recognition_ledger (recognition_key) VALUES (?)",
        [("a",), ("b",), ("a",)],
    )
    problems = health.check_ledger_duplicates(conn)
    assert len(problems) == 1 and "1 duplicate key(s)" in problems[0]


def test_poll_starvation_null_counts_as_starved(conn):
    conn.execute("INSERT INTO players VALUES ('#AAA')")
    conn.execute("INSERT INTO clan_memberships (player_tag, left_at) VALUES ('#AAA', NULL)")
    conn.execute(
        "INSERT INTO poll_state (player_tag, last_battlelog_poll, last_profile_poll) VALUES ('#AAA', NULL, NULL)"
    )
    problems = health.check_poll_starvation(conn)
    assert "1 member(s) past the 6h battlelog floor" in problems
    assert "1 member(s) past the 24h profile floor" in problems


def test_poll_starvation_ignores_departed_members(conn):
    conn.execute("INSERT INTO players VALUES ('#GONE')")
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, left_at) VALUES ('#GONE', '2026-06-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO poll_state (player_tag, last_battlelog_poll, last_profile_poll) VALUES ('#GONE', NULL, NULL)"
    )
    assert health.check_poll_starvation(conn) == []


def test_memory_writes_flags_stale_and_accepts_fresh(conn):
    conn.execute("INSERT INTO memories (created_at) VALUES (?)", (_now(conn, "-3 days"),))
    assert len(health.check_memory_writes(conn)) == 1
    conn.execute("INSERT INTO memories (created_at) VALUES (?)", (_now(conn, "-1 hours"),))
    assert health.check_memory_writes(conn) == []
