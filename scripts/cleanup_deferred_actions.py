"""One-off cleanup: retire the Defer action (2026-07-10).

Defer was removed from the leader-action HITL flow (see docs / management.md:
declining is now the only "not now" and the engine re-nominates on sustained
evidence, not a leader-set clock). Any rows still carrying the legacy
``deferred`` status are migrated to their decline equivalent:

  * ``decision_cases``  deferred -> dismissed  — stops the stale "due" nagging
    the awareness read surfaced forever (Loop #26 named five such cases).
  * ``leader_action_recommendations`` deferred -> rejected — history reads as
    the declines they effectively were.

Idempotent: re-running finds nothing to migrate. ``--dry-run`` only reports.

Run against the live DB with ELIXIR_DB_PATH pointed at it, e.g.:
    ELIXIR_DB_PATH=elixir-v51.db ./venv/bin/python scripts/cleanup_deferred_actions.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy 'deferred' rows to their decline equivalents."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report counts without writing."
    )
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        cases = conn.execute(
            "SELECT case_id, target_player_name, case_type FROM decision_cases "
            "WHERE status = 'deferred' ORDER BY case_id"
        ).fetchall()
        actions_n = conn.execute(
            "SELECT COUNT(*) FROM leader_action_recommendations WHERE status = 'deferred'"
        ).fetchone()[0]

        print(f"decision_cases deferred: {len(cases)}")
        for row in cases:
            print(
                f"  case #{row['case_id']} {row['target_player_name']} ({row['case_type']})"
            )
        print(f"leader_action_recommendations deferred: {actions_n}")

        if args.dry_run:
            print("dry-run: no changes written.")
            return 0
        if not cases and not actions_n:
            print("nothing to migrate.")
            return 0

        now = db._utcnow()
        conn.execute(
            "UPDATE decision_cases "
            "SET status = 'dismissed', resolved_at = ?, due_at = NULL, updated_at = ?, "
            "    resolution = COALESCE(resolution, "
            "        'Defer retired — dismissed; engine re-nominates on new evidence.') "
            "WHERE status = 'deferred'",
            (now, now),
        )
        conn.execute(
            "UPDATE leader_action_recommendations SET status = 'rejected', updated_at = ? "
            "WHERE status = 'deferred'",
            (now,),
        )
        conn.commit()
        print(
            f"migrated {len(cases)} case(s) -> dismissed, {actions_n} action(s) -> rejected."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
