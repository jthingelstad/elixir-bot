"""One-time fix: normalize T14 seed rows' claimed_at to ISO format.

The migration's calendar seeds copied CR-compact timestamps
('20260628T120000.000Z') into recognition_ledger.claimed_at while live claims
write ISO-Z — compact sorts above ISO lexicographically, mis-bucketing
time-ordered queries (found by the 2026-07-04 live audit; catalogued in
normalize.md). Idempotent: rows already ISO are untouched.

Usage: ./venv/bin/python scripts/migrate_v51/fix_ledger_seed_ts.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from engine.normalize import parse_cr_time  # noqa: E402


def main() -> int:
    path = os.getenv("ELIXIR_DB_PATH", "elixir-v51.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        """SELECT recognition_key, claimed_at FROM recognition_ledger
           WHERE claimed_at NOT LIKE '____-__-__T%'"""
    ).fetchall()
    fixed = 0
    for r in rows:
        dt = parse_cr_time(r["claimed_at"])
        if dt is None:
            print(f"SKIP unparseable: {r['recognition_key']} {r['claimed_at']}")
            continue
        conn.execute(
            "UPDATE recognition_ledger SET claimed_at = ? WHERE recognition_key = ?",
            (dt.strftime("%Y-%m-%dT%H:%M:%SZ"), r["recognition_key"]),
        )
        fixed += 1
    conn.commit()
    print(f"normalized {fixed} claimed_at value(s); {'clean' if not rows else 'done'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
