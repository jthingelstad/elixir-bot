"""Backfill the durable ``awards`` ledger: member war awards are POINTS, not fame.

A member earns war POINTS; only the CLAN earns fame. The 2026-07-11 rename
(65cd6bf) fixed the live war-race read paths and the go-forward award writers
(they now emit ``metric_unit='points'``), but it never backfilled the award rows
written BEFORE it. Those legacy rows still carry ``metric_unit='fame'`` on
member-level war awards (war_champ / war_participant / free_pass / rookie_mvp),
and war_champ rows carry ``avg_fame`` in their metadata.

Because Elixir echoes a field's own unit, any goodbye or retrospective citing a
pre-rename member war award prints "fame" — e.g. Atternam's #186 goodbye said
"12,300 fame across 4 races" (his Season 132 War Champ, award_id 133). This
one-time, label-only backfill closes that leak.

Default mode is a transactional dry-run. Pass ``--apply`` to commit. Idempotent:
safe to re-run (a second run finds zero rows to change). See
[[elixir-member-points-not-fame]].
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db

# The only award types whose value is a member's war contribution (POINTS).
# donation_champ=donations, iron_king=battle_days, pol_champ=rating are correct
# as-is and are deliberately untouched.
MEMBER_WAR_AWARD_TYPES = ("war_champ", "war_participant", "free_pass", "rookie_mvp")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=os.getenv("ELIXIR_DB_PATH", "elixir-v51.db"), help="DB path"
    )
    parser.add_argument(
        "--apply", action="store_true", help="commit (default is a dry-run rollback)"
    )
    args = parser.parse_args()

    conn = db.get_connection(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        # Guard: every row still labeled 'fame' must be a member-war award. If an
        # unexpected award_type ever carries 'fame', abort rather than mislabel it.
        stray = conn.execute(
            "SELECT DISTINCT award_type FROM awards "
            "WHERE metric_unit = 'fame' AND award_type NOT IN "
            "(?, ?, ?, ?)",
            MEMBER_WAR_AWARD_TYPES,
        ).fetchall()
        if stray:
            raise SystemExit(
                f"ABORT: unexpected award_type(s) with metric_unit='fame': "
                f"{[r['award_type'] for r in stray]} — review before backfilling."
            )

        before = {
            r["award_type"]: r["n"]
            for r in conn.execute(
                "SELECT award_type, COUNT(*) AS n FROM awards "
                "WHERE metric_unit = 'fame' GROUP BY award_type"
            ).fetchall()
        }
        meta_rows = conn.execute(
            "SELECT award_id, metadata_json FROM awards "
            "WHERE metric_unit = 'fame' AND metadata_json LIKE '%avg_fame%'"
        ).fetchall()

        print("=== before ===")
        for t in MEMBER_WAR_AWARD_TYPES:
            print(f"  {t:16} metric_unit='fame' rows: {before.get(t, 0)}")
        print(f"  rows with avg_fame in metadata: {len(meta_rows)}")

        # 1) metric_unit: fame -> points (member-war awards only).
        unit_changed = conn.execute(
            "UPDATE awards SET metric_unit = 'points' WHERE metric_unit = 'fame'"
        ).rowcount

        # 2) metadata_json: rename the avg_fame key -> avg_points (value unchanged).
        meta_changed = 0
        for row in meta_rows:
            meta = json.loads(row["metadata_json"])
            if "avg_fame" in meta:
                # Preserve key order: rebuild with avg_fame renamed in place.
                meta = {
                    ("avg_points" if k == "avg_fame" else k): v for k, v in meta.items()
                }
                conn.execute(
                    "UPDATE awards SET metadata_json = ? WHERE award_id = ?",
                    (json.dumps(meta), row["award_id"]),
                )
                meta_changed += 1

        # Post-conditions: no member-war award may remain labeled 'fame', and no
        # avg_fame key may survive anywhere in the ledger.
        remaining_unit = conn.execute(
            "SELECT COUNT(*) FROM awards WHERE metric_unit = 'fame'"
        ).fetchone()[0]
        remaining_meta = conn.execute(
            "SELECT COUNT(*) FROM awards WHERE metadata_json LIKE '%avg_fame%'"
        ).fetchone()[0]

        print("=== change ===")
        print(f"  metric_unit fame->points: {unit_changed} rows")
        print(f"  metadata avg_fame->avg_points: {meta_changed} rows")
        print("=== after ===")
        print(f"  metric_unit='fame' remaining: {remaining_unit}")
        print(f"  avg_fame in metadata remaining: {remaining_meta}")

        assert remaining_unit == 0, "post-condition failed: 'fame' units remain"
        assert remaining_meta == 0, "post-condition failed: avg_fame keys remain"

        if args.apply:
            conn.commit()
            print("APPLIED (committed).")
        else:
            conn.rollback()
            print("DRY-RUN (rolled back). Re-run with --apply to commit.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
