"""Tests for db.py — SQLite history store."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

import db


@pytest.fixture
def conn():
    """In-memory SQLite DB with schema."""
    c = db.get_connection(":memory:")
    yield c
    c.close()


SAMPLE_MEMBERS = [
    {
        "tag": "#ABC123",
        "name": "King Levy",
        "trophies": 9500,
        "bestTrophies": 9600,
        "donations": 80,
        "donationsReceived": 40,
        "role": "elder",
        "arena": {"id": 21, "name": "Electro Valley"},
        "expLevel": 14,
        "clanRank": 1,
        "lastSeen": "20260304T120000.000Z",
    },
    {
        "tag": "#DEF456",
        "name": "Vijay",
        "trophies": 7200,
        "bestTrophies": 7500,
        "donations": 120,
        "donationsReceived": 30,
        "role": "member",
        "arena": {"id": 18, "name": "Rascal's Hideout"},
        "expLevel": 12,
        "clanRank": 2,
        "lastSeen": "20260304T100000.000Z",
    },
]


def test_schema_creation(conn):
    """DB initializes with all 3 tables."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [t["name"] for t in tables]
    assert "member_snapshots" in names
    assert "war_results" in names
    assert "war_participation" in names


def test_snapshot_members(conn):
    """Snapshots are recorded correctly."""
    db.snapshot_members(SAMPLE_MEMBERS, conn=conn)
    rows = conn.execute("SELECT * FROM member_snapshots").fetchall()
    assert len(rows) == 2
    levy = [r for r in rows if r["tag"] == "#ABC123"][0]
    assert levy["name"] == "King Levy"
    assert levy["trophies"] == 9500
    assert levy["arena_name"] == "Electro Valley"
    assert levy["role"] == "elder"


def test_snapshot_only_stores_changes(conn):
    """Calling snapshot twice with same data only stores one row per member."""
    db.snapshot_members(SAMPLE_MEMBERS, conn=conn)
    db.snapshot_members(SAMPLE_MEMBERS, conn=conn)  # Same data again
    rows = conn.execute("SELECT * FROM member_snapshots").fetchall()
    assert len(rows) == 2  # Still just 2, not 4

    # Now change trophies for one member
    changed = [
        {**SAMPLE_MEMBERS[0], "trophies": 9600},  # Changed
        SAMPLE_MEMBERS[1],  # Unchanged
    ]
    db.snapshot_members(changed, conn=conn)
    rows = conn.execute("SELECT * FROM member_snapshots").fetchall()
    assert len(rows) == 3  # Only the changed member got a new row


def test_detect_milestones(conn):
    """Trophy milestone crossing is detected."""
    # First snapshot: 9850 trophies
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9850, "Arena 24", "2026-03-04T10:00:00"),
    )
    # Second snapshot: 10023 trophies — crossed 10k
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC123", "King Levy", 10023, "Arena 25", "2026-03-04T11:00:00"),
    )
    conn.commit()

    milestones = db.detect_milestones(conn=conn)
    trophy_milestones = [m for m in milestones if m["type"] == "trophy_milestone"]
    assert len(trophy_milestones) == 1
    assert trophy_milestones[0]["milestone"] == 10000
    assert trophy_milestones[0]["name"] == "King Levy"
    assert trophy_milestones[0]["old_value"] == 9850
    assert trophy_milestones[0]["new_value"] == 10023

    # Also detect the arena change
    arena_changes = [m for m in milestones if m["type"] == "arena_change"]
    assert len(arena_changes) == 1
    assert arena_changes[0]["old_value"] == "Arena 24"
    assert arena_changes[0]["new_value"] == "Arena 25"


def test_detect_milestones_no_change(conn):
    """No false positives when trophies stay within the same milestone range."""
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9100, "Arena 24", "2026-03-04T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9300, "Arena 24", "2026-03-04T11:00:00"),
    )
    conn.commit()

    milestones = db.detect_milestones(conn=conn)
    assert len(milestones) == 0


