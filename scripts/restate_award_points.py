#!/usr/bin/env python3
"""Restate stored award point totals from war_participation.

Why this exists
---------------
Season 133's awards were granted while that season's participation rows were
still in flux, so the stored metric values do not describe the season that
actually happened. Audited 2026-08-03 against a RoyaleAPI clan-wars export:
31 of 45 point-based S133 rows disagreed with the finished data. The
`war_participant` rows had stored only the FIRST week's points (2 was recorded
at 2,700 -- their section-0 value -- against a real season total of 14,400),
while S134 stores season totals for the same award type. Same ledger, two
meanings.

The safety property
-------------------
**Only seasons whose participation data is COMPLETE are eligible.** Seasons 129
through 132 lost their final week to a retention purge (the week's rows were
stamped with the epoch-sentinel `finish_time`, so the sweep deleted them on
sight). Their stored award values are *higher* than what the database can now
reproduce -- because those values are RIGHT and the database is what is missing
data. Restating them would overwrite correct history with an incomplete sum.
The completeness gate below is what stops that, and it is the single most
important line in this script.

A restatement also never reorders a podium. If the corrected numbers would
change who placed where, this aborts and reports instead: that is a different
decision than fixing a number, and a human should make it.

Usage:
    python3 scripts/restate_award_points.py              # dry run
    python3 scripts/restate_award_points.py --apply
    python3 scripts/restate_award_points.py --season 133 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PODIUM_TYPES = ("war_champ", "rookie_mvp")


def _connect(path: str | None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or os.getenv("ELIXIR_DB_PATH") or "elixir-v51.db")
    conn.row_factory = sqlite3.Row
    # The bot is a live single-writer holding the same file; wait for it rather
    # than dying half way through a restatement.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def complete_seasons(conn: sqlite3.Connection) -> dict[int, int]:
    """Seasons where every declared week has participation rows.

    A season missing any week is INELIGIBLE -- see the module docstring.
    """
    out = {}
    for row in conn.execute("SELECT season_id, weeks FROM war_seasons WHERE weeks IS NOT NULL"):
        have = conn.execute(
            "SELECT COUNT(DISTINCT section_index) n FROM war_participation WHERE season_id = ?",
            (row["season_id"],),
        ).fetchone()["n"]
        if have and have == row["weeks"]:
            out[int(row["season_id"])] = int(row["weeks"])
    return out


def true_points(conn: sqlite3.Connection, season_id: int) -> dict[str, tuple[int, int]]:
    """player_tag -> (season points, races participated). All members, current or
    departed: the ledger records what happened, not who is still here."""
    return {
        r["player_tag"]: (int(r["pts"] or 0), int(r["races"] or 0))
        for r in conn.execute(
            "SELECT player_tag, SUM(COALESCE(fame,0)) pts, COUNT(*) races "
            "FROM war_participation WHERE season_id = ? GROUP BY player_tag",
            (season_id,),
        )
    }


def _podium_order(rows: list[sqlite3.Row], values: dict[int, int]) -> list[str]:
    """Podium order under `values`, ties broken as stored (by existing rank)."""
    ranked = sorted(rows, key=lambda r: (-values[r["award_id"]], r["rank"]))
    return [r["player_tag"] for r in ranked]


def plan_season(conn: sqlite3.Connection, season_id: int) -> tuple[list[dict], list[str]]:
    truth = true_points(conn, season_id)
    rows = conn.execute(
        "SELECT award_id, award_type, rank, player_tag, metric_value, metadata_json "
        "FROM awards WHERE season_id = ? AND metric_unit = 'points' ORDER BY award_type, rank",
        (season_id,),
    ).fetchall()

    changes, problems = [], []
    new_value = {}
    for r in rows:
        pts, races = truth.get(r["player_tag"], (None, None))
        if pts is None:
            problems.append(
                f"award {r['award_id']} ({r['award_type']}, {r['player_tag']}): "
                "no participation rows for this season -- refusing to guess"
            )
            new_value[r["award_id"]] = int(r["metric_value"] or 0)
            continue
        new_value[r["award_id"]] = pts
        if abs(float(r["metric_value"] or 0) - pts) < 0.5:
            continue
        meta = json.loads(r["metadata_json"] or "{}")
        meta["races_participated"] = races
        if "avg_points" in meta:
            meta["avg_points"] = round(pts / races, 1) if races else 0.0
        meta["restated"] = {
            "on": "2026-08-03",
            "from": float(r["metric_value"] or 0),
            "reason": "granted against incomplete participation data; "
            "restated from finished war_participation (audited vs RoyaleAPI export)",
        }
        changes.append(
            {
                "award_id": r["award_id"],
                "award_type": r["award_type"],
                "player_tag": r["player_tag"],
                "old": float(r["metric_value"] or 0),
                "new": float(pts),
                "metadata_json": json.dumps(meta),
            }
        )

    # A restatement corrects numbers, never standings.
    for award_type in PODIUM_TYPES:
        podium = [r for r in rows if r["award_type"] == award_type]
        if len(podium) < 2:
            continue
        before = [r["player_tag"] for r in sorted(podium, key=lambda r: r["rank"])]
        after = _podium_order(podium, new_value)
        if before != after:
            problems.append(
                f"S{season_id} {award_type}: restating REORDERS the podium "
                f"{before} -> {after}. Not applying; a human decides this."
            )
    return changes, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--season", type=int, help="restrict to one season")
    ap.add_argument("--db")
    args = ap.parse_args()

    conn = _connect(args.db)
    eligible = complete_seasons(conn)
    if args.season:
        if args.season not in eligible:
            print(f"S{args.season} is NOT eligible (participation data incomplete). Refusing.")
            return 1
        eligible = {args.season: eligible[args.season]}

    all_seasons = [r["season_id"] for r in conn.execute("SELECT season_id FROM war_seasons")]
    skipped = sorted(set(all_seasons) - set(eligible))
    print(f"eligible (complete) seasons: {sorted(eligible)}")
    print(f"skipped (incomplete -- stored values are authoritative): {skipped}\n")

    total, blocked = 0, False
    for season_id in sorted(eligible):
        changes, problems = plan_season(conn, season_id)
        for p in problems:
            print(f"  !! {p}")
            blocked = True
        if not changes:
            print(f"S{season_id}: nothing to restate")
            continue
        print(f"S{season_id}: {len(changes)} row(s) to restate")
        for c in changes:
            name = conn.execute(
                "SELECT COALESCE(display_name, current_name) n FROM players WHERE player_tag = ?",
                (c["player_tag"],),
            ).fetchone()
            print(
                f"   {c['award_type']:16} {(name['n'] if name else c['player_tag']):16} "
                f"{c['old']:>8.0f} -> {c['new']:>8.0f}"
            )
        total += len(changes)
        if args.apply and not problems:
            with conn:
                for c in changes:
                    conn.execute(
                        "UPDATE awards SET metric_value = ?, metadata_json = ? WHERE award_id = ?",
                        (c["new"], c["metadata_json"], c["award_id"]),
                    )
            print(f"   applied {len(changes)} update(s)")

    if blocked:
        print("\nABORTED for the seasons flagged above.")
        return 2
    if not args.apply:
        print(f"\nDRY RUN -- {total} row(s) would change. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
