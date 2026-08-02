"""The awareness read is delta-first: player/clan/war signals are only what's
NEW since the last tick (no rolling-window flood, no backfilled seed rows), while
game events are standing background context (recent cards/events) so a new card is
known for weeks, not one tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from db import managed_connection
from storage import events_read

# Inside a rolling list_recent_events(days=30) window, so a literal date is a fuse:
# it passes until it ages out and then fails forever. Anchor to "now" instead.
_RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


@managed_connection
def _seed_player_event(dedup, event_type, observed_at, *, backfilled=0, conn=None):
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at, timing, "
        "scope, payload_json, created_at) VALUES (?, ?, '#P1', ?, 'exact', 'public', '{}', ?)",
        (dedup, event_type, observed_at, observed_at),
    )
    conn.commit()


@managed_connection
def _seed_game_event(dedup, event_type, observed_at, payload, *, backfilled=0, conn=None):
    from storage import game_events as ge

    ge.insert_game_event(
        conn,
        dedup_key=dedup,
        event_type=event_type,
        change_key=dedup,
        observed_at=observed_at,
        payload=payload,
        backfilled=bool(backfilled),
    )
    conn.commit()


def test_list_recent_events_can_include_game_and_drop_backfilled():
    _seed_game_event(
        "card_added:9",
        "card_added",
        _RECENT,
        {"name": "Ronin"},
        backfilled=1,
    )  # seeded history
    _seed_game_event(
        "event_started:x",
        "event_started",
        _RECENT,
        {"title": "Live Event"},
        backfilled=0,
    )  # real

    # Default streams never include game.
    default = events_read.list_recent_events(days=30)
    assert not any(e["stream"] == "game" for e in default)

    # Opt-in to game, dropping backfilled → only the real event, not the seed card.
    got = events_read.list_recent_events(days=30, streams=("game",), exclude_backfilled=True)
    types = {e["event_type"] for e in got}
    assert types == {"event_started"}
    assert all(e["timing"] is None for e in got)  # game has no `timing` column


def test_signals_are_since_last_tick_not_a_window():
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    # A prior thought fixes the cursor; an event before it is NOT new, one after IS.
    store.persist_thought({"t": 1}, {"posts": []})
    last = store.last_tick_at()
    assert last  # cursor is set from the persisted thought
    _seed_player_event("old:1", "badge_earned", "2026-01-01T00:00:00Z")  # long before cursor
    _seed_player_event("new:1", "badge_earned", "2999-01-01T00:00:00Z")  # after the cursor

    conn = db.get_connection()
    try:
        sig = read_mod._signals(conn)
    finally:
        conn.close()
    keys = {e["dedup_key"] for e in sig}
    assert "new:1" in keys  # after the last tick → surfaced
    assert "old:1" not in keys  # before the last tick → not re-shown


def test_game_context_keeps_recent_card_as_background_with_is_new():
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    store.persist_thought({"t": 1}, {"posts": []})  # sets the cursor
    _seed_game_event(
        "card_added:ronin",
        "card_added",
        "2999-01-02T00:00:00Z",
        {"name": "Ronin", "rarity": "legendary", "elixir_cost": 5},
    )

    conn = db.get_connection()
    try:
        gc = read_mod._game_context(conn)
    finally:
        conn.close()
    cards = gc["recent_cards"]
    assert any(c["name"] == "Ronin" for c in cards)
    ronin = next(c for c in cards if c["name"] == "Ronin")
    assert ronin["is_new"] is True  # first seen after the last tick
    assert ronin["rarity"] == "legendary"


def _seed_many_player_events(conn, count: int, *, prefix: str = "burst", start_hours_ago=0):
    conn.execute("PRAGMA foreign_keys=OFF")
    base = datetime.now(timezone.utc) - timedelta(hours=start_hours_ago)
    conn.executemany(
        "INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at, timing, "
        "scope, payload_json, created_at) VALUES (?, 'badge_earned', '#P1', ?, "
        "'exact', 'public', '{}', ?)",
        [
            (
                f"{prefix}:{i:03d}",
                (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            for i in range(count)
        ],
    )


def test_awareness_backlog_is_oldest_first_and_drains_past_batch_limit(engine_conn):
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    positions, initialized = store.ensure_event_cursors(engine_conn)
    assert initialized is True
    assert positions["player"] == 0
    _seed_many_player_events(engine_conn, 100)

    first = read_mod._pending_event_batch(engine_conn)
    assert [e["dedup_key"] for e in first["events"]] == [f"burst:{i:03d}" for i in range(80)]
    assert first["has_more"] is True

    store.persist_thought(
        {"batch": 1},
        {"posts": [], "skipped_reason": "reviewed"},
        cursor_positions=first["checkpoints"],
        conn=engine_conn,
    )
    second = read_mod._pending_event_batch(engine_conn)
    assert [e["dedup_key"] for e in second["events"]] == [f"burst:{i:03d}" for i in range(80, 100)]
    assert second["has_more"] is False


def test_awareness_cursor_sees_old_inserted_event_after_long_outage(engine_conn):
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    store.ensure_event_cursors(engine_conn)
    _seed_many_player_events(engine_conn, 1, prefix="old-but-new", start_hours_ago=48)

    pending = read_mod._pending_event_batch(engine_conn)
    assert [e["dedup_key"] for e in pending["events"]] == ["old-but-new:000"]


def test_failed_awareness_thought_does_not_advance_event_positions(engine_conn):
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    before, _ = store.ensure_event_cursors(engine_conn)
    _seed_many_player_events(engine_conn, 2, prefix="retry")
    pending = read_mod._pending_event_batch(engine_conn)

    store.persist_thought(
        {"batch": 1},
        {"_error": {"kind": "delivery", "detail": "boom"}},
        cursor_positions=pending["checkpoints"],
        conn=engine_conn,
    )
    assert store.event_cursor_positions(conn=engine_conn) == before
    retry = read_mod._pending_event_batch(engine_conn)
    assert [e["dedup_key"] for e in retry["events"]] == ["retry:000", "retry:001"]
