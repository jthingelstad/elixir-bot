"""Replay gate — regression testing against REALITY (testing lever 1).

Snapshots the live DB to a scratch copy, clears state_baselines, and replays
the engine's own raw-payload window (raw_api_payloads, since go-live) back
through the offline engine. Because every dedup key is derived from payload
content + observation time, a correct engine re-derives (a subset of) the
events it already emitted and produces ~ZERO new rows:

    first payload per (entity, aspect)  -> re-seeds the baseline (first sight
                                           emits nothing — emitters contract)
    every later payload                 -> re-derives the original diffs;
                                           INSERT OR IGNORE dedups them away

So "new events after replay" is the drift alarm: it means current emitter
code, run over the exact payloads the live engine saw, disagrees with what
the live engine did. Small deltas are expected right after an emitter FIX
(the fix re-derives keys the buggy code missed) — the gate prints a per-type
breakdown so that judgment call takes ten seconds, and thresholds keep the
alarm from being noise.

Finishes with the season-close rehearsal (scripts/migrate_v51/
rehearse_season_close.py) on the same scratch copy when the latest season is
still open there, and the global DB invariants (tests/conftest.py).

Usage:
    ./venv/bin/python scripts/replay_gate.py            # full window since go-live
    ./venv/bin/python scripts/replay_gate.py --days 3   # recent window only
    ./venv/bin/python scripts/replay_gate.py --keep     # keep the scratch DB
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

EVENT_TABLES = ("player_events", "clan_events", "war_events")
NEW_EVENTS_MAX = 12   # slop for freshly-fixed emitters re-deriving missed keys
NEW_CLAIMS_MAX = 6
NEW_INTENTS_MAX = 6


def snapshot(live_path: str, scratch_path: str) -> None:
    """Consistent copy while the bot is live (WAL) — sqlite backup API."""
    src = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
    dst = sqlite3.connect(scratch_path)
    src.backup(dst)
    src.close()
    dst.close()


def engine_go_live(conn) -> str | None:
    """First moment the v5.1 engine emitted anything — replaying earlier
    payloads would 'discover' pre-engine history and flood the gate."""
    firsts = [
        r[0]
        for t in EVENT_TABLES
        for r in conn.execute(f"SELECT MIN(created_at) FROM {t}")
        if r[0]
    ]
    return min(firsts) if firsts else None


def counts(conn) -> dict:
    c = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in EVENT_TABLES}
    c["battle_events"] = conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0]
    c["ledger"] = conn.execute("SELECT COUNT(*) FROM recognition_ledger").fetchone()[0]
    c["intents"] = conn.execute("SELECT COUNT(*) FROM communication_intents").fetchone()[0]
    return c


def max_rowids(conn) -> dict:
    return {
        t: conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {t}").fetchone()[0]
        for t in EVENT_TABLES
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live-db", default=os.path.join(_REPO, "elixir-v51.db"))
    ap.add_argument("--days", type=int, default=None,
                    help="limit the replay window (default: everything since go-live)")
    ap.add_argument("--scratch-dir", default=None,
                    help="where the scratch copy lives (default: a tempdir)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch DB for inspection")
    ap.add_argument("--skip-season-close", action="store_true")
    args = ap.parse_args()

    scratch_dir = args.scratch_dir or tempfile.mkdtemp(prefix="elixir-replay-gate-")
    scratch = os.path.join(scratch_dir, "replay-gate.db")
    if os.path.exists(scratch):
        os.remove(scratch)
    print(f"snapshot: {args.live_db} -> {scratch}")
    snapshot(args.live_db, scratch)

    from engine.offline import OfflineEngine

    eng = OfflineEngine(scratch)
    conn = eng.conn

    go_live = engine_go_live(conn)
    if go_live is None:
        print("FAIL: no engine events in the DB — nothing to gate against")
        return 1
    window_start = go_live
    if args.days is not None:
        floor = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        window_start = max(window_start, floor)
    print(f"replay window: {window_start} -> now (engine go-live {go_live})")

    before = counts(conn)
    rowid_mark = max_rowids(conn)

    # Clear baselines so the first replayed payload per (entity, aspect)
    # re-seeds silently; without this the current baseline (today's state)
    # diffs BACKWARD against the window's oldest payload and emits phantoms.
    conn.execute("DELETE FROM state_baselines")
    conn.commit()

    rows = conn.execute(
        """SELECT endpoint, entity_key, payload_json, fetched_at
           FROM raw_api_payloads WHERE fetched_at >= ?
           ORDER BY fetched_at ASC, payload_id ASC""",
        (window_start,),
    ).fetchall()
    print(f"replaying {len(rows)} payloads...")
    for i, (endpoint, entity_key, payload_json, fetched_at) in enumerate(rows):
        eng.apply(endpoint, entity_key, payload_json, fetched_at)
        if i and i % 2000 == 0:
            print(f"  ... {i}/{len(rows)}")
    eng.finish()
    after = counts(conn)

    print("\n=== REPLAY DELTAS (before -> after) ===")
    for k in before:
        print(f"  {k:16s} {before[k]:7d} -> {after[k]:7d}  (+{after[k] - before[k]})")

    new_events = sum(after[t] - before[t] for t in EVENT_TABLES)
    if new_events:
        print("\nnew events by type:")
        for t in EVENT_TABLES:
            for r in conn.execute(
                f"SELECT event_type, COUNT(*), MIN(dedup_key) FROM {t} "
                "WHERE rowid > ? GROUP BY event_type",
                (rowid_mark[t],),
            ):
                print(f"  {t}.{r[0]}: {r[1]}  e.g. {r[2]}")

    gates: dict[str, bool] = {}
    gates[f"new events <= {NEW_EVENTS_MAX} (idempotence)"] = new_events <= NEW_EVENTS_MAX
    gates["new battle rows == 0"] = after["battle_events"] == before["battle_events"]
    gates[f"new ledger claims <= {NEW_CLAIMS_MAX}"] = (
        after["ledger"] - before["ledger"] <= NEW_CLAIMS_MAX
    )
    gates[f"new intents <= {NEW_INTENTS_MAX}"] = (
        after["intents"] - before["intents"] <= NEW_INTENTS_MAX
    )
    dupes = conn.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT recognition_key) FROM recognition_ledger"
    ).fetchone()[0]
    gates["zero duplicate ledger claims"] = dupes == 0

    try:
        from tests.conftest import assert_db_invariants

        assert_db_invariants(conn, label="replay gate")
        gates["global DB invariants"] = True
    except AssertionError as exc:
        print(f"\n{exc}")
        gates["global DB invariants"] = False

    # Season-close rehearsal on the replayed copy (skipped once the latest
    # season has actually closed — the rehearsal script targets an open one).
    if not args.skip_season_close:
        open_season = conn.execute(
            "SELECT season_id FROM war_seasons WHERE ended_at IS NULL "
            "ORDER BY season_id DESC LIMIT 1"
        ).fetchone()
        if open_season is None:
            print("\nseason-close rehearsal: skipped (no open season in the copy)")
        else:
            conn.commit()
            print(f"\nseason-close rehearsal (open season {open_season[0]})...")
            proc = subprocess.run(
                [sys.executable, os.path.join(_REPO, "scripts/migrate_v51/rehearse_season_close.py"),
                 scratch],
                capture_output=True, text=True, cwd=_REPO,
            )
            fails = [ln for ln in proc.stdout.splitlines() if "[FAIL]" in ln]
            passes = [ln for ln in proc.stdout.splitlines() if "[PASS]" in ln]
            gates[f"season-close rehearsal ({len(passes)} sub-gates)"] = (
                proc.returncode == 0 and passes and not fails
            )
            for ln in fails:
                print(" ", ln)
            if proc.returncode != 0:
                print(proc.stderr[-2000:])

    conn.close()
    print("\n=== GATES ===")
    ok = True
    for k, v in gates.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        ok = ok and v
    if args.keep or not ok:
        print(f"\nscratch DB kept at {scratch}")
    else:
        os.remove(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