def test_detect_role_changes(conn):
    """Promotion from member to elder is detected."""
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, role, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#DEF456", "Vijay", "member", 7200, "2026-03-04T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, role, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#DEF456", "Vijay", "elder", 7200, "2026-03-04T11:00:00"),
    )
    conn.commit()

    changes = db.detect_role_changes(conn=conn)
    assert len(changes) == 1
    assert changes[0]["name"] == "Vijay"
    assert changes[0]["old_role"] == "member"
    assert changes[0]["new_role"] == "elder"


def test_store_war_log(conn):
    """War results are ingested; duplicates are skipped."""
    race_log = {
        "items": [
            {
                "seasonId": 50,
                "sectionIndex": 2,
                "createdDate": "20260301T120000.000Z",
                "standings": [
                    {
                        "rank": 1,
                        "clan": {
                            "tag": "#J2RGCRVG",
                            "name": "POAP KINGS",
                            "fame": 10500,
                            "finishTime": "20260301T100000.000Z",
                            "participants": [
                                {"tag": "#ABC123", "name": "King Levy", "fame": 3200, "repairPoints": 0, "decksUsed": 4},
                                {"tag": "#DEF456", "name": "Vijay", "fame": 2800, "repairPoints": 0, "decksUsed": 4},
                            ],
                        },
                    },
                    {
                        "rank": 2,
                        "clan": {"tag": "#OTHERCLAN", "name": "Other", "fame": 8000},
                    },
                ],
            }
        ]
    }

    db.store_war_log(race_log, "J2RGCRVG", conn=conn)
    results = conn.execute("SELECT * FROM war_results").fetchall()
    assert len(results) == 1
    assert results[0]["our_rank"] == 1
    assert results[0]["our_fame"] == 10500

    # Participants stored
    parts = conn.execute("SELECT * FROM war_participation").fetchall()
    assert len(parts) == 2
    levy = [p for p in parts if p["tag"] == "#ABC123"][0]
    assert levy["fame"] == 3200

    # Storing again should not duplicate
    db.store_war_log(race_log, "J2RGCRVG", conn=conn)
    results = conn.execute("SELECT * FROM war_results").fetchall()
    assert len(results) == 1


def test_get_promotion_candidates(conn):
    """Members with good stats but 'member' role are returned as candidates."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    recent_seen = datetime.utcnow().strftime("%Y%m%dT%H%M%S.000Z")

    # Good candidate: member role, high donations, recently active
    conn.execute(
        "INSERT INTO member_snapshots "
        "(tag, name, trophies, donations, role, last_seen, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("#DEF456", "Vijay", 7200, 80, "member", recent_seen, now),
    )
    # Not a candidate: already elder
    conn.execute(
        "INSERT INTO member_snapshots "
        "(tag, name, trophies, donations, role, last_seen, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9500, 100, "elder", recent_seen, now),
    )
    # Not a candidate: low donations
    conn.execute(
        "INSERT INTO member_snapshots "
        "(tag, name, trophies, donations, role, last_seen, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("#GHI789", "Newbie", 3000, 10, "member", recent_seen, now),
    )
    conn.commit()

    # Add a war participation for Vijay
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (1, 50, 1, 2, 8000, '20260301T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#DEF456', 'Vijay', 2800, 4)"
    )
    conn.commit()

    candidates = db.get_promotion_candidates(conn=conn)
    assert len(candidates) == 1
    assert candidates[0]["name"] == "Vijay"
    assert candidates[0]["war_participations"] == 1


def test_purge_old_data(conn):
    """Old snapshots and war data are purged; recent data survives."""
    old_time = (datetime.utcnow() - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S")
    recent_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # Old snapshot (>90 days)
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 8000, old_time),
    )
    # Recent snapshot
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9500, recent_time),
    )

    # Old war result (>180 days)
    very_old = (datetime.utcnow() - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, "
        "created_date, recorded_at) VALUES (1, 40, 1, 2, 5000, '20250801T120000.000Z', ?)",
        (very_old,),
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame) VALUES (1, '#ABC123', 'King Levy', 1500)"
    )
    # Recent war result
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, "
        "created_date, recorded_at) VALUES (2, 50, 2, 1, 10000, '20260301T120000.000Z', ?)",
        (recent_time,),
    )
    conn.commit()

    db.purge_old_data(conn=conn)

    # Old snapshot gone, recent survives
    snaps = conn.execute("SELECT * FROM member_snapshots").fetchall()
    assert len(snaps) == 1
    assert snaps[0]["trophies"] == 9500

    # Old war gone, recent survives
    wars = conn.execute("SELECT * FROM war_results").fetchall()
    assert len(wars) == 1
    assert wars[0]["season_id"] == 50

    # Old participation gone
    parts = conn.execute("SELECT * FROM war_participation").fetchall()
    assert len(parts) == 0


def test_get_war_history(conn):
    """Returns war results ordered by date descending."""
    conn.execute(
        "INSERT INTO war_results (season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (50, 1, 2, 8000, '20260201T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_results (season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (50, 2, 1, 10500, '20260301T120000.000Z')"
    )
    conn.commit()

    history = db.get_war_history(n=10, conn=conn)
    assert len(history) == 2
    # Most recent first
    assert history[0]["our_fame"] == 10500
    assert history[1]["our_fame"] == 8000


def test_get_member_war_stats(conn):
    """Returns a member's war participation joined with war results."""
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (1, 50, 1, 2, 8000, '20260201T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#ABC123', 'King Levy', 3200, 4)"
    )
    conn.commit()

    stats = db.get_member_war_stats("#ABC123", conn=conn)
    assert len(stats) == 1
    assert stats[0]["fame"] == 3200
    assert stats[0]["our_rank"] == 2


