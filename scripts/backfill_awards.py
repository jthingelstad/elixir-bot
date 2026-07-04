"""RETIRED at the v5.1 cut (2026-07-03).

This script backfilled season awards via heartbeat._awards.backfill_season,
which was deleted with the Gen C engine (docs/v5.1/migration.md Phase 4).
Awards now fire on the war stream's season_closed event (Q5) and live in the
durable `awards` table of elixir-v51.db; historical rows were carried by
migration transform T6. The cold archive (elixir-v5-archive-2026H2.db) holds
the pre-cut state if a manual backfill is ever needed again.
"""

import sys

print(__doc__)
sys.exit(1)
