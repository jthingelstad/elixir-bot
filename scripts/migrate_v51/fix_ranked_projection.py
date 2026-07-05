"""One-time backfill: player_current_state ranked columns from the ranked
baselines. The projection read leagueStatistics.currentSeason.trophies (trophy
road!) as the Ranked rating, so migrated values sat frozen and wrong
(rehearsal 2026-07-04: OllieTurtle "rating 14,000"; Atternam rating NULL).
The projection source is fixed in engine/projections.py; this repairs the
stored rows immediately instead of waiting a poll cycle. Idempotent.

Usage: ./venv/bin/python scripts/migrate_v51/fix_ranked_projection.py [db_path]
"""

import json
import sqlite3
import sys


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "elixir-v51.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    fixed = 0
    rows = conn.execute(
        "SELECT entity_tag, payload_json FROM state_baselines "
        "WHERE entity_kind='player' AND aspect='ranked'"
    ).fetchall()
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        # Ranked aspect shape: current is TOP-LEVEL (league/rank/trophies),
        # with last/best nested (D6 extension).
        league = p.get("league")
        rating = p.get("trophies")
        if league is None:
            continue
        cur = conn.execute(
            "UPDATE player_current_state SET ranked_league = ?, ranked_trophies = ? "
            "WHERE player_tag = ?",
            (league, rating, r["entity_tag"]),
        )
        fixed += cur.rowcount
    # Members with NO ranked baseline keep stale migrated values — null them
    # (they are not currently in Ranked; frozen PoL-era numbers mislead).
    nulled = conn.execute(
        "UPDATE player_current_state SET ranked_league = NULL, ranked_trophies = NULL "
        "WHERE player_tag NOT IN (SELECT entity_tag FROM state_baselines "
        "WHERE entity_kind='player' AND aspect='ranked') "
        "AND (ranked_league IS NOT NULL OR ranked_trophies IS NOT NULL)"
    ).rowcount
    conn.commit()
    print(f"backfilled {fixed} from ranked baselines; nulled {nulled} stale rows")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
