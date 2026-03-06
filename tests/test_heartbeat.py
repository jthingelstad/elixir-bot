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


def test_war_day_detection_thursday(conn):
    """Thursday triggers war_day_start signal."""
    thursday = datetime(2026, 3, 5, 10, 0)  # March 5, 2026 is Thursday
    signals = heartbeat.detect_war_day_transition(now=thursday, conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_day_start"


def test_war_day_detection_sunday(conn):
    """Sunday triggers war_day_end signal."""
    sunday = datetime(2026, 3, 8, 10, 0)  # March 8, 2026 is Sunday
    signals = heartbeat.detect_war_day_transition(now=sunday, conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_day_end"


def test_war_day_detection_wednesday(conn):
    """Wednesday (training day) produces no signal."""
    wednesday = datetime(2026, 3, 4, 10, 0)  # March 4, 2026 is Wednesday
    signals = heartbeat.detect_war_day_transition(now=wednesday, conn=conn)
    assert len(signals) == 0


def test_war_day_detection_friday(conn):
    """Friday is a regular battle day."""
    friday = datetime(2026, 3, 6, 10, 0)
    signals = heartbeat.detect_war_day_transition(now=friday, conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "war_battle_day"


def test_war_day_transition_dedup(conn):
    """War day signal only fires once per day."""
    thursday = datetime(2026, 3, 5, 10, 0)
    signals1 = heartbeat.detect_war_day_transition(now=thursday, conn=conn)
    assert len(signals1) == 1
    db.mark_signal_sent("war_day_start", "2026-03-05", conn=conn)
    signals2 = heartbeat.detect_war_day_transition(now=thursday, conn=conn)
    assert len(signals2) == 0


def test_detect_donation_leaders(conn):
    """Top 3 donors are identified."""
    members = [
        {"name": "A", "donations": 150},
        {"name": "B", "donations": 100},
        {"name": "C", "donations": 50},
        {"name": "D", "donations": 10},
    ]
    signals = heartbeat.detect_donation_leaders(members, conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "donation_leaders"
    leaders = signals[0]["leaders"]
    assert len(leaders) == 3
    assert leaders[0]["name"] == "A"
    assert leaders[0]["donations"] == 150


def test_detect_donation_leaders_no_donations(conn):
    """No signal when nobody has donated."""
    members = [{"name": "A", "donations": 0}]
    signals = heartbeat.detect_donation_leaders(members, conn=conn)
    assert len(signals) == 0


def test_detect_donation_leaders_dedup(conn):
    """Donation leaders signal only fires once per day."""
    members = [{"name": "A", "donations": 150}]
    signals1 = heartbeat.detect_donation_leaders(members, conn=conn)
    assert len(signals1) == 1
    db.mark_signal_sent("donation_leaders", datetime.now().strftime("%Y-%m-%d"), conn=conn)
    signals2 = heartbeat.detect_donation_leaders(members, conn=conn)
    assert len(signals2) == 0


def test_detect_inactivity(conn):
    """Flags members not seen in 3+ days."""
    now = datetime(2026, 3, 10, 12, 0)
    members = [
        {"name": "Active", "tag": "#A", "lastSeen": "20260310T100000.000Z", "role": "member"},
        {"name": "Inactive", "tag": "#B", "lastSeen": "20260305T100000.000Z", "role": "member"},
        {"name": "VeryInactive", "tag": "#C", "lastSeen": "20260301T100000.000Z", "role": "member"},
    ]
    signals = heartbeat.detect_inactivity(members, now=now, conn=conn)
    assert len(signals) == 1
    assert signals[0]["type"] == "inactive_members"
    names = [m["name"] for m in signals[0]["members"]]
    assert "Inactive" in names
    assert "VeryInactive" in names
    assert "Active" not in names
    # Sorted by most inactive first
    assert signals[0]["members"][0]["name"] == "VeryInactive"


def test_detect_inactivity_dedup(conn):
    """Inactivity signal only fires once per day."""
    now = datetime(2026, 3, 10, 12, 0)
    members = [
        {"name": "Inactive", "tag": "#B", "lastSeen": "20260305T100000.000Z", "role": "member"},
    ]
    signals1 = heartbeat.detect_inactivity(members, now=now, conn=conn)
    assert len(signals1) == 1
    db.mark_signal_sent("inactive_members", "2026-03-10", conn=conn)
    signals2 = heartbeat.detect_inactivity(members, now=now, conn=conn)
    assert len(signals2) == 0


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


def test_detect_war_deck_usage_battle_day(conn):
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
        signals = heartbeat.detect_war_deck_usage(war_data, conn=conn)

    assert len(signals) == 1
    sig = signals[0]
    assert sig["type"] == "war_deck_usage"
    assert len(sig["used_all_4"]) == 1
    assert sig["used_all_4"][0]["name"] == "King Levy"
    assert len(sig["used_some"]) == 1
    assert sig["used_some"][0]["name"] == "Vijay"
    assert len(sig["used_none"]) == 1
    assert sig["used_none"][0]["name"] == "Newbie"


def test_detect_war_deck_usage_training_day(conn):
    """No war deck signal on training days."""
    war_data = {"state": "warDay", "clan": {"participants": []}}
    # Wednesday = training day
    with patch("heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 3, 4, 18, 0)  # Wednesday
        signals = heartbeat.detect_war_deck_usage(war_data, conn=conn)

    assert len(signals) == 0


def test_detect_war_deck_usage_no_war(conn):
    """No signal when not in war."""
    signals = heartbeat.detect_war_deck_usage({"state": "notInWar"}, conn=conn)
    assert len(signals) == 0


def test_detect_war_deck_usage_dedup(conn):
    """War deck usage signal only fires once per day."""
    war_data = {
        "state": "warDay",
        "clan": {
            "participants": [
                {"name": "King Levy", "tag": "#ABC", "decksUsedToday": 4},
            ]
        }
    }
    with patch("heartbeat.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 3, 5, 18, 0)  # Thursday
        mock_dt.utcnow = datetime.utcnow
        mock_dt.strptime = datetime.strptime
        signals1 = heartbeat.detect_war_deck_usage(war_data, conn=conn)
        assert len(signals1) == 1
        db.mark_signal_sent("war_deck_usage", "2026-03-05", conn=conn)
        signals2 = heartbeat.detect_war_deck_usage(war_data, conn=conn)
        assert len(signals2) == 0


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


def test_detect_cake_days_clan_birthday(conn):
    """Clan birthday detected on Feb 4."""
    signals = heartbeat.detect_cake_days(today_str="2027-02-04", conn=conn)
    clan_bday = [s for s in signals if s["type"] == "clan_birthday"]
    assert len(clan_bday) == 1
    assert clan_bday[0]["years"] == 1


def test_detect_cake_days_join_anniversary(conn):
    """Join anniversary detected for member."""
    db.set_member_join_date("#ABC", "King Levy", "2025-03-05", conn=conn)
    signals = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
    anniv = [s for s in signals if s["type"] == "join_anniversary"]
    assert len(anniv) == 1
    assert anniv[0]["members"][0]["name"] == "King Levy"
    assert anniv[0]["members"][0]["years"] == 1


def test_detect_cake_days_member_birthday(conn):
    """Member birthday detected."""
    db.set_member_birthday("#ABC", "King Levy", 3, 5, conn=conn)
    signals = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
    bday = [s for s in signals if s["type"] == "member_birthday"]
    assert len(bday) == 1
    assert bday[0]["members"][0]["name"] == "King Levy"


def test_detect_cake_days_dedup(conn):
    """Second call on same day returns empty (dedup)."""
    db.set_member_birthday("#ABC", "King Levy", 3, 5, conn=conn)
    signals1 = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
    assert len([s for s in signals1 if s["type"] == "member_birthday"]) == 1

    signals2 = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
    assert len([s for s in signals2 if s["type"] == "member_birthday"]) == 0


def test_detect_cake_days_leave_resets_anniversary(conn):
    """Leaving the clan clears join date so rejoin gets a fresh tenure."""
    # Member joins and gets a join date
    db.set_member_join_date("#ABC", "King Levy", "2025-03-05", conn=conn)

    # Verify anniversary would fire
    signals = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
    assert len([s for s in signals if s["type"] == "join_anniversary"]) == 1

    # Member leaves — tenure is cleared
    db.clear_member_tenure("#ABC", conn=conn)

    # Anniversary no longer fires
    signals = heartbeat.detect_cake_days(today_str="2027-03-05", conn=conn)
    assert len([s for s in signals if s["type"] == "join_anniversary"]) == 0

    # Member rejoins with fresh date
    db.record_join_date("#ABC", "King Levy", "2027-01-15", conn=conn)
    dates = db.get_member_dates("#ABC", conn=conn)
    assert dates["joined_date"] == "2027-01-15"


def test_detect_cake_days_no_match(conn):
    """No signals when nothing matches today."""
    db.set_member_birthday("#ABC", "King Levy", 7, 15, conn=conn)
    signals = heartbeat.detect_cake_days(today_str="2026-03-05", conn=conn)
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


def test_tick_returns_bundle_with_clan_and_war(conn):
    """tick returns HeartbeatTickResult including fetched clan/war payloads."""
    clan = {
        "memberList": [
            {"tag": "#ABC", "name": "King Levy", "trophies": 9000, "donations": 10, "role": "member"}
        ]
    }
    war = {"state": "warDay", "clan": {"participants": []}}

    with (
        patch("heartbeat.cr_api.get_clan", return_value=clan),
        patch("heartbeat.cr_api.get_current_war", return_value=war),
        patch("heartbeat.detect_joins_leaves", return_value=([], {})),
        patch("heartbeat.detect_trophy_milestones", return_value=[]),
        patch("heartbeat.detect_arena_changes", return_value=[]),
        patch("heartbeat.detect_role_changes", return_value=[]),
        patch("heartbeat.detect_war_day_transition", return_value=[]),
        patch("heartbeat.detect_war_deck_usage", return_value=[]),
        patch("heartbeat.detect_war_completion", return_value=[]),
        patch("heartbeat.detect_war_champ_update", return_value=[]),
        patch("heartbeat.detect_donation_leaders", return_value=[]),
        patch("heartbeat.detect_inactivity", return_value=[]),
        patch("heartbeat.detect_cake_days", return_value=[]),
        patch("heartbeat.db.backfill_join_dates"),
        patch("heartbeat.db.purge_old_data"),
        patch("heartbeat.db.snapshot_members"),
        patch("heartbeat.db.mark_signal_sent"),
    ):
        result = heartbeat.tick(conn=conn)

    assert isinstance(result, heartbeat.HeartbeatTickResult)
    assert result.clan == clan
    assert result.war == war
    assert result.signals == []
