#!/usr/bin/env python3
"""Compact elixir.db: purge expired rows, then VACUUM to return space to disk.

Run this during a maintenance window with the bot stopped — VACUUM needs an
exclusive lock and rewrites the whole file (needs ~2x the DB size in free disk
temporarily). Opening the project `db` package also applies any pending
migrations (e.g. the war_current_state collapse and the player_profile_snapshots
cards_json cleanup), so the one-time historical reduction happens here too.

    venv/bin/python scripts/db_compact.py            # purge + vacuum
    venv/bin/python scripts/db_compact.py --purge-only
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


def _size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1024 / 1024
    except OSError:
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge expired rows and VACUUM elixir.db.")
    parser.add_argument("--purge-only", action="store_true", help="Skip VACUUM (no file shrink).")
    args = parser.parse_args()

    path = db.DB_PATH
    before = _size_mb(path)
    print(f"Database: {path}")
    print(f"Size before: {before:,.0f} MB")

    # Opening a connection runs pending migrations (one-time historical cleanup).
    conn = db.get_connection()
    conn.close()

    stats = db.purge_old_data()
    deleted = {table: n for table, n in sorted(stats.items()) if n}
    if deleted:
        print("Purged rows:")
        for table, count in deleted.items():
            print(f"  {table}: {count:,}")
    else:
        print("Purged rows: none expired")

    if args.purge_only:
        print("Skipping VACUUM (--purge-only). Freed pages will be reused, file size unchanged.")
        return

    # VACUUM must run outside any transaction, so use a dedicated autocommit
    # connection. It needs exclusive access — if the bot is still running this
    # will raise "database is locked".
    vac = sqlite3.connect(path, isolation_level=None)
    try:
        vac.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print("Running VACUUM (this can take a while on a large DB)...")
        vac.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        print(f"VACUUM failed: {exc}")
        print("Is the bot still running? Stop it and retry — VACUUM needs an exclusive lock.")
        sys.exit(1)
    finally:
        vac.close()

    after = _size_mb(path)
    print(f"Size after: {after:,.0f} MB  (reclaimed {before - after:,.0f} MB)")


if __name__ == "__main__":
    main()
