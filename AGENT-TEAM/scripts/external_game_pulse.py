#!/usr/bin/env python3
"""Audit the bounded, source-linked external evidence for Clash Royale."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = REPO / "AGENT-TEAM" / "external_game_sources.toml"
REQUIRED_TIERS = {"official", "competitive_aggregate", "community_sentiment"}
REQUIRED_SOURCE_KEYS = {"id", "tier", "cadence", "url", "purpose", "interpretation"}


def load_sources(path: Path = DEFAULT_SOURCES) -> list[dict[str, str]]:
    """Load a small, reviewed source manifest rather than scraping the web."""
    payload = tomllib.loads(path.read_text())
    if payload.get("version") != 1:
        raise ValueError("external source manifest version must be 1")
    entries = payload.get("source")
    if not isinstance(entries, list) or not entries:
        raise ValueError("external source manifest must contain sources")
    sources: list[dict[str, str]] = []
    ids: set[str] = set()
    tiers: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not REQUIRED_SOURCE_KEYS <= entry.keys():
            raise ValueError(
                "each external source needs id, tier, cadence, url, purpose, interpretation"
            )
        source = {key: str(entry[key]).strip() for key in REQUIRED_SOURCE_KEYS}
        if not all(source.values()):
            raise ValueError("external source fields must not be blank")
        if not source["url"].startswith("https://"):
            raise ValueError(f"external source URL must use https: {source['id']}")
        if source["id"] in ids:
            raise ValueError(f"duplicate external source id: {source['id']}")
        ids.add(source["id"])
        tiers.add(source["tier"])
        sources.append(source)
    missing_tiers = REQUIRED_TIERS - tiers
    if missing_tiers:
        raise ValueError(
            "external source manifest misses tiers: " + ", ".join(sorted(missing_tiers))
        )
    return sorted(sources, key=lambda source: source["id"])


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("meta snapshot timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def audit(
    conn: sqlite3.Connection,
    *,
    sources: list[dict[str, str]],
    max_meta_age_hours: int = 168,
    now: datetime | None = None,
) -> dict:
    """Report source policy and whether the existing aggregate snapshot needs review."""
    if max_meta_age_hours < 1:
        raise ValueError("max-meta-age-hours must be at least 1")
    current = now or datetime.now(timezone.utc)
    try:
        row = conn.execute(
            """SELECT snapshot_at, COUNT(*) AS deck_count, COUNT(DISTINCT source_url) AS source_count
               FROM meta_decks
               WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM meta_decks)"""
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    meta: dict[str, object]
    if row is None or row["snapshot_at"] is None:
        meta = {"state": "missing", "snapshot_at": None, "deck_count": 0, "source_count": 0}
    else:
        snapshot_at = _parse_timestamp(str(row["snapshot_at"]))
        age_hours = round(max(0.0, (current - snapshot_at).total_seconds() / 3600), 1)
        meta = {
            "state": "fresh" if age_hours <= max_meta_age_hours else "review_due",
            "snapshot_at": snapshot_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_hours": age_hours,
            "deck_count": int(row["deck_count"]),
            "source_count": int(row["source_count"]),
        }
    due = meta["state"] != "fresh"
    return {
        "sources": sources,
        "meta_snapshot": meta,
        "next_action": (
            "Perform the source-linked external review; do not scrape or change member behavior."
            if due
            else "Continue the scheduled source-linked review."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO / "elixir-v51.db")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--max-meta-age-hours", type=int, default=168)
    parser.add_argument("--exit-code", action="store_true", help="return 1 when meta review is due")
    args = parser.parse_args()
    try:
        sources = load_sources(args.sources)
        with _read_only_connection(args.db) as conn:
            result = audit(conn, sources=sources, max_meta_age_hours=args.max_meta_age_hours)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result))
    return 1 if args.exit_code and result["meta_snapshot"]["state"] != "fresh" else 0


if __name__ == "__main__":
    raise SystemExit(main())
