#!/usr/bin/env python3
"""Backfill POAP KINGS blog posts for all past weekly recap messages.

Reads every ``weekly_clan_recap`` message from the database and commits a
markdown blog post to the site repo for each one. Safe to re-run — GitHub will
accept the commit whether or not the file already exists (it will overwrite).

Usage:
    python scripts/backfill_weekly_recap_blog_posts.py
    python scripts/backfill_weekly_recap_blog_posts.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402
from modules.poap_kings import site as poap_kings_site  # noqa: E402
from runtime.jobs._site import _publish_weekly_recap_blog_post  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be published without committing")
    args = parser.parse_args()

    if not args.dry_run and not poap_kings_site.site_enabled():
        print("ERROR: POAP KINGS site integration is not enabled. Set POAP_KINGS_SITE_ENABLED=1 and configure the token.")
        sys.exit(1)

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT message_id, created_at, content FROM messages WHERE event_type = 'weekly_clan_recap' ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No weekly_clan_recap messages found in the database.")
        return

    print(f"Found {len(rows)} weekly recap(s) to backfill.\n")

    for message_id, created_at, content in rows:
        # Parse timestamp from DB (stored as ISO 8601 UTC string)
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)

        from modules.poap_kings import site as _pk_site
        import pytz
        CHICAGO = pytz.timezone("America/Chicago")
        local_ts = ts.astimezone(CHICAGO)
        date_str = local_ts.strftime("%Y-%m-%d")
        path = f"src/blog/posts/{date_str}-weekly-recap.md"

        print(f"  message_id={message_id}  date={date_str}  → {path}")

        if args.dry_run:
            continue

        try:
            result = _publish_weekly_recap_blog_post(content, now=ts)
            if result.get("changed"):
                print(f"    ✓ committed: {result['commit_url']}")
            else:
                print(f"    – no change")
        except Exception as exc:
            print(f"    ✗ failed: {exc}")

    if args.dry_run:
        print("\nDry run complete — no commits made.")
    else:
        print("\nBackfill complete.")


if __name__ == "__main__":
    main()
