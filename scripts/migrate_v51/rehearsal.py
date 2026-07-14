"""v5.1 offline rehearsal harness (docs/v5.1/migration.md Phase 0 item 4).

Replays the raw-payload window (14 days, `raw_api_payloads`) through the new
engine's offline entry point into a throwaway DB — exercising
emit → project → recognize → ledger with zero Discord and zero API calls.
Precedent: the offline-rehearsal seam in `event_core/live/tick.py:25–28`.

The engine seam
---------------
The harness feeds payloads, oldest first, to:

    engine.offline.OfflineEngine(db_path)
        .apply(endpoint, entity_key, payload_json, fetched_at)
        .finish() -> dict of counters

That module lands in migration Phase 4 (the new `engine/` package). Until it
exists, run `--census` mode, which iterates the exact same replay stream and
reports what a rehearsal will process — proving the iteration, ordering, and
window math against real data today.

Acceptance rule (migration.md Phase 6, "Recognition rehearsal"): a full replay
produces ZERO duplicate ledger claims and no first-sight flood (first-day
event volume comparable to a normal day, not a backfill spike).

Usage:
    ./venv/bin/python scripts/migrate_v51/rehearsal.py --census --source elixir-v5.db
    ./venv/bin/python scripts/migrate_v51/rehearsal.py --source elixir-v5-archive-2026H2.db \
        --target /tmp/rehearsal.db
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys

# C3: the archive stores riverracelog under the legacy alias. The rehearsal
# feeds the new engine the TRUE endpoint name, same as the post-cut raw log.
ENDPOINT_RENAMES = {"clan_war_log": "riverracelog"}


def _connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def replay_stream(source: sqlite3.Connection, days: int):
    """Yield (endpoint, entity_key, payload_json, fetched_at) oldest-first.

    Oldest-first is load-bearing: the emitters must see states in observation
    order or every diff is wrong.
    """
    rows = source.execute(
        """SELECT endpoint, entity_key, payload_json, fetched_at
           FROM raw_api_payloads
           WHERE fetched_at >= datetime('now', ?)
           ORDER BY fetched_at ASC, payload_id ASC""",
        (f"-{days} days",),
    )
    for r in rows:
        endpoint = ENDPOINT_RENAMES.get(r["endpoint"], r["endpoint"])
        yield endpoint, r["entity_key"], r["payload_json"], r["fetched_at"]


def run_census(source_path: str, days: int) -> int:
    source = _connect_ro(source_path)
    per_endpoint = collections.Counter()
    per_day = collections.Counter()
    entities = collections.defaultdict(set)
    total = 0
    first = last = None
    for endpoint, entity_key, _payload, fetched_at in replay_stream(source, days):
        total += 1
        per_endpoint[endpoint] += 1
        per_day[fetched_at[:10]] += 1
        entities[endpoint].add(entity_key)
        first = first or fetched_at
        last = fetched_at
    print(f"replay window: last {days} days — {total} payloads, {first} → {last}")
    print("\nper endpoint (payloads / distinct entities):")
    for endpoint, n in per_endpoint.most_common():
        print(f"  {endpoint:22s} {n:6d} / {len(entities[endpoint])}")
    print("\nper day:")
    for day in sorted(per_day):
        print(f"  {day}  {per_day[day]:5d}")
    if "clan_war_log" in per_endpoint:
        print("\nWARNING: legacy alias leaked through ENDPOINT_RENAMES — fix the map.")
        return 1
    return 0


def run_rehearsal(source_path: str, target_path: str, days: int) -> int:
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        from engine.offline import OfflineEngine
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("engine"):
            print(
                "engine.offline is not built yet (migration Phase 4).\n"
                "Run with --census to validate the replay stream today.",
                file=sys.stderr,
            )
            return 2
        raise

    source = _connect_ro(source_path)
    eng = OfflineEngine(target_path)
    n = 0
    for endpoint, entity_key, payload_json, fetched_at in replay_stream(source, days):
        eng.apply(endpoint, entity_key, payload_json, fetched_at)
        n += 1
    # Historical migration rehearsal explicitly exercises the retired
    # deterministic recognizer. Normal replay mirrors production and leaves
    # this off; see scripts/replay_gate.py.
    counters = eng.finish(legacy_proactive=True)
    print(f"replayed {n} payloads into {target_path}")
    for key, val in sorted(counters.items()):
        print(f"  {key}: {val}")

    # Acceptance probes (Phase 6 "Recognition rehearsal" row)
    tdb = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True)
    dupes = tdb.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT recognition_key) FROM recognition_ledger"
    ).fetchone()[0]
    print(f"duplicate ledger claims: {dupes} (must be 0)")
    return 0 if dupes == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="elixir-v5.db", help="DB holding raw_api_payloads")
    parser.add_argument("--target", default="/tmp/elixir-v51-rehearsal.db")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--census", action="store_true", help="report the replay stream only")
    args = parser.parse_args()
    if args.census:
        return run_census(args.source, args.days)
    return run_rehearsal(args.source, args.target, args.days)


if __name__ == "__main__":
    sys.exit(main())
