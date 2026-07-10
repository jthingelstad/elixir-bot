"""Regression: the game-events backfill must NOT emit a `card_added` per catalog
card. That bug produced 126 bogus "new card" events on 2026-07-07 (only Ronin was
genuinely new), flooding the stream and confusing the brain. New cards are
detected going forward by the daily catalog sync, not seeded from history."""

import importlib.util
import os

import db

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "backfill_game_events.py")


def _load():
    spec = importlib.util.spec_from_file_location("bge_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_plan_emits_no_card_added_even_with_full_catalog():
    conn = db.get_connection()
    try:
        # Seed real catalog cards — a buggy plan would emit a card_added for each.
        for cid, name in ((26000000, "Knight"), (26000106, "Ronin")):
            conn.execute(
                "INSERT OR IGNORE INTO card_catalog (card_id, name, rarity, synced_at) "
                "VALUES (?, ?, 'common', '2026-01-01')", (cid, name))
        conn.commit()

        plan = _load()._plan(conn)
    finally:
        conn.close()

    card_events = [p for p in plan if p["event_type"] == "card_added"]
    assert card_events == [], "backfill must not seed card_added from the catalog"