def test_get_trophy_changes(conn):
    """Detects trophy changes over a time window."""
    now = datetime.utcnow()
    old_time = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
    new_time = now.strftime("%Y-%m-%dT%H:%M:%S")

    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9000, old_time),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9500, new_time),
    )
    conn.commit()

    changes = db.get_trophy_changes(since_hours=24, conn=conn)
    assert len(changes) == 1
    assert changes[0]["change"] == 500
    assert changes[0]["name"] == "King Levy"


def test_get_member_history(conn):
    """Returns snapshot history for a member."""
    now = datetime.utcnow()
    for i in range(3):
        t = (now - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("#ABC123", "King Levy", 9000 + i * 100, t),
        )
    conn.commit()

    history = db.get_member_history("#ABC123", days=30, conn=conn)
    assert len(history) == 3
    # Ordered by time ascending
    assert history[0]["trophies"] == 9200
    assert history[2]["trophies"] == 9000


# ── Conversation memory tests ───────────────────────────────────────────────

def test_save_and_get_conversation(conn):
    """Conversation turns are saved and retrieved in chronological order."""
    db.save_conversation_turn("user123", "LeaderBob", "user", "Who should we promote?", conn=conn)
    db.save_conversation_turn("user123", "LeaderBob", "assistant", "Vijay looks ready.", conn=conn)
    db.save_conversation_turn("user123", "LeaderBob", "user", "What about King Levy?", conn=conn)

    history = db.get_conversation_history("user123", conn=conn)
    assert len(history) == 3
    # Oldest first
    assert history[0]["role"] == "user"
    assert "promote" in history[0]["content"]
    assert history[1]["role"] == "assistant"
    assert history[2]["role"] == "user"
    assert "King Levy" in history[2]["content"]


def test_conversation_per_leader(conn):
    """Different leaders have separate conversation histories."""
    db.save_conversation_turn("leader1", "Alice", "user", "Question from Alice", conn=conn)
    db.save_conversation_turn("leader2", "Bob", "user", "Question from Bob", conn=conn)

    alice_history = db.get_conversation_history("leader1", conn=conn)
    bob_history = db.get_conversation_history("leader2", conn=conn)

    assert len(alice_history) == 1
    assert len(bob_history) == 1
    assert "Alice" in alice_history[0]["content"]
    assert "Bob" in bob_history[0]["content"]


