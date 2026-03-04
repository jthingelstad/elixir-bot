"""Tests for heartbeat.py — signal detection."""

from datetime import datetime
from unittest.mock import patch

import pytest

import db
import heartbeat


@pytest.fixture
def conn():
    """In-memory SQLite DB with schema."""
    c = db.get_connection(":memory:")
    yield c
    c.close()


MEMBERS_A = [
    {"tag": "#ABC", "name": "King Levy", "trophies": 9500, "donations": 80,
     "role": "elder", "arena": {"id": 24, "name": "Legendary Arena"},
     "lastSeen": "20260304T120000.000Z"},
    {"tag": "#DEF", "name": "Vijay", "trophies": 7200, "donations": 120,
     "role": "member", "arena": {"id": 19, "name": "Dragon Spa"},
     "lastSeen": "20260304T100000.000Z"},
]

MEMBERS_B_MILESTONE = [
    {"tag": "#ABC", "name": "King Levy", "trophies": 10100, "donations": 90,
     "role": "elder", "arena": {"id": 25, "name": "Lumberlove"},
     "lastSeen": "20260304T130000.000Z"},
    {"tag": "#DEF", "name": "Vijay", "trophies": 7200, "donations": 130,
     "role": "member", "arena": {"id": 19, "name": "Dragon Spa"},
     "lastSeen": "20260304T110000.000Z"},
]


def test_detect_joins_leaves():
    """Detects new members and departures."""
    known = {"#ABC": "King Levy", "#OLD": "GonePlayer"}
    current = [
        {"tag": "#ABC", "name": "King Levy"},
        {"tag": "#NEW", "name": "NewPlayer"},
    ]

    signals, updated = heartbeat.detect_joins_leaves(current, known)

    join_signals = [s for s in signals if s["type"] == "member_join"]
    leave_signals = [s for s in signals if s["type"] == "member_leave"]

    assert len(join_signals) == 1
    assert join_signals[0]["name"] == "NewPlayer"
    assert len(leave_signals) == 1
    assert leave_signals[0]["name"] == "GonePlayer"
    assert "#NEW" in updated
    assert "#OLD" not in updated


