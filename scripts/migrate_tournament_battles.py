#!/usr/bin/env python3
"""Fold `tournament_battles` into `battle_events` before dropping it (#216).

`tournament_battles` existed for one reason: it stored BOTH decks, and
`battle_events` stored only the polled player's. v16 fixed that, so the table is
now a second copy of a record the main stream already keeps -- except for 7 rows
from 2026-04-18, which predate `battle_events` entirely (it starts 2026-05-07)
and would be lost outright.

Those rows carry `raw_json`: the complete original API payload. So this does not
translate columns, it REPLAYS them through the production ingest path. The
replayed rows come out strictly richer than the table held -- they gain support
cards, elixir leaked and tower HP, none of which `tournament_battles` had
columns for. Using `mirror_battles` rather than a hand-written INSERT keeps the
dedup key, war keys and column set defined in exactly one place.

THE TRAP, and why the polled player comes from `raw_json` and not `player1_tag`:
`tournament_battles` normalized its player1/player2 ordering, so on 4 of the 7
rows `player1_tag` is the OPPONENT of the player whose battle log the payload
came from. `extract_battles` falls back to `team[0]` when the tag it is handed
is not on the team -- which silently files one player's deck, crowns and elixir
under the other player's tag. The authoritative perspective is `team[0]` in the
payload itself; `player1_tag` is a presentation choice made by the old writer.

Idempotent, and self-correcting: a first run that used `player1_tag` wrote rows
under the wrong tag, and `--apply` removes exactly those before re-inserting.

Usage:
    uv run --locked python scripts/migrate_tournament_battles.py           # dry run
    uv run --locked python scripts/migrate_tournament_battles.py --apply
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.ingest import mirror_battles  # noqa: E402
from engine.normalize import canon_tag  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv

    import db

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT battle_time, player1_tag, raw_json FROM tournament_battles ORDER BY battle_time"
        ).fetchall()
    except Exception:
        print("tournament_battles is already gone — nothing to migrate")
        conn.close()
        return 0

    print(f"tournament_battles rows: {len(rows)}")
    plan: list[tuple[str, str, dict]] = []  # (polled_tag, battle_time, payload)
    bad: list[tuple[str, str]] = []  # rows a previous run filed under the wrong tag
    skipped = already = 0

    for row in rows:
        if not row["raw_json"]:
            skipped += 1
            print(f"  {row['battle_time']}  NO raw_json — cannot replay")
            continue
        try:
            payload = json.loads(row["raw_json"])
        except json.JSONDecodeError, TypeError, ValueError:
            skipped += 1
            continue
        team0 = (payload.get("team") or [{}])[0]
        polled = canon_tag(team0.get("tag") or "")
        if not polled:
            skipped += 1
            continue
        stored = canon_tag(row["player1_tag"] or "")
        if stored and stored != polled:
            # player1_tag is the opponent here. A previous run would have written
            # this battle under `stored` with team[0]'s facts — wrong player.
            bad.append((row["battle_time"], stored))
        present = conn.execute(
            "SELECT COUNT(*) FROM battle_events WHERE battle_time = ? AND player_tag = ?",
            (row["battle_time"], polled),
        ).fetchone()[0]
        if present:
            already += 1
            continue
        plan.append((polled, row["battle_time"], payload))

    print(f"  correctly present already: {already}")
    print(f"  to replay:                 {len(plan)}")
    print(f"  unrecoverable:             {skipped}")
    if bad:
        misfiled = [
            b
            for b in bad
            if conn.execute(
                "SELECT COUNT(*) FROM battle_events WHERE battle_time = ? AND player_tag = ?",
                b,
            ).fetchone()[0]
        ]
        print(
            f"  misfiled rows to remove:   {len(misfiled)} (wrong player_tag from an earlier run)"
        )
        for bt, tag in misfiled:
            print(f"      {bt}  filed under {tag}")
    else:
        misfiled = []

    if not apply:
        conn.rollback()
        conn.close()
        print("\ndry run — pass --apply to write")
        return 0

    for bt, tag in misfiled:
        conn.execute(
            "DELETE FROM battle_events WHERE battle_time = ? AND player_tag = ?", (bt, tag)
        )
    for polled, battle_time, payload in plan:
        # observed_at is the battle's own time: these come from an archive, so
        # claiming we saw them now would be a lie.
        mirror_battles(conn, polled, [payload], battle_time, None)
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM battle_events WHERE tournament_tag IS NOT NULL"
    ).fetchone()[0]
    print(f"\napplied. battle_events rows carrying a tournament_tag: {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