def test_conversation_limit(conn):
    """History is limited to the requested number of turns."""
    for i in range(15):
        db.save_conversation_turn("user123", "Bob", "user", f"Message {i}", conn=conn)

    history = db.get_conversation_history("user123", limit=5, conn=conn)
    assert len(history) == 5
    # Should be the 5 most recent, oldest first
    assert "Message 10" in history[0]["content"]
    assert "Message 14" in history[4]["content"]


def test_conversation_trimmed_on_save(conn):
    """Excess turns are trimmed when saving beyond CONVERSATION_MAX_PER_LEADER."""
    for i in range(25):
        db.save_conversation_turn("user123", "Bob", "user", f"Message {i}", conn=conn)

    rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM leader_conversations WHERE author_id = 'user123'"
    ).fetchone()
    assert rows["cnt"] <= db.CONVERSATION_MAX_PER_LEADER


def test_purge_old_conversations(conn):
    """Old conversations are purged."""
    old_time = (datetime.utcnow() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")
    recent_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    conn.execute(
        "INSERT INTO leader_conversations (author_id, author_name, role, content, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("user123", "Bob", "user", "Old question", old_time),
    )
    conn.execute(
        "INSERT INTO leader_conversations (author_id, author_name, role, content, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("user123", "Bob", "user", "Recent question", recent_time),
    )
    conn.commit()

    db.purge_old_conversations(conn=conn)

    rows = conn.execute("SELECT * FROM leader_conversations").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "Recent question"


# ── War Champ tests ──────────────────────────────────────────────────────────

def test_get_war_champ_standings(conn):
    """Aggregates fame per member across a season."""
    # Two war results in the same season
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (1, 50, 1, 2, 8000, '20260201T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (2, 50, 2, 1, 10500, '20260301T120000.000Z')"
    )
    # Participation for both races
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#ABC123', 'King Levy', 3200, 4)"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#DEF456', 'Vijay', 2800, 4)"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (2, '#ABC123', 'King Levy', 3500, 4)"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (2, '#DEF456', 'Vijay', 4000, 4)"
    )
    conn.commit()

    standings = db.get_war_champ_standings(season_id=50, conn=conn)
    assert len(standings) == 2
    # Vijay has more total fame (2800 + 4000 = 6800) vs Levy (3200 + 3500 = 6700)
    assert standings[0]["name"] == "Vijay"
    assert standings[0]["total_fame"] == 6800
    assert standings[0]["races_participated"] == 2
    assert standings[1]["name"] == "King Levy"
    assert standings[1]["total_fame"] == 6700


def test_get_war_champ_standings_auto_season(conn):
    """Uses the most recent season when season_id is not specified."""
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (1, 49, 1, 2, 8000, '20260101T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (2, 50, 1, 1, 10000, '20260301T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#ABC123', 'King Levy', 3000, 4)"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (2, '#ABC123', 'King Levy', 3500, 4)"
    )
    conn.commit()

    # Without specifying season_id, should use season 50
    standings = db.get_war_champ_standings(conn=conn)
    assert len(standings) == 1
    assert standings[0]["total_fame"] == 3500  # Only season 50 data


def test_get_war_champ_standings_empty(conn):
    """Returns empty list when no war data exists."""
    standings = db.get_war_champ_standings(conn=conn)
    assert standings == []


def test_get_current_season_id(conn):
    """Returns the most recent season_id."""
    conn.execute(
        "INSERT INTO war_results (season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (49, 1, 2, 8000, '20260101T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_results (season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (50, 1, 1, 10000, '20260301T120000.000Z')"
    )
    conn.commit()

    assert db.get_current_season_id(conn=conn) == 50


# ── Perfect war participation tests ─────────────────────────────────────────

def test_get_perfect_war_participants(conn):
    """Members who participated in every race are identified."""
    # Season 50 with 3 races
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
            "VALUES (?, 50, ?, 1, 10000, ?)",
            (i, i, f"2026030{i}T120000.000Z"),
        )
    # King Levy played all 3 races
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
            "VALUES (?, '#ABC123', 'King Levy', 3000, 4)",
            (i,),
        )
    # Vijay only played 2 of 3
    for i in range(1, 3):
        conn.execute(
            "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
            "VALUES (?, '#DEF456', 'Vijay', 2500, 4)",
            (i,),
        )
    conn.commit()

    perfect = db.get_perfect_war_participants(season_id=50, conn=conn)
    assert len(perfect) == 1
    assert perfect[0]["name"] == "King Levy"
    assert perfect[0]["races_participated"] == 3
    assert perfect[0]["total_races_in_season"] == 3
    assert perfect[0]["total_fame"] == 9000


