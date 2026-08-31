"""Backfill war_weeks.our_fame from war_week_clans (boat fame, not point sums).

The #166 finalizer (b9a88a8e, 2026-07-06) wrote our_fame = max(participation
point sum, snapshot fame). Points keep accruing after the boat crosses the
finish line, so every early-finish week since then stored a 3-4x inflated
"fame" (e.g. S135 w3: 28,400 points recorded for a 10,246-fame week), and the
chronicle fed the brain a fake season-long fame slide. The emitter is fixed;
this repairs the durable rows from war_week_clans, whose fame column always
held the true API snapshot.

Only rows where our_fame differs from our clan's war_week_clans fame are
touched, and only downward (a point sum is always >= the boat fame it
shadowed). Dry-run by default; pass --apply to write. Honors ELIXIR_DB_PATH.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

OUR_TAG = "#J2RGCRVG"
DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "elixir-v51.db"
)

FIND = """
SELECT w.season_id, w.section_index, w.period_type, w.our_fame, c.fame AS boat_fame
FROM war_weeks w
JOIN war_week_clans c
  ON c.season_id = w.season_id AND c.section_index = w.section_index AND c.clan_tag = ?
WHERE w.our_fame IS NOT NULL AND c.fame IS NOT NULL AND w.our_fame > c.fame
ORDER BY w.season_id, w.section_index
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the fix (default: dry run)")
    args = ap.parse_args()

    db_path = os.environ.get("ELIXIR_DB_PATH", DEFAULT_DB)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(FIND, (OUR_TAG,)).fetchall()
    if not rows:
        print(f"{db_path}: no contaminated rows — nothing to do")
        return 0

    for r in rows:
        print(
            f"S{r['season_id']} w{r['section_index']} ({r['period_type']}): "
            f"our_fame {r['our_fame']} -> {r['boat_fame']}"
        )
    if not args.apply:
        print(f"\nDRY RUN: {len(rows)} row(s) would change in {db_path}. Re-run with --apply.")
        return 0

    with conn:
        for r in rows:
            conn.execute(
                "UPDATE war_weeks SET our_fame = ? WHERE season_id = ? AND section_index = ?",
                (r["boat_fame"], r["season_id"], r["section_index"]),
            )
    left = conn.execute(FIND, (OUR_TAG,)).fetchall()
    print(f"\nAPPLIED: {len(rows)} row(s) updated in {db_path}; {len(left)} still divergent.")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
