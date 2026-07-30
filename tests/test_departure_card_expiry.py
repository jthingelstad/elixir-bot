"""The departure auto-settle path, which has never run in production.

`expire_departure_verification_cards` closes a departure card the leaders did
not answer within `_DEPARTURE_CARD_TIMEOUT_DAYS`, and — the part that matters —
writes `clan_memberships.leave_source = 'leave_unverified'` so the member's
departure stops looking unclassified forever.

Zero rows in the live database carry `decided_by_discord_user_id = 'system'`,
so this has never fired: all six departure cards to date were leader-verified
within 58 hours. It is wired into the engine tick and reports a counter every
ten minutes, which reads as coverage without being any.

Nothing here is speculative: each test drives the real function against a real
database and asserts the two writes it is responsible for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
from storage.leader_actions import (
    _DEPARTURE_CARD_TIMEOUT_DAYS,
    expire_departure_verification_cards,
)


@pytest.fixture
def departure_db(tmp_path, monkeypatch):
    path = str(tmp_path / "departures.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        yield conn
    finally:
        conn.close()


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _departed_member(conn, tag: str, *, days_ago: float):
    """A member who left and whose departure is still unclassified."""
    left_at = _stamp(days_ago)
    joined_at = _stamp(days_ago + 30)
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)",
        (left_at,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
        (tag, tag.lstrip("#"), tag.lstrip("#"), joined_at, left_at),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, left_at, "
        "join_source, leave_source) VALUES (?, '#J2RGCRVG', ?, ?, 'test', 'roster_diff')",
        (tag, joined_at, left_at),
    )


def _departure_card(conn, tag: str, *, days_ago: float):
    action = db.create_leader_action_recommendation(
        action_type="departure_verification",
        objective="verify departure",
        prompt_text=f"Did {tag} leave or get kicked?",
        rationale="roster diff",
        target_player_tag=tag,
        source_signal_key=f"engine:departure:{tag}",
        source_signal_type="test",
        action_key=f"engine:departure:{tag}",
        conn=conn,
    )
    conn.execute(
        "UPDATE leader_action_recommendations SET proposed_at = ? WHERE action_id = ?",
        (_stamp(days_ago), action["action_id"]),
    )
    conn.commit()
    return action["action_id"]


def _leave_source(conn, tag: str) -> str | None:
    row = conn.execute(
        "SELECT leave_source FROM clan_memberships WHERE UPPER(player_tag) = UPPER(?) "
        "ORDER BY left_at DESC LIMIT 1",
        (tag,),
    ).fetchone()
    return row["leave_source"] if row else None


def test_an_unanswered_card_past_the_window_auto_settles(departure_db):
    _departed_member(departure_db, "#STALE", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 2)
    action_id = _departure_card(departure_db, "#STALE", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 2)

    expired = expire_departure_verification_cards(conn=departure_db)

    assert [row["action_id"] for row in expired] == [action_id]
    card = db.get_leader_action_by_id(action_id, conn=departure_db)
    assert card["status"] == db.ACTION_DONE
    assert str(card["decided_by_discord_user_id"]) == "system"
    assert card["decision_emoji"] == "⌛"
    assert "organic leave" in (card["decision_note"] or "")


def test_auto_settling_records_the_departure_as_unverified(departure_db):
    """The membership write is the whole point: without it the member's
    departure stays 'roster_diff' and reads as an open question forever."""
    _departed_member(departure_db, "#UNV", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 1)
    _departure_card(departure_db, "#UNV", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 1)
    assert _leave_source(departure_db, "#UNV") == "roster_diff"

    expire_departure_verification_cards(conn=departure_db)

    assert _leave_source(departure_db, "#UNV") == "leave_unverified"


def test_a_card_inside_the_window_is_left_for_the_leaders(departure_db):
    _departed_member(departure_db, "#FRESH", days_ago=0.5)
    action_id = _departure_card(departure_db, "#FRESH", days_ago=0.5)

    assert expire_departure_verification_cards(conn=departure_db) == []
    card = db.get_leader_action_by_id(action_id, conn=departure_db)
    assert card["status"] == db.ACTION_PROPOSED
    assert _leave_source(departure_db, "#FRESH") == "roster_diff"


def test_auto_settling_never_overrides_a_leader_verdict(departure_db):
    """A card the leader already classified must not be reopened or relabelled,
    however old it is."""
    _departed_member(departure_db, "#JUDGED", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 10)
    action_id = _departure_card(departure_db, "#JUDGED", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 10)
    db.classify_departure(action_id, classification="kick", discord_user_id=42, conn=departure_db)
    departure_db.commit()

    assert expire_departure_verification_cards(conn=departure_db) == []
    card = db.get_leader_action_by_id(action_id, conn=departure_db)
    assert str(card["decided_by_discord_user_id"]) == "42"
    assert _leave_source(departure_db, "#JUDGED") == "leader_verified_kick"


def test_settling_is_not_repeated_on_the_next_tick(departure_db):
    """The engine runs this every ten minutes. A settled card must drop out."""
    _departed_member(departure_db, "#ONCE", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 3)
    _departure_card(departure_db, "#ONCE", days_ago=_DEPARTURE_CARD_TIMEOUT_DAYS + 3)

    assert len(expire_departure_verification_cards(conn=departure_db)) == 1
    assert expire_departure_verification_cards(conn=departure_db) == []
