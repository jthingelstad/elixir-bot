#!/usr/bin/env python
"""Backfill battle-grain events into game_event_stream from member_battle_facts.

The event stream began recording live battle telemetry only going forward, so
historical battles already captured as facts are not yet in the stream. This
script projects every existing ``member_battle_facts`` row into the stream as a
``battle``-class event, with the mode family attached.

Idempotent: each ``event_key`` derives from the same dedupe tuple as the battle
fact, so re-running — including on the live DB after more battles accumulate —
never double-inserts. Validate on a DB copy first, then run on production after
a backup.

Usage:
    venv/bin/python scripts/backfill_battle_events.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402
from storage.event_stream import BATTLE_EVENT_CLASS, record_battle_event  # noqa: E402
from storage.game_modes import classify_battle_mode  # noqa: E402


_SELECT_SQL = """
    SELECT
        m.player_tag AS player_tag,
        f.battle_time, f.battle_type, f.game_mode_id, f.game_mode_name,
        f.deck_selection, f.event_tag, f.tournament_tag, f.is_hosted_match,
        f.outcome, f.crowns_for, f.crowns_against, f.trophy_change,
        f.league_number, f.arena_name, f.opponent_name, f.opponent_tag,
        f.opponent_clan_tag
    FROM member_battle_facts f
    JOIN members m ON m.member_id = f.member_id
    WHERE m.player_tag IS NOT NULL AND f.battle_time IS NOT NULL
    ORDER BY f.battle_time ASC
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="max facts to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="classify + count only; write nothing")
    args = parser.parse_args()

    conn = db.get_connection()
    before = conn.execute(
        "SELECT COUNT(*) FROM game_event_stream WHERE event_class = ?",
        (BATTLE_EVENT_CLASS,),
    ).fetchone()[0]

    rows = conn.execute(_SELECT_SQL).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    print(f"battle facts to process: {total} (existing battle events: {before})")

    mode_counts: dict[str, int] = {}
    written = 0
    start = time.time()
    for i, row in enumerate(rows, 1):
        mode = classify_battle_mode(
            battle_type=row["battle_type"],
            game_mode_id=row["game_mode_id"],
            game_mode_name=row["game_mode_name"],
            deck_selection=row["deck_selection"],
            event_tag=row["event_tag"],
            tournament_tag=row["tournament_tag"],
            is_hosted_match=row["is_hosted_match"],
        )
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        if not args.dry_run:
            record_battle_event(
                member_tag=row["player_tag"],
                battle_time=row["battle_time"],
                mode_group=mode,
                battle_type=row["battle_type"],
                game_mode_name=row["game_mode_name"],
                outcome=row["outcome"],
                crowns_for=row["crowns_for"],
                crowns_against=row["crowns_against"],
                trophy_change=row["trophy_change"],
                league_number=row["league_number"],
                arena_name=row["arena_name"],
                opponent_name=row["opponent_name"],
                opponent_tag=row["opponent_tag"],
                opponent_clan_tag=row["opponent_clan_tag"],
                conn=conn,
            )
            written += 1
        if i % 1000 == 0:
            print(f"  {i}/{total} ({time.time() - start:.1f}s)")

    after = conn.execute(
        "SELECT COUNT(*) FROM game_event_stream WHERE event_class = ?",
        (BATTLE_EVENT_CLASS,),
    ).fetchone()[0]
    dist = ", ".join(f"{k}={v}" for k, v in sorted(mode_counts.items(), key=lambda kv: -kv[1]))
    print(f"\nmode distribution: {dist}")
    print(
        f"battle events: {before} -> {after} (+{after - before}); "
        f"processed {total}, wrote {written} in {time.time() - start:.1f}s"
    )
    if args.dry_run:
        print("dry-run: no rows written")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
