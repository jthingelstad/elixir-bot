#!/usr/bin/env python3
"""Backfill the game-level stream's durable record (game_events).

Reconstructs the history of Clash Royale changes we can already see — every
card in the catalog, and every event / non-mastery event-badge the API sentinel
has first-seen — as `backfilled=1` game_events. Backfilled rows are the RECORD
only: the recognizer skips them, so nothing is (re-)announced. Their value is
giving Elixir a historical corpus for consistent voice ("the last new card
before this was …") and seeding the novelty baseline so only changes appearing
AFTER the backfill are treated as new.

It also seeds the live cursors to the current tips (recognize:game →
game_events head, emit:game → sentinel head) so the stream starts clean.

    ./venv/bin/python scripts/backfill_game_events.py --dry-run
    ./venv/bin/python scripts/backfill_game_events.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from engine.db import cursor_set  # noqa: E402
from engine.normalize import humanize_badge  # noqa: E402
from storage import game_events as ge  # noqa: E402
from storage._formatting import preferred_display_name  # noqa: E402


def _sentinel_rows(conn, sentinel_type):
    return conn.execute(
        "SELECT * FROM api_sentinel_observations WHERE sentinel_type = ? "
        "ORDER BY observation_id", (sentinel_type,)
    ).fetchall()


def _sample(row):
    try:
        return json.loads(row["sample_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _plan(conn) -> list[dict]:
    """Every history row we'd record, as (dedup_key, event_type, change_key,
    observed_at, subject_tag, payload). Pure — no writes.

    NOTE: this deliberately does NOT seed `card_added` for the existing card
    catalog. A `card_added` event means "this card is NEW to the game"; emitting
    one per pre-existing card (the original bug — 126 bogus events on
    2026-07-07, only Ronin genuinely new) floods the stream and misleads the
    brain into thinking the whole catalog just released. New cards are detected
    going forward by the daily catalog sync (storage.card_catalog: only fires
    for card_ids not already known). The catalog table itself is the history."""
    plan: list[dict] = []
    for e in _sentinel_rows(conn, "event"):
        s = _sample(e)
        tag = e["name"]
        plan.append({
            "dedup_key": f"event_started:{tag}", "event_type": "event_started",
            "change_key": f"event:{tag}", "observed_at": e["first_seen_at"],
            "subject_tag": None,
            "payload": {"event_type": "event_started", "event_tag": tag,
                        "title": s.get("title") or tag, "description": s.get("description")},
        })
    for b in _sentinel_rows(conn, "badge_name"):
        name = b["name"] or ""
        if not name or name.startswith("Mastery"):
            continue
        s = _sample(b)
        badge = s.get("badge") or {}
        entity = (b["first_entity_key"] if "first_entity_key" in b.keys()
                  and b["first_entity_key"] else b["entity_key"])
        subject_tag = f"#{entity.lstrip('#')}" if entity else None
        member_name = preferred_display_name(conn, subject_tag) if subject_tag else None
        if member_name in (None, "", subject_tag):
            member_name = None
        plan.append({
            "dedup_key": f"event_badge_earned:{name}", "event_type": "event_badge_earned",
            "change_key": f"badge:{name}", "observed_at": b["first_seen_at"],
            "subject_tag": subject_tag,
            "payload": {
                "event_type": "event_badge_earned", "badge_name": name,
                "badge_label": humanize_badge(name), "member_name": member_name,
                "member_tag": subject_tag,
                "image_url": (badge.get("iconUrls") or {}).get("large"),
            },
        })
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="print the plan, write nothing (default)")
    g.add_argument("--apply", action="store_true", help="persist backfilled rows + seed cursors")
    args = ap.parse_args()

    conn = db.get_connection()
    ge.ensure_schema(conn)
    plan = _plan(conn)
    by_type: dict[str, int] = {}
    for row in plan:
        by_type[row["event_type"]] = by_type.get(row["event_type"], 0) + 1

    print(f"game_events backfill plan: {len(plan)} rows")
    for et in ("card_added", "event_started", "event_badge_earned"):
        print(f"  {et}: {by_type.get(et, 0)}")
    # show the event badges (the interesting, attributable ones)
    for row in plan:
        if row["event_type"] == "event_badge_earned":
            p = row["payload"]
            print(f"    badge {p['badge_label']!r} -> {p['member_name'] or '(unattributed)'}")

    if not args.apply:
        print("\n(dry-run — nothing written; re-run with --apply)")
        conn.close()
        return 0

    written = 0
    for row in plan:
        written += ge.insert_game_event(
            conn, dedup_key=row["dedup_key"], event_type=row["event_type"],
            change_key=row["change_key"], observed_at=row["observed_at"],
            payload=row["payload"], subject_tag=row["subject_tag"], backfilled=True,
        )
    # Seed the live cursors to the current tips so nothing pre-existing announces.
    game_head = conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM game_events").fetchone()[0]
    sentinel_head = conn.execute(
        "SELECT COALESCE(MAX(observation_id), 0) FROM api_sentinel_observations"
    ).fetchone()[0]
    cursor_set(conn, "recognize:game", game_head)
    cursor_set(conn, "emit:game", sentinel_head)
    conn.commit()
    conn.close()
    print(f"\napplied: {written} new backfilled row(s); "
          f"cursors seeded (recognize:game={game_head}, emit:game={sentinel_head})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
