"""One-time teammate_tag backfill (ranked-and-profiles.md D3).

Replays the raw battlelog buffer (14-day retention) through the ingest
extractor and fills battle_events.teammate_tag where it is NULL — rescuing
the 2v2 duo data that predates the column. Idempotent: rows already filled
are left alone; re-running is a no-op.

Usage: ./venv/bin/python scripts/migrate_v51/backfill_teammates.py [db_path]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from engine.ingest import extract_battles  # noqa: E402


def main(db_path: str | None = None) -> None:
    db_path = db_path or os.getenv("ELIXIR_DB_PATH", "elixir-v51.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(battle_events)")]
    if "teammate_tag" not in cols:
        conn.execute("ALTER TABLE battle_events ADD COLUMN teammate_tag TEXT")
        print("added battle_events.teammate_tag")
    payloads = conn.execute(
        """SELECT entity_key, payload_json FROM raw_api_payloads
           WHERE endpoint = 'player_battlelog' ORDER BY fetched_at"""
    ).fetchall()
    filled = 0
    for row in payloads:
        try:
            battlelog = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        for bt in extract_battles(row["entity_key"], battlelog or []):
            if not bt.get("teammate_tag"):
                continue
            dedup = f"{bt['player_tag']}:{bt['battle_time']}:{bt['opponent_tag']}"
            cur = conn.execute(
                "UPDATE battle_events SET teammate_tag = ? "
                "WHERE dedup_key = ? AND teammate_tag IS NULL",
                (bt["teammate_tag"], dedup),
            )
            filled += cur.rowcount
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) FROM battle_events WHERE teammate_tag IS NOT NULL"
    ).fetchone()[0]
    print(f"backfilled {filled} rows; battle_events with teammate: {total}")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
