#!/usr/bin/env python3
"""Recover pre-v16 battle decks from the rolling backup archive (#216).

`scripts/backfill_opponent_decks.py` recovers what the LIVE database still
holds, but `raw_api_payloads` is deliberately short-retention, so it could only
reach back about two weeks. The battles older than that are not gone: every
nightly backup in ~/elixir-backups froze that window as it stood on its own
date, so the union of the backups covers far more history than any single
snapshot.

This mines them. For each backup, in date order, it pulls the
`player_battlelog` payloads and keeps only the battles that are still missing a
deck in the live DB, then stops early once nothing is left to find.

Backups are opened READ-ONLY from a scratch copy and deleted immediately after,
so the archive itself is never touched and only one is on disk at a time.

Not every backup has the table: the older `elixir-v5-memory-*` and
`elixir-v5-events-*` families are different databases, and pre-cut schemas
differ. Those are skipped rather than treated as failures.

Usage:
    uv run --locked python scripts/backfill_opponent_decks_from_backups.py
    uv run --locked python scripts/backfill_opponent_decks_from_backups.py --apply
    ... [--backup-dir DIR] [--limit N]
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backfill_opponent_decks import _recover  # noqa: E402

DEFAULT_BACKUP_DIR = Path.home() / "elixir-backups"


def _arg(flag: str, default: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _missing_keys(conn) -> set[tuple[str, str]]:
    return {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT player_tag, battle_time FROM battle_events WHERE opponent_deck_json IS NULL"
        )
    }


def _mine(path: Path, wanted: set[tuple[str, str]], stage: Path) -> dict:
    """Extract decks for `wanted` battles from one gzipped backup."""
    db_file = stage / "backup.db"
    try:
        with gzip.open(path, "rb") as src, open(db_file, "wb") as dst:
            shutil.copyfileobj(src, dst, length=8 << 20)
    except OSError, EOFError, gzip.BadGzipFile:
        return {}

    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("SELECT 1 FROM raw_api_payloads LIMIT 1")
        except sqlite3.DatabaseError:
            return {}  # different family (memory/events) or pre-cut schema
        found = _recover(conn)
        return {k: v for k, v in found.items() if k in wanted and v[1]}
    except sqlite3.DatabaseError:
        return {}
    finally:
        try:
            conn.close()
        except sqlite3.Error, NameError:
            pass
        db_file.unlink(missing_ok=True)


def main() -> int:
    apply = "--apply" in sys.argv
    backup_dir = Path(_arg("--backup-dir", str(DEFAULT_BACKUP_DIR))).expanduser()
    limit = int(_arg("--limit", "0"))

    import db

    conn = db.get_connection()
    wanted = _missing_keys(conn)
    print(f"battles still missing an opponent deck: {len(wanted)}")
    if not wanted:
        print("nothing to do")
        return 0

    backups = sorted(backup_dir.glob("*.db.gz"), reverse=True)
    if limit:
        backups = backups[:limit]
    print(f"backups to scan: {len(backups)} from {backup_dir}\n")

    recovered: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    stage = Path(tempfile.mkdtemp(prefix="elixir-backup-mine-"))
    try:
        for i, path in enumerate(backups, 1):
            still = wanted - recovered.keys()
            if not still:
                print(f"\nall gaps filled after {i - 1} backups — stopping early")
                break
            hits = _mine(path, still, stage)
            recovered.update(hits)
            if hits:
                print(
                    f"  [{i:3}/{len(backups)}] {path.name:44} +{len(hits):5}"
                    f"  (total {len(recovered)}/{len(wanted)})"
                )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\nrecovered {len(recovered)} of {len(wanted)} missing battles")
    if not apply:
        conn.rollback()
        conn.close()
        print("dry run — pass --apply to write")
        return 0

    conn.executemany(
        "UPDATE battle_events SET deck_json = COALESCE(?, deck_json), opponent_deck_json = ? "
        "WHERE player_tag = ? AND battle_time = ? AND opponent_deck_json IS NULL",
        [(own, opp, tag, bt) for (tag, bt), (own, opp) in recovered.items()],
    )
    conn.commit()
    total, filled = conn.execute(
        "SELECT COUNT(*), COUNT(opponent_deck_json) FROM battle_events"
    ).fetchone()
    print(f"applied. battle_events with an opponent deck: {filled} of {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
