#!/usr/bin/env python3
"""Report fresh battle-mode sentinels lacking a reviewed display label."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.normalize import game_mode_label_status  # noqa: E402


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def audit(conn: sqlite3.Connection, *, hours: int, now: datetime | None = None) -> list[dict]:
    if hours < 1:
        raise ValueError("hours must be at least 1")
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT name, first_seen_at, sample_json
           FROM api_sentinel_observations
           WHERE sentinel_type = 'battle_game_mode' AND first_seen_at >= ?
           ORDER BY first_seen_at DESC, observation_id DESC""",
        (cutoff,),
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        try:
            sample = json.loads(row["sample_json"] or "{}")
        except TypeError, ValueError:
            sample = {}
        mode_name = sample.get("name") if isinstance(sample, dict) else None
        status, label = game_mode_label_status(mode_name)
        findings.append(
            {
                "mode_id": str(row["name"]),
                "mode_name": mode_name,
                "display_label": label,
                "label_status": status,
                "event_tag": sample.get("event_tag") if isinstance(sample, dict) else None,
                "first_seen_at": row["first_seen_at"],
            }
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "elixir-v51.db")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--exit-code", action="store_true", help="return 1 for unreviewed modes")
    args = parser.parse_args()
    try:
        with _read_only_connection(args.db) as conn:
            findings = audit(conn, hours=args.hours)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        parser.error(str(exc))
    review_required = [item for item in findings if item["label_status"] == "unreviewed"]
    print(json.dumps({"hours": args.hours, "modes": findings, "review_required": review_required}))
    return 1 if args.exit_code and review_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
