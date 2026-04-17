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
]

# Historical award types that still appear in _revoked summaries when the
# backfill sweep prunes leftover rows from an older release.
DEPRECATED_AWARD_ORDER = ("perfect_week", "victory_lap", "donation_champ_weekly")


def _fmt_metric(signal: dict) -> str:
    metric = signal.get("metric_value")
    if metric is None:
        return ""
    unit = signal.get("metric_unit") or ""
    value = int(metric) if float(metric).is_integer() else metric
    return f"{value} {unit}".strip()


def _fmt_scope(section_index) -> str:
    return "season" if section_index is None else f"w{section_index}"


def _fmt_grant(signal: dict) -> str:
    member = signal.get("member") or {}
    tag = member.get("tag") or signal.get("tag") or "?"
    name = member.get("name") or signal.get("name") or "?"
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(signal.get("rank"), "  ")
    metric = _fmt_metric(signal)
    suffix = f" — {metric}" if metric else ""
    return f"      {medal} {name:<22} {tag}{suffix}"


def _fmt_stale(row: dict) -> str:
    name = row.get("player_name") or "?"
    tag = row.get("player_tag") or "?"
    scope = _fmt_scope(row.get("section_index"))
    return f"      · {name:<22} {tag}  [{scope}]"


def _print_season(season_id: int, race_count: int, is_complete: bool, summary: dict) -> tuple[int, int]:
    """Render one season's summary; return (granted, revoked) counts."""
    mode = "closed" if is_complete else "in-progress"
    weeks_label = f"{race_count} week{'s' if race_count != 1 else ''}"

    granted_lines: list[str] = []
    granted_total = 0
    for award_type in AWARD_ORDER:
        signals = summary.get(award_type) or []
        if not signals:
            continue
        granted_total += len(signals)
        granted_lines.append(f"  + {award_type} ({len(signals)})")
        granted_lines.extend(_fmt_grant(s) for s in signals)

    revoked_lines: list[str] = []
    revoked_total = 0
    revoked = summary.get("_revoked") or {}
    for award_type in (*AWARD_ORDER, *DEPRECATED_AWARD_ORDER):
        rows = revoked.get(award_type) or []
        if not rows:
            continue
        revoked_total += len(rows)
        if award_type in DEPRECATED_AWARD_ORDER:
            revoked_lines.append(f"  - {award_type} ({len(rows)}, deprecated)")
        else:
            revoked_lines.append(f"  - {award_type} ({len(rows)})")
            revoked_lines.extend(_fmt_stale(r) for r in rows)

    if granted_lines or revoked_lines:
        print(f"\nSeason {season_id} · {weeks_label} · {mode}")
        for line in granted_lines:
            print(line)
        for line in revoked_lines:
            print(line)
    else:
        print(f"\nSeason {season_id} · {weeks_label} · {mode} — no changes")

    return granted_total, revoked_total


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

        grand_granted = 0
        grand_revoked = 0
        for season_id in season_ids:
            race_count = conn.execute(
                "SELECT COUNT(*) AS c FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["c"]
            if race_count == 0:
                print(f"\nSeason {season_id} · no war_races rows, skipping")
                continue

            is_complete = db.season_is_complete(season_id, conn=conn)
            summary = backfill_season(season_id, conn=conn)
            granted, revoked = _print_season(season_id, race_count, is_complete, summary)
            grand_granted += granted
            grand_revoked += revoked

        print(
            f"\n{len(season_ids)} season{'s' if len(season_ids) != 1 else ''} · "
            f"{grand_granted} granted · {grand_revoked} revoked · re-run is idempotent"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
