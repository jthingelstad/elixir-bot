#!/usr/bin/env python3
"""One-time backfill of players.display_name (normalize-at-source rollout).

Materializes the injection-safe display name for every existing player from
their current_name + preferred_nickname. Idempotent — safe to re-run; the
lazy column-ensure + ingest keep it fresh afterward.

    python scripts/backfill_display_names.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import db  # noqa: E402
from engine.db import _ensure_display_name_column, refresh_display_name  # noqa: E402


def main() -> int:
    conn = db.get_connection()
    try:
        _ensure_display_name_column(conn)
        tags = [r[0] for r in conn.execute("SELECT player_tag FROM players")]
        for tag in tags:
            refresh_display_name(conn, tag)
        conn.commit()
        changed = conn.execute(
            "SELECT current_name, display_name FROM players "
            "WHERE COALESCE(current_name,'') != COALESCE(display_name,'') "
            "ORDER BY current_name"
        ).fetchall()
        print(f"backfilled {len(tags)} players; {len(changed)} normalized:")
        for r in changed:
            print(f"  {r['current_name']!r:26} -> {r['display_name']!r}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