def test_get_perfect_war_participants_multiple(conn):
    """Multiple members can have perfect participation."""
    for i in range(1, 3):
        conn.execute(
            "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
            "VALUES (?, 50, ?, 1, 10000, ?)",
            (i, i, f"2026030{i}T120000.000Z"),
        )
    for i in range(1, 3):
        conn.execute(
            "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
            "VALUES (?, '#ABC', 'King Levy', 3000, 4)",
            (i,),
        )
        conn.execute(
            "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
            "VALUES (?, '#DEF', 'Vijay', 3500, 4)",
            (i,),
        )
    conn.commit()

    perfect = db.get_perfect_war_participants(season_id=50, conn=conn)
    assert len(perfect) == 2


def test_get_perfect_war_participants_empty(conn):
    """Returns empty when no war data."""
    assert db.get_perfect_war_participants(conn=conn) == []


# ── Cake day tests ─────────────────────────────────────────────────────────

def test_schema_includes_cake_day_tables(conn):
    """DB initializes with member_dates and cake_day_announcements tables."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [t["name"] for t in tables]
    assert "member_dates" in names
    assert "cake_day_announcements" in names


def test_set_and_get_member_birthday(conn):
    """Birthday can be set and retrieved."""
    db.set_member_birthday("#ABC123", "King Levy", 7, 15, conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result is not None
    assert result["birth_month"] == 7
    assert result["birth_day"] == 15
    assert result["name"] == "King Levy"
    assert result["joined_date"] is None


def test_set_member_birthday_overwrites(conn):
    """Birthday can be overwritten."""
    db.set_member_birthday("#ABC123", "King Levy", 7, 15, conn=conn)
    db.set_member_birthday("#ABC123", "King Levy", 12, 25, conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result["birth_month"] == 12
    assert result["birth_day"] == 25


def test_set_and_get_member_join_date(conn):
    """Join date can be set (leader override) and retrieved."""
    db.set_member_join_date("#ABC123", "King Levy", "2025-06-01", conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result is not None
    assert result["joined_date"] == "2025-06-01"


def test_record_join_date_does_not_overwrite(conn):
    """record_join_date only sets if not already present."""
    db.set_member_join_date("#ABC123", "King Levy", "2025-01-01", conn=conn)
    db.record_join_date("#ABC123", "King Levy", "2026-03-05", conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result["joined_date"] == "2025-01-01"  # Not overwritten


def test_record_join_date_sets_when_null(conn):
    """record_join_date sets date when none exists."""
    db.record_join_date("#ABC123", "King Levy", "2026-03-05", conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result["joined_date"] == "2026-03-05"


def test_backfill_join_dates(conn):
    """Backfills join dates from earliest snapshot."""
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9000, "2025-06-15T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9500, "2025-07-01T10:00:00"),
    )
    conn.commit()

    db.backfill_join_dates(conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result is not None
    assert result["joined_date"] == "2025-06-15"


def test_backfill_does_not_overwrite_existing(conn):
    """Backfill respects existing (leader-set) join dates."""
    db.set_member_join_date("#ABC123", "King Levy", "2024-01-01", conn=conn)

    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        ("#ABC123", "King Levy", 9000, "2025-06-15T10:00:00"),
    )
    conn.commit()

    db.backfill_join_dates(conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result["joined_date"] == "2024-01-01"


def test_get_join_anniversaries_today(conn):
    """Returns members whose join anniversary matches today."""
    db.set_member_join_date("#ABC123", "King Levy", "2025-03-05", conn=conn)
    db.set_member_join_date("#DEF456", "Vijay", "2025-07-10", conn=conn)
    # Same year join should be excluded
    db.set_member_join_date("#GHI789", "Newbie", "2026-03-05", conn=conn)

    results = db.get_join_anniversaries_today("2026-03-05", conn=conn)
    assert len(results) == 1
    assert results[0]["tag"] == "#ABC123"
    assert results[0]["years"] == 1


def test_get_birthdays_today(conn):
    """Returns members whose birthday matches today."""
    db.set_member_birthday("#ABC123", "King Levy", 3, 5, conn=conn)
    db.set_member_birthday("#DEF456", "Vijay", 7, 10, conn=conn)

    results = db.get_birthdays_today("2026-03-05", conn=conn)
    assert len(results) == 1
    assert results[0]["tag"] == "#ABC123"


def test_announcement_dedup(conn):
    """Announcement dedup prevents re-announcing."""
    assert not db.was_announcement_sent("2026-03-05", "birthday", "#ABC123", conn=conn)
    db.mark_announcement_sent("2026-03-05", "birthday", "#ABC123", conn=conn)
    assert db.was_announcement_sent("2026-03-05", "birthday", "#ABC123", conn=conn)


def test_announcement_dedup_clan_birthday(conn):
    """Clan birthday dedup works with NULL target_tag."""
    assert not db.was_announcement_sent("2026-02-04", "clan_birthday", None, conn=conn)
    db.mark_announcement_sent("2026-02-04", "clan_birthday", None, conn=conn)
    assert db.was_announcement_sent("2026-02-04", "clan_birthday", None, conn=conn)


def test_migration_version_tracking():
    """PRAGMA user_version equals len(_MIGRATIONS) after get_connection."""
    conn = db.get_connection(":memory:")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(db._MIGRATIONS)
    finally:
        conn.close()


def test_migrations_idempotent():
    """Running get_connection twice on the same DB causes no errors."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn1 = db.get_connection(path)
        v1 = conn1.execute("PRAGMA user_version").fetchone()[0]
        conn1.close()

        conn2 = db.get_connection(path)
        v2 = conn2.execute("PRAGMA user_version").fetchone()[0]
        conn2.close()

        assert v1 == v2 == len(db._MIGRATIONS)
    finally:
        os.unlink(path)


