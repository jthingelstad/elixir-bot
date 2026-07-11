"""The awareness read surfaces today's member "cake days" for the WHOLE Chicago
day (not the one-tick delta window), ACTIVE members only, so the brain reliably
celebrates them once. Covers runtime/awareness/read.py:_cake_days_today."""
from __future__ import annotations

import db
from runtime.awareness.read import build_read

NOW = "2026-07-01T00:00:00Z"


def _insert_cake_event(conn, *, event_type, subject_tag, dedup_key, observed_at, payload_json):
    conn.execute(
        "INSERT INTO clan_events (dedup_key, event_type, clan_tag, subject_tag, "
        "observed_at, payload_json, created_at) VALUES (?, ?, '#J2RGCRVG', ?, ?, ?, ?)",
        (dedup_key, event_type, subject_tag, observed_at, payload_json, observed_at),
    )


def _member(conn, tag, name, *, left_at=None):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)", (tag, name, NOW, NOW))
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source) "
        "VALUES (?, '2026-03-01', ?, 'test')", (tag, left_at))


def test_cake_days_today_surfaces_active_members_all_day(engine_conn):
    today = db.chicago_today()
    # Stamped at the very start of the day: cake_days_today is date-based, NOT
    # gated on last_tick_at, so an event from hours ago still surfaces.
    stamp = f"{today}T00:00:01Z"

    engine_conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (NOW,))
    _member(engine_conn, "#A", "Al")                       # active
    _member(engine_conn, "#B", "Bo", left_at="2026-06-20")  # departed

    _insert_cake_event(
        engine_conn, event_type="member_birthday", subject_tag="#A",
        dedup_key=f"member_birthday:#A:{today}", observed_at=stamp,
        payload_json='{"name": "Al"}')
    _insert_cake_event(
        engine_conn, event_type="cr_account_anniversary", subject_tag="#A",
        dedup_key="cr_account_anniversary:#A:5", observed_at=stamp,
        payload_json='{"name": "Al", "years": 5}')
    _insert_cake_event(  # departed member — must be excluded
        engine_conn, event_type="join_anniversary", subject_tag="#B",
        dedup_key=f"join_anniversary:#B:{today}", observed_at=stamp,
        payload_json='{"name": "Bo", "months": 12, "is_annual": true}')
    _insert_cake_event(  # clan-wide, null subject — always included
        engine_conn, event_type="clan_birthday", subject_tag=None,
        dedup_key=f"clan_birthday:{today}", observed_at=stamp,
        payload_json='{"years": 1}')
    engine_conn.commit()

    read = build_read(conn=engine_conn)
    cake = read["cake_days_today"]
    assert "cake_days_today" not in read.get("_degraded", [])

    present = {(c["type"], c["subject_tag"]) for c in cake}
    assert ("member_birthday", "#A") in present
    assert ("cr_account_anniversary", "#A") in present
    assert ("clan_birthday", None) in present
    # Departed member is never surfaced.
    assert all(c["subject_tag"] != "#B" for c in cake)

    birthday = next(c for c in cake if c["type"] == "member_birthday")
    assert birthday["signal_key"] == f"member_birthday:#A:{today}"
    assert birthday["name"] == "Al"
    cr = next(c for c in cake if c["type"] == "cr_account_anniversary")
    assert cr["years"] == 5


def test_cake_days_today_empty_when_none_today(engine_conn):
    engine_conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (NOW,))
    _member(engine_conn, "#A", "Al")
    # A cake event stamped on a DIFFERENT day must not surface today.
    _insert_cake_event(
        engine_conn, event_type="member_birthday", subject_tag="#A",
        dedup_key="member_birthday:#A:2001-01-01", observed_at="2001-01-01T12:00:00Z",
        payload_json='{"name": "Al"}')
    engine_conn.commit()

    read = build_read(conn=engine_conn)
    assert read["cake_days_today"] == []
    assert "cake_days_today" not in read.get("_degraded", [])
