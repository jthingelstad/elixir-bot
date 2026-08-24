#!/usr/bin/env python3
"""Check whether a naturally delivered Elixir post used one or more exact labels."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check(
    conn: sqlite3.Connection,
    *,
    labels: list[str],
    since: str,
    expires_hours: int,
    require_all: bool = False,
    now: datetime | None = None,
) -> dict:
    if not labels:
        raise ValueError("at least one label is required")
    if expires_hours < 1:
        raise ValueError("expires-hours must be at least 1")
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("since must include a timezone")
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT lane, posted_at, content_preview
           FROM awareness_posts WHERE posted_at >= ? ORDER BY posted_at ASC""",
        (start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    matches: list[dict] = []
    found: set[str] = set()
    for row in rows:
        matched = [label for label in labels if label in str(row["content_preview"] or "")]
        if matched:
            found.update(matched)
            matches.append({"lane": row["lane"], "posted_at": row["posted_at"], "labels": matched})
    accepted = found == set(labels) if require_all else bool(found)
    expires_at = start.astimezone(timezone.utc) + timedelta(hours=expires_hours)
    state = "accepted" if accepted else "expired" if current >= expires_at else "waiting"
    return {
        "state": state,
        "since": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "found_labels": sorted(found),
        "matches": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "elixir-v51.db")
    parser.add_argument("--since", required=True, help="UTC ISO timestamp after deployment")
    parser.add_argument("--label", action="append", required=True, dest="labels")
    parser.add_argument("--expires-hours", type=int, default=336)
    parser.add_argument("--all-labels", action="store_true", help="wait until every label appears")
    parser.add_argument("--exit-code", action="store_true", help="return 2 when the watch expires")
    args = parser.parse_args()
    try:
        with _read_only_connection(args.db) as conn:
            result = check(
                conn,
                labels=args.labels,
                since=args.since,
                expires_hours=args.expires_hours,
                require_all=args.all_labels,
            )
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result))
    return 2 if args.exit_code and result["state"] == "expired" else 0


if __name__ == "__main__":
    raise SystemExit(main())
