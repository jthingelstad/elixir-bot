"""One-time backfill: player_current_state.exp_level / clan_rank /
donations_received_week from the live roster baseline.

The roster-refresh path read raw CR camelCase keys from the snake_case
projection until 2026-07-04, so these columns stayed 0/NULL for members
whose rows predate the fix (battery finding: "average level is 16" —
an average over zeros). Forward writes are already correct; this
repairs the standing rows. Idempotent.

Usage: uv run python scripts/migrate_v51/fix_current_state_backfill.py [db_path]
"""

import json
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "elixir-v51.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=30000")

# Profile baselines are the ONLY live source: the CR clan memberList
# returns expLevel: 0 for every member (dead field at the API — see the
# normalize.md catalog row added with this script).
rows = conn.execute(
    "SELECT entity_tag, payload_json FROM state_baselines "
    "WHERE entity_kind='player' AND aspect='profile'"
).fetchall()
updated = 0
for row in rows:
    profile = json.loads(row["payload_json"]) or {}
    exp = profile.get("exp_level")
    if not exp:
        continue
    cur = conn.execute(
        "SELECT exp_level FROM player_current_state WHERE player_tag = ?",
        (row["entity_tag"],),
    ).fetchone()
    if cur is None or (cur["exp_level"] or 0) > 0:
        continue
    conn.execute(
        "UPDATE player_current_state SET exp_level = ? WHERE player_tag = ?",
        (exp, row["entity_tag"]),
    )
    updated += 1

conn.commit()
healthy = conn.execute(
    "SELECT COUNT(*) FROM player_current_state pcs "
    "JOIN clan_memberships cm ON cm.player_tag = pcs.player_tag AND cm.left_at IS NULL "
    "WHERE COALESCE(pcs.exp_level, 0) > 0"
).fetchone()[0]
print(f"backfilled {updated} rows; active members with real exp_level: {healthy}")
conn.close()