def test_clear_member_tenure(conn):
    """Clearing tenure NULLs join date and removes anniversary announcements."""
    db.set_member_join_date("#ABC123", "King Levy", "2025-01-01", conn=conn)
    db.mark_announcement_sent("2026-01-01", "join_anniversary", "#ABC123", conn=conn)
    db.mark_announcement_sent("2026-01-01", "birthday", "#ABC123", conn=conn)

    db.clear_member_tenure("#ABC123", conn=conn)

    dates = db.get_member_dates("#ABC123", conn=conn)
    assert dates["joined_date"] is None
    # Anniversary announcement removed, birthday announcement kept
    assert not db.was_announcement_sent("2026-01-01", "join_anniversary", "#ABC123", conn=conn)
    assert db.was_announcement_sent("2026-01-01", "birthday", "#ABC123", conn=conn)


def test_clear_member_tenure_preserves_birthday(conn):
    """Clearing tenure keeps birthday data intact."""
    db.set_member_join_date("#ABC123", "King Levy", "2025-01-01", conn=conn)
    db.set_member_birthday("#ABC123", "King Levy", 7, 15, conn=conn)

    db.clear_member_tenure("#ABC123", conn=conn)

    dates = db.get_member_dates("#ABC123", conn=conn)
    assert dates["joined_date"] is None
    assert dates["birth_month"] == 7
    assert dates["birth_day"] == 15


def test_birthday_and_join_date_independent(conn):
    """Birthday and join date can be set independently on the same member."""
    db.set_member_birthday("#ABC123", "King Levy", 7, 15, conn=conn)
    db.set_member_join_date("#ABC123", "King Levy", "2025-01-01", conn=conn)
    result = db.get_member_dates("#ABC123", conn=conn)
    assert result["birth_month"] == 7
    assert result["birth_day"] == 15
    assert result["joined_date"] == "2025-01-01"