def test_detect_trophy_milestones(conn):
    """Trophy milestone crossing produces correct signal."""
    # Snapshot A: 9500 trophies
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 9500, "Legendary Arena", "2026-03-04T10:00:00"),
    )
    # Snapshot B: 10100 trophies — crossed 10k
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 10100, "Lumberlove", "2026-03-04T11:00:00"),
    )
    conn.commit()

    signals = heartbeat.detect_trophy_milestones(conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "trophy_milestone"
    assert signals[0]["milestone"] == 10000
    assert signals[0]["name"] == "King Levy"


def test_detect_arena_changes(conn):
    """Arena change produces correct signal."""
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 9500, "Legendary Arena", "2026-03-04T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 10100, "Lumberlove", "2026-03-04T11:00:00"),
    )
    conn.commit()

    signals = heartbeat.detect_arena_changes(conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "arena_change"
    assert signals[0]["old_arena"] == "Legendary Arena"
    assert signals[0]["new_arena"] == "Lumberlove"


def test_war_day_detection_thursday():
    """Thursday triggers war_day_start signal."""
    thursday = datetime(2026, 3, 5, 10, 0)  # March 5, 2026 is Thursday
    signals = heartbeat.detect_war_day_transition(now=thursday)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_day_start"


def test_war_day_detection_sunday():
    """Sunday triggers war_day_end signal."""
    sunday = datetime(2026, 3, 8, 10, 0)  # March 8, 2026 is Sunday
    signals = heartbeat.detect_war_day_transition(now=sunday)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_day_end"


def test_war_day_detection_wednesday():
    """Wednesday (training day) produces no signal."""
    wednesday = datetime(2026, 3, 4, 10, 0)  # March 4, 2026 is Wednesday
    signals = heartbeat.detect_war_day_transition(now=wednesday)
    assert len(signals) == 0


def test_war_day_detection_friday():
    """Friday is a regular battle day."""
    friday = datetime(2026, 3, 6, 10, 0)
    signals = heartbeat.detect_war_day_transition(now=friday)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_battle_day"


def test_detect_donation_leaders():
    """Top 3 donors are identified."""
    members = [
        {"name": "A", "donations": 150},
        {"name": "B", "donations": 100},
        {"name": "C", "donations": 50},
        {"name": "D", "donations": 10},
    ]
    signals = heartbeat.detect_donation_leaders(members)
    assert len(signals) == 1
    assert signals[0]["type"] == "donation_leaders"
    leaders = signals[0]["leaders"]
    assert len(leaders) == 3
    assert leaders[0]["name"] == "A"
    assert leaders[0]["donations"] == 150


def test_detect_donation_leaders_no_donations():
    """No signal when nobody has donated."""
    members = [{"name": "A", "donations": 0}]
    signals = heartbeat.detect_donation_leaders(members)
    assert len(signals) == 0


def test_detect_inactivity():
    """Flags members not seen in 3+ days."""
    now = datetime(2026, 3, 10, 12, 0)
    members = [
        {"name": "Active", "tag": "#A", "lastSeen": "20260310T100000.000Z", "role": "member"},
        {"name": "Inactive", "tag": "#B", "lastSeen": "20260305T100000.000Z", "role": "member"},
        {"name": "VeryInactive", "tag": "#C", "lastSeen": "20260301T100000.000Z", "role": "member"},
    ]
    signals = heartbeat.detect_inactivity(members, now=now)
    assert len(signals) == 1
    assert signals[0]["type"] == "inactive_members"
    names = [m["name"] for m in signals[0]["members"]]
    assert "Inactive" in names
    assert "VeryInactive" in names
    assert "Active" not in names
    # Sorted by most inactive first
    assert signals[0]["members"][0]["name"] == "VeryInactive"


def test_detect_role_changes(conn):
    """Promotion from member to elder produces signal."""
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, role, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#DEF", "Vijay", "member", 7200, "2026-03-04T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, role, trophies, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("#DEF", "Vijay", "elder", 7200, "2026-03-04T11:00:00"),
    )
    conn.commit()

    signals = heartbeat.detect_role_changes(conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "role_change"
    assert signals[0]["old_role"] == "member"
    assert signals[0]["new_role"] == "elder"


def test_detect_war_deck_usage_battle_day():
    """War deck usage signal produced on battle days."""
    war_data = {
        "state": "warDay",
        "clan": {
            "participants": [
                {"name": "King Levy", "tag": "#ABC", "decksUsedToday": 4},
                {"name": "Vijay", "tag": "#DEF", "decksUsedToday": 2},
                {"name": "Newbie", "tag": "#GHI", "decksUsedToday": 0},
            ]
        }
    }
    # Thursday = battle day
    with patch("heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 3, 5, 18, 0)  # Thursday
        mock_dt.utcnow = datetime.utcnow
        mock_dt.strptime = datetime.strptime
        signals = heartbeat.detect_war_deck_usage(war_data)

    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "war_deck_usage"
    assert len(sig["used_all_4"]) == 1
    assert sig["used_all_4"][0]["name"] == "King Levy"
    assert len(sig["used_some"]) == 1
    assert sig["used_some"][0]["name"] == "Vijay"
    assert len(sig["used_none"]) == 1
    assert sig["used_none"][0]["name"] == "Newbie"


def test_detect_war_deck_usage_training_day():
    """No war deck signal on training days."""
    war_data = {"state": "warDay", "clan": {"participants": []}}
    # Wednesday = training day
    with patch("heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 3, 4, 18, 0)  # Wednesday
        signals = heartbeat.detect_war_deck_usage(war_data)

    assert len(signals) == 0


def test_detect_war_deck_usage_no_war():
    """No signal when not in war."""
    signals = heartbeat.detect_war_deck_usage({"state": "notInWar"})
    assert len(signals) == 0


def test_detect_war_champ_update(conn):
    """War Champ standings signal includes top contributors."""
    conn.execute(
        "INSERT INTO war_results (id, season_id, section_index, our_rank, our_fame, created_date) "
        "VALUES (1, 50, 1, 1, 10000, '20260301T120000.000Z')"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#ABC', 'King Levy', 3500, 4)"
    )
    conn.execute(
        "INSERT INTO war_participation (war_result_id, tag, name, fame, decks_used) "
        "VALUES (1, '#DEF', 'Vijay', 4000, 4)"
    )
    conn.commit()

    signals = heartbeat.detect_war_champ_update(conn=conn)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "war_champ_standings"
    assert sig["season_id"] == 50
    assert sig["leader"]["name"] == "Vijay"  # Vijay has more fame
    assert len(sig["standings"]) == 2


def test_detect_war_champ_update_empty(conn):
    """No signal when no war data."""
    signals = heartbeat.detect_war_champ_update(conn=conn)
    assert len(signals) == 0


def test_multiple_signals(conn):
    """Multiple changes produce multiple signals."""
    # Snapshot A
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, role, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 9850, "elder", "Legendary Arena", "2026-03-04T10:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, role, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("#DEF", "Vijay", 7200, "member", "Dragon Spa", "2026-03-04T10:00:00"),
    )
    # Snapshot B: Levy crosses 10k + new arena, Vijay promoted
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, role, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("#ABC", "King Levy", 10100, "elder", "Lumberlove", "2026-03-04T11:00:00"),
    )
    conn.execute(
        "INSERT INTO member_snapshots (tag, name, trophies, role, arena_name, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("#DEF", "Vijay", 7200, "elder", "Dragon Spa", "2026-03-04T11:00:00"),
    )
    conn.commit()

    # Trophy milestones
    trophy_signals = heartbeat.detect_trophy_milestones(conn=conn)
    assert len(trophy_signals) == 1

    # Arena changes
    arena_signals = heartbeat.detect_arena_changes(conn=conn)
    assert len(arena_signals) == 1

    # Role changes
    role_signals = heartbeat.detect_role_changes(conn=conn)
    assert len(role_signals) == 1

    # Total across all detectors
    all_signals = trophy_signals + arena_signals + role_signals
    assert len(all_signals) == 3
