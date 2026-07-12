"""Departure verification: leaders confirm LEAVE vs KICK, the public goodbye is
held until verified, and the leader's answer is the authoritative signal."""
from __future__ import annotations

from storage.cases import (
    _departure_was_kick,
    expire_departure_verification_cards,
    raise_departure_verification_cards,
)
from storage.leader_actions import classify_departure
from engine.emitters.clan import emit_verified_leave_events

NOW = "2026-07-12T12:00:00Z"


def _seed_departure(conn, tag="#A", name="Alice", left_at="2026-07-12T11:00:00Z",
                    joined_at="2026-05-01", leave_source="roster_diff"):
    conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, "
                 "is_home) VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)", (left_at,))
    conn.execute("INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
                 "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                 (tag, name, name, joined_at, left_at))
    conn.execute("INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, left_at, "
                 "join_source, leave_source) VALUES (?, '#J2RGCRVG', ?, ?, 'test', ?)",
                 (tag, joined_at, left_at, leave_source))
    conn.commit()


def _seed_done_kick(conn, tag="#A", decided_at="2026-07-12T10:00:00Z"):
    conn.execute(
        "INSERT INTO leader_action_recommendations (action_key, action_type, objective, "
        "prompt_text, status, target_player_tag, proposed_at, decided_at, created_at, updated_at) "
        "VALUES (?, 'kick_recommendation', 'kick', 'kick', 'done', ?, ?, ?, ?, ?)",
        (f"kick:{tag}", tag, decided_at, decided_at, decided_at, decided_at),
    )
    conn.commit()


def _open_departure_card(conn, tag="#A"):
    return conn.execute(
        "SELECT * FROM leader_action_recommendations WHERE action_type='departure_verification' "
        "AND target_player_tag=? AND status='proposed'", (tag,)).fetchone()


def _leave_source(conn, tag="#A"):
    return conn.execute("SELECT leave_source FROM clan_memberships WHERE player_tag=? "
                        "ORDER BY membership_id DESC LIMIT 1", (tag,)).fetchone()[0]


def test_card_raised_for_ambiguous_departure(engine_conn, _isolate_default_sqlite_db):
    _seed_departure(engine_conn)
    raised = raise_departure_verification_cards(now=NOW, conn=engine_conn)
    assert len(raised) == 1 and raised[0]["player_tag"] == "#A"
    card = _open_departure_card(engine_conn)
    assert card is not None
    # idempotent — a second pass raises nothing
    assert raise_departure_verification_cards(now=NOW, conn=engine_conn) == []


def test_confirmed_kick_settles_silently_no_card(engine_conn, _isolate_default_sqlite_db):
    _seed_departure(engine_conn)
    _seed_done_kick(engine_conn)  # a done kick card already explains the departure
    raised = raise_departure_verification_cards(now=NOW, conn=engine_conn)
    assert raised == []  # no card — already a known kick
    assert _open_departure_card(engine_conn) is None
    assert _leave_source(engine_conn) == "leader_verified_kick"


def test_classify_leave_writes_authoritative_source_and_memory(engine_conn, _isolate_default_sqlite_db):
    from memory_store import ensure_memory_schema
    ensure_memory_schema(engine_conn)
    _seed_departure(engine_conn)
    raise_departure_verification_cards(now=NOW, conn=engine_conn)
    card = _open_departure_card(engine_conn)
    updated = classify_departure(card["action_id"], classification="leave",
                                 discord_user_id=42, comment="left for a friend's clan", conn=engine_conn)
    assert updated["status"] == "done"
    assert _leave_source(engine_conn) == "leader_verified_leave"
    mem = engine_conn.execute("SELECT body, kind, scope FROM memories WHERE member_tag='#A'").fetchone()
    assert mem is not None and mem[1] == "leader_note" and mem[2] == "leadership"
    assert "friend's clan" in mem[0]


def test_classify_kick_sets_verified_kick(engine_conn, _isolate_default_sqlite_db):
    _seed_departure(engine_conn)
    raise_departure_verification_cards(now=NOW, conn=engine_conn)
    card = _open_departure_card(engine_conn)
    classify_departure(card["action_id"], classification="kick", discord_user_id=42, conn=engine_conn)
    assert _leave_source(engine_conn) == "leader_verified_kick"


def test_departure_was_kick_prefers_verified_source(engine_conn, _isolate_default_sqlite_db):
    # A done kick card would infer "kick", but a leader-verified LEAVE overrides it.
    _seed_departure(engine_conn, leave_source="leader_verified_leave")
    _seed_done_kick(engine_conn)
    assert _departure_was_kick(engine_conn, "#A", "2026-07-12T11:00:00Z") is False
    engine_conn.execute("UPDATE clan_memberships SET leave_source='leader_verified_kick' WHERE player_tag='#A'")
    assert _departure_was_kick(engine_conn, "#A", "2026-07-12T11:00:00Z") is True


def test_verified_leave_emits_event_kick_does_not(engine_conn, _isolate_default_sqlite_db):
    _seed_departure(engine_conn, leave_source="leader_verified_leave")
    n = emit_verified_leave_events(engine_conn, "#J2RGCRVG", NOW)
    assert n == 1
    ev = engine_conn.execute("SELECT event_type FROM clan_events WHERE subject_tag='#A' "
                             "AND event_type='member_left_verified'").fetchone()
    assert ev is not None
    # a confirmed kick emits nothing
    _seed_departure(engine_conn, tag="#B", name="Bob", leave_source="leader_verified_kick")
    engine_conn.commit()
    emit_verified_leave_events(engine_conn, "#J2RGCRVG", NOW)
    assert engine_conn.execute("SELECT 1 FROM clan_events WHERE subject_tag='#B'").fetchone() is None


def test_unanswered_card_times_out_to_leave(engine_conn, _isolate_default_sqlite_db):
    _seed_departure(engine_conn, left_at="2026-07-01T00:00:00Z")  # old departure
    # raise a card dated in the past so it's beyond the timeout
    engine_conn.execute(
        "INSERT INTO leader_action_recommendations (action_key, action_type, objective, "
        "prompt_text, status, target_player_tag, proposed_at, created_at, updated_at) "
        "VALUES ('dep:#A', 'departure_verification', 'confirm', 'confirm', 'proposed', '#A', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')")
    engine_conn.commit()
    expired = expire_departure_verification_cards(now=NOW, conn=engine_conn)
    assert len(expired) == 1
    assert _leave_source(engine_conn) == "leave_unverified"
    assert _open_departure_card(engine_conn) is None
