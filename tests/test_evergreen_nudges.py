"""Evergreen nudges: quiet-period gating, rate cap, cooldown, rotation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from storage.evergreen_nudges import (
    SEED_NUDGES,
    due_nudge,
    ensure_schema,
    is_quiet_period,
    mark_nudge_sent,
)

NOW = datetime(2026, 7, 6, 18, 0, 0, tzinfo=timezone.utc)
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt):
    return dt.strftime(_ISO)


def _public_post(conn, fulfilled_at, key="k1"):
    # recognition_key is nullable (FK to recognition_ledger) — omit it; the quiet
    # check only reads scope + fulfilled_at.
    conn.execute(
        "INSERT INTO communication_intents (intent_type, lane, scope, payload_json, "
        "status, attempts, created_at, expires_at, fulfilled_at) "
        "VALUES ('celebrate', 'member-highlights', 'public', '{}', 'fulfilled', 0, ?, ?, ?)",
        (fulfilled_at, fulfilled_at, fulfilled_at),
    )
    conn.commit()


def test_seed_populates_inventory(engine_conn):
    ensure_schema(engine_conn)
    n = engine_conn.execute("SELECT COUNT(*) FROM evergreen_nudges").fetchone()[0]
    assert n == len(SEED_NUDGES)
    keys = {r[0] for r in engine_conn.execute("SELECT nudge_key FROM evergreen_nudges")}
    assert "discord_invite" in keys


def test_quiet_period_detection(engine_conn):
    c = engine_conn
    ensure_schema(c)
    assert is_quiet_period(c, NOW) is True  # no posts at all → quiet
    _public_post(c, _iso(NOW - timedelta(days=1)), key="recent")
    assert is_quiet_period(c, NOW, quiet_days=3) is False  # posted yesterday
    c.execute("DELETE FROM communication_intents")
    _public_post(c, _iso(NOW - timedelta(days=5)), key="old")
    c.commit()
    assert is_quiet_period(c, NOW, quiet_days=3) is True  # last post 5d ago


def test_due_nudge_picks_never_sent(engine_conn):
    c = engine_conn
    ensure_schema(c)
    item = due_nudge(c, NOW)
    assert item is not None and item["nudge_key"] in {n["nudge_key"] for n in SEED_NUDGES}
    assert "forbidden_terms" in item  # parsed for the composer


def test_global_rate_cap(engine_conn):
    c = engine_conn
    ensure_schema(c)
    mark_nudge_sent(c, "discord_invite", NOW - timedelta(days=2))  # a nudge 2d ago
    c.commit()
    assert due_nudge(c, NOW, global_cap_days=7) is None  # within the 7d cap
    assert due_nudge(c, NOW + timedelta(days=8), global_cap_days=7) is not None


def test_per_item_cooldown_and_rotation(engine_conn):
    c = engine_conn
    ensure_schema(c)
    # discord_invite sent 40d ago (past its 30d cooldown), poap_faq 10d ago (within)
    c.execute("UPDATE evergreen_nudges SET last_sent_at=? WHERE nudge_key='discord_invite'",
              (_iso(NOW - timedelta(days=40)),))
    c.execute("UPDATE evergreen_nudges SET last_sent_at=? WHERE nudge_key='poap_faq'",
              (_iso(NOW - timedelta(days=10)),))
    c.commit()
    # global cap counts from the most recent send (poap_faq, 10d ago) → past 7d, ok.
    item = due_nudge(c, NOW)
    assert item is not None
    assert item["nudge_key"] != "poap_faq", "still in cooldown"
    # oldest-sent-first: website_home is never-sent → sorts before discord_invite (40d)
    assert item["nudge_key"] == "website_home"


def test_pending_card_blocks(engine_conn):
    c = engine_conn
    ensure_schema(c)
    from db import create_leader_action_recommendation
    create_leader_action_recommendation(
        action_type="in_game_relay", objective="clan_nudge",
        prompt_text="x", source_signal_key="evergreen_nudge:test",
        source_signal_type="evergreen_nudge", conn=c,
    )
    c.commit()
    assert due_nudge(c, NOW) is None  # a proposed nudge card already pending
