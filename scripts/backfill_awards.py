#!/usr/bin/env python3
"""Backfill clan awards for one or more historical seasons.

The v4.8 "Trophy Hall" release introduced a durable ``awards`` table with
detectors that grant season-wide awards on season close and weekly awards on
each ``war_completed``. Seasons that ended *before* v4.8 deployed — plus
already-completed weeks of the currently-live season — need a one-off backfill.

This script inserts the award rows directly via ``heartbeat._awards.backfill_season``
**without emitting live Discord signals** — the returned signal dicts are
collected for printing, but nothing is written to the signal dispatcher or
channel pipeline. After the backfill lands, the existing detectors handle
all future awards idempotently.

The grant layer uses ``INSERT OR IGNORE`` so the script is safe to re-run —
a second invocation reports zero new rows when everything is already in
place. That idempotency is what makes a "preview" flag unnecessary.

Usage:
    python scripts/backfill_awards.py --season 130
    python scripts/backfill_awards.py --season 130 --season 131
    python scripts/backfill_awards.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402
from heartbeat._awards import backfill_season  # noqa: E402


AWARD_ORDER = [
    "war_champ",
    "iron_king",
    "donation_champ",
    "rookie_mvp",
    "war_participant",
    "perfect_week",
    "victory_lap",
    "donation_champ_weekly",
]


def _format_row(signal: dict) -> str:
    member = signal.get("member", {}) or {}
    tag = member.get("tag") or signal.get("tag") or "?"
    name = member.get("name") or signal.get("name") or "?"
    rank = signal.get("rank")
    section = signal.get("section_index")
    metric = signal.get("metric_value")
    unit = signal.get("metric_unit") or ""
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "  ")
    scope = f"w{section}" if section is not None else "season"
    metric_text = f"{int(metric) if metric is not None and float(metric).is_integer() else metric} {unit}".strip() if metric is not None else ""
    return f"  {medal} [{scope:>6}] {name:<20} {tag:<10} {metric_text}"


def _print_summary(season_id: int, summary: dict[str, list[dict]]):
    header = f"\n=== Season {season_id} "
    header += "=" * (70 - len(header))
    print(header)
    total = 0
    for award_type in AWARD_ORDER:
        signals = summary.get(award_type, [])
        if not signals:
            print(f"\n{award_type}: (no new grants)")
            continue
        print(f"\n{award_type}: {len(signals)} new grant{'s' if len(signals) != 1 else ''}")
        for signal in signals:
            print(_format_row(signal))
        total += len(signals)
    print(f"\nSeason {season_id} total: {total} new award rows")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill clan awards for completed seasons and weeks.")
    parser.add_argument(
        "--season",
        action="append",
        type=int,
        help="Season ID to backfill. Repeat the flag to backfill multiple seasons.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Backfill every season present in war_races (oldest first).",
    )
    args = parser.parse_args()

    if not args.season and not args.all:
        parser.error("pass --season <id> (repeatable) or --all")
    if args.season and args.all:
        parser.error("--season and --all are mutually exclusive")

    conn = db.get_connection()
    try:
        if args.all:
            season_ids = [
                row["season_id"]
                for row in conn.execute(
                    "SELECT DISTINCT season_id FROM war_races ORDER BY season_id"
                ).fetchall()
            ]
            if not season_ids:
                print("No seasons found in war_races.")
                return 0
            print(f"Backfilling {len(season_ids)} season(s): {', '.join(str(s) for s in season_ids)}")
        else:
            season_ids = list(args.season)

        grand_total = 0
        for season_id in season_ids:
            race_count = conn.execute(
                "SELECT COUNT(*) AS c FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["c"]
            if race_count == 0:
                print(f"Season {season_id}: no war_races rows, skipping.")
                continue

            is_complete = db.season_is_complete(season_id, conn=conn)
            mode = "closed — full backfill" if is_complete else "in-progress — weekly awards + participants only"
            print(f"\nSeason {season_id}: {race_count} weeks in war_races; {mode}.")

            summary = backfill_season(season_id, conn=conn)
            _print_summary(season_id, summary)
            grand_total += sum(len(v) for v in summary.values())

        print(f"\n{grand_total} new award rows committed across {len(season_ids)} season(s). Re-run is safe (INSERT OR IGNORE).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
