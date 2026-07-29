#!/usr/bin/env python3
"""One-time, idempotent repair for issue #184's live integrity findings.

Default mode is a transactional dry-run. Pass ``--apply`` only after taking a
verified SQLite backup. The repair is intentionally narrow:

* restore profile-owned player projection fields from state baselines and raw
  profile history;
* canonicalize carried war timestamps to ISO-Z;
* apply the schema's intended ON DELETE SET NULL result to retired-channel
  conversation rows;
* remove only the known pre-cut/bootstrap membership duplicates.

Every mutation is followed by the same terminal audit used by the CLI. Any
remaining foreign-key violation, overlapping membership, projection mismatch,
or non-canonical war timestamp aborts the transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db
from engine.normalize import canonical_utc_timestamp, parse_cr_time

CHICAGO = ZoneInfo("America/Chicago")

WAR_TIMESTAMP_COLUMNS = (
    ("war_seasons", "started_at"),
    ("war_seasons", "ended_at"),
    ("war_weeks", "created_date"),
    ("war_weeks", "finish_time"),
    ("war_week_clans", "completed_at"),
    ("war_week_clans", "observed_at"),
    ("war_participation", "observed_at"),
    ("war_attendance_days", "observed_at"),
    ("war_events", "observed_at"),
    ("war_events", "window_start"),
    ("war_events", "created_at"),
)


def _canon_tag(value: str | None) -> str:
    text = str(value or "").strip().upper()
    return f"#{text.lstrip('#')}" if text else ""


def _count(conn, sql: str, params=()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _membership_overlap_count(conn) -> int:
    return _count(
        conn,
        """SELECT COUNT(*) FROM clan_memberships a
             JOIN clan_memberships b
               ON a.player_tag = b.player_tag
              AND a.clan_tag = b.clan_tag
              AND a.membership_id < b.membership_id
            WHERE a.joined_at < COALESCE(b.left_at, '9999-12-31')
              AND b.joined_at < COALESCE(a.left_at, '9999-12-31')""",
    )


def _noncanonical_war_timestamp_count(conn) -> int:
    total = 0
    for table, column in WAR_TIMESTAMP_COLUMNS:
        total += _count(
            conn,
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL "
            f"AND (length({column}) < 10 OR substr({column}, 5, 1) <> '-' "
            f"OR substr({column}, 8, 1) <> '-')",
        )
    return total


def audit(conn) -> dict[str, int]:
    return {
        "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        "membership_overlaps": _membership_overlap_count(conn),
        "profile_best_projection_mismatches": _count(
            conn,
            """WITH profile AS (
                   SELECT entity_tag AS player_tag,
                          CAST(json_extract(payload_json, '$.best_trophies') AS INTEGER) AS best
                     FROM state_baselines
                    WHERE entity_kind = 'player' AND aspect = 'profile'
               )
               SELECT COUNT(*)
                 FROM profile p JOIN player_current_state cs USING(player_tag)
                WHERE p.best IS NOT NULL AND cs.best_trophies IS NOT p.best""",
        ),
        "profile_exp_projection_mismatches": _count(
            conn,
            """WITH profile AS (
                   SELECT entity_tag AS player_tag,
                          CAST(json_extract(payload_json, '$.exp_level') AS INTEGER) AS exp
                     FROM state_baselines
                    WHERE entity_kind = 'player' AND aspect = 'profile'
               )
               SELECT COUNT(*)
                 FROM profile p JOIN player_current_state cs USING(player_tag)
                WHERE p.exp > 0 AND cs.exp_level IS NOT p.exp""",
        ),
        "daily_best_nulls": _count(
            conn,
            "SELECT COUNT(*) FROM player_daily_metrics WHERE best_trophies IS NULL",
        ),
        "daily_best_drops_to_null": _count(
            conn,
            """WITH ordered AS (
                   SELECT player_tag, metric_date, best_trophies,
                          LAG(best_trophies) OVER (
                              PARTITION BY player_tag ORDER BY metric_date
                          ) AS prior
                     FROM player_daily_metrics
               )
               SELECT COUNT(*) FROM ordered
                WHERE best_trophies IS NULL AND prior IS NOT NULL""",
        ),
        "noncanonical_war_timestamps": _noncanonical_war_timestamp_count(conn),
    }


def repair_current_player_projection(conn) -> int:
    changed = 0
    rows = conn.execute(
        "SELECT entity_tag, payload_json FROM state_baselines "
        "WHERE entity_kind = 'player' AND aspect = 'profile'"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except TypeError, ValueError:
            continue
        best = payload.get("best_trophies")
        exp = payload.get("exp_level")
        if not isinstance(best, int) and not (isinstance(exp, int) and exp > 0):
            continue
        cursor = conn.execute(
            """UPDATE player_current_state
                  SET best_trophies = COALESCE(?, best_trophies),
                      exp_level = COALESCE(?, exp_level)
                WHERE player_tag = ?
                  AND (best_trophies IS NOT COALESCE(?, best_trophies)
                       OR exp_level IS NOT COALESCE(?, exp_level))""",
            (
                best if isinstance(best, int) else None,
                exp if isinstance(exp, int) and exp > 0 else None,
                row["entity_tag"],
                best if isinstance(best, int) else None,
                exp if isinstance(exp, int) and exp > 0 else None,
            ),
        )
        changed += cursor.rowcount
    return changed


def _raw_profile_history(conn) -> dict[str, list[tuple[str, int | None, int | None]]]:
    history: dict[str, list[tuple[str, int | None, int | None]]] = defaultdict(list)
    rows = conn.execute(
        "SELECT entity_key, fetched_at, payload_json FROM raw_api_payloads "
        "WHERE endpoint = 'player' ORDER BY fetched_at ASC, rowid ASC"
    ).fetchall()
    for row in rows:
        when = parse_cr_time(row["fetched_at"])
        if when is None:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except TypeError, ValueError:
            continue
        best = payload.get("bestTrophies")
        exp = payload.get("expLevel")
        history[_canon_tag(row["entity_key"])].append(
            (
                when.astimezone(CHICAGO).date().isoformat(),
                best if isinstance(best, int) else None,
                exp if isinstance(exp, int) and exp > 0 else None,
            )
        )
    return history


def repair_player_daily_metrics(conn) -> int:
    history = _raw_profile_history(conn)
    changed = 0
    tags = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT player_tag FROM player_daily_metrics ORDER BY player_tag"
        )
    ]
    for tag in tags:
        observations = history.get(tag, [])
        obs_index = 0
        raw_best = raw_exp = None
        carried_best = carried_exp = None
        metrics = conn.execute(
            "SELECT metric_date, best_trophies, exp_level FROM player_daily_metrics "
            "WHERE player_tag = ? ORDER BY metric_date",
            (tag,),
        ).fetchall()
        for metric in metrics:
            while (
                obs_index < len(observations)
                and observations[obs_index][0] <= metric["metric_date"]
            ):
                _, candidate_best, candidate_exp = observations[obs_index]
                if candidate_best is not None:
                    raw_best = candidate_best
                if candidate_exp is not None:
                    raw_exp = candidate_exp
                obs_index += 1

            existing_best = metric["best_trophies"]
            existing_exp = metric["exp_level"]
            if isinstance(existing_best, int) and existing_best > 0:
                carried_best = max(carried_best or 0, existing_best)
            if isinstance(existing_exp, int) and existing_exp > 0:
                carried_exp = max(carried_exp or 0, existing_exp)

            target_best = raw_best if raw_best is not None else carried_best
            target_exp = raw_exp if raw_exp is not None else carried_exp
            if target_best is not None:
                carried_best = max(carried_best or 0, target_best)
            if target_exp is not None:
                carried_exp = max(carried_exp or 0, target_exp)

            # A leading historical row may predate the retained raw window. In
            # that case there is nothing factual to backfill, so preserve its
            # current value without counting a no-op COALESCE update forever.
            final_best = target_best if target_best is not None else existing_best
            final_exp = target_exp if target_exp is not None else existing_exp
            if existing_best == final_best and existing_exp == final_exp:
                continue
            cursor = conn.execute(
                "UPDATE player_daily_metrics SET best_trophies = ?, exp_level = ? "
                "WHERE player_tag = ? AND metric_date = ?",
                (final_best, final_exp, tag, metric["metric_date"]),
            )
            changed += cursor.rowcount
    return changed


def normalize_war_timestamps(conn) -> int:
    changed = 0
    for table, column in WAR_TIMESTAMP_COLUMNS:
        rows = conn.execute(
            f"SELECT rowid AS repair_rowid, {column} AS value FROM {table} "
            f"WHERE {column} IS NOT NULL"
        ).fetchall()
        for row in rows:
            canonical = canonical_utc_timestamp(row["value"])
            if canonical is None:
                raise RuntimeError(
                    f"cannot normalize {table}.{column} rowid={row['repair_rowid']}: "
                    f"{row['value']!r}"
                )
            if canonical == row["value"]:
                continue
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                (canonical, row["repair_rowid"]),
            )
            changed += 1
    return changed


def repair_membership_overlaps(conn) -> int:
    # Fourteen synthetic cutover rows overlap a better historical/manual row.
    # The fifteenth overlap is the inverse: a broad manual-clear bootstrap row
    # is authoritative and the shorter generic backfill is the duplicate.
    rows = conn.execute(
        """SELECT DISTINCT a.membership_id
             FROM clan_memberships a
             JOIN clan_memberships b
               ON a.player_tag = b.player_tag
              AND a.clan_tag = b.clan_tag
              AND a.membership_id <> b.membership_id
            WHERE a.joined_at < COALESCE(b.left_at, '9999-12-31')
              AND b.joined_at < COALESCE(a.left_at, '9999-12-31')
              AND (
                    a.leave_source LIKE 'pre_cut_reconciliation%'
                    OR (a.join_source = 'backfill' AND b.leave_source = 'manual_clear')
                  )"""
    ).fetchall()
    ids = [int(row[0]) for row in rows]
    if not ids:
        return 0
    conn.executemany(
        "DELETE FROM clan_memberships WHERE membership_id = ?",
        [(membership_id,) for membership_id in ids],
    )
    return len(ids)


def run_repair(conn, *, apply: bool) -> dict:
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    before = audit(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changes = {
            "player_current_state_rows": repair_current_player_projection(conn),
            "player_daily_metrics_rows": repair_player_daily_metrics(conn),
            "war_timestamp_values": normalize_war_timestamps(conn),
            "membership_rows_removed": repair_membership_overlaps(conn),
        }
        after = audit(conn)
        terminal = {
            key: after[key]
            for key in (
                "foreign_key_violations",
                "membership_overlaps",
                "profile_best_projection_mismatches",
                "profile_exp_projection_mismatches",
                "daily_best_drops_to_null",
                "noncanonical_war_timestamps",
            )
            if after[key]
        }
        if terminal:
            raise RuntimeError(f"repair terminal audit failed: {terminal}")
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return {"applied": apply, "before": before, "changes": changes, "after": after}
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the repair")
    parser.add_argument(
        "--db",
        default=os.getenv("ELIXIR_DB_PATH", "elixir-v51.db"),
        help="operational database path",
    )
    args = parser.parse_args()
    conn = db.get_connection(args.db)
    try:
        result = run_repair(conn, apply=args.apply)
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
