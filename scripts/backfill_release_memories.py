"""Backfill durable clan memories for every past Elixir release in RELEASES.md.

    ./venv/bin/python scripts/backfill_release_memories.py --dry-run
    ./venv/bin/python scripts/backfill_release_memories.py --apply

Idempotent: each memory is keyed by the release tag (name-slug for the current
scheme, the version for legacy vX.Y entries), so re-running upserts rather than
duplicates. Parses both header shapes: `## Name (YYYY-MM-DD)` and the legacy
`## vX.Y — Name` (date read from the `**Date:**` line).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agent import release_notes as rn  # noqa: E402
from storage.contextual_memory import upsert_release_memory  # noqa: E402

_DATE_LINE = re.compile(r"^\s*\*\*Date:\*\*\s*(\S+)", re.M)


def parse_releases(text: str) -> list[dict]:
    matches = list(rn._RELEASE_HEADER.finditer(text))
    out: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[start:end]
        version = m.group(1)
        if version:  # legacy `## v5.1 — Consolidated Collector`
            name = m.group(2).strip()
            dm = _DATE_LINE.search(section)
            date = dm.group(1) if dm else ""
            tag = version
        else:  # current `## Blazing Balloon (2026-07-08)`
            name = m.group(3).strip()
            date = m.group(4)
            tag = rn.slugify_release(name)
        body = re.sub(
            r"^\s*\*\*Date:\*\*[^\n]*\n+", "", section.lstrip(), count=1
        ).strip()
        out.append(
            {"name": name, "date": date, "tag": tag, "body": body, "version": version}
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run", action="store_true", help="list what would be recorded (default)"
    )
    g.add_argument("--apply", action="store_true", help="write the memories")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    releases = parse_releases(open(rn.RELEASES_MD).read())
    print(f"{len(releases)} releases parsed from RELEASES.md\n")
    for r in releases:
        label = f"{r['name']} ({r['date']})" if r["date"] else r["name"]
        if not apply:
            print(
                f"  would record: {label:<42} tag={r['tag']:<24} body={len(r['body'])} chars"
            )
            continue
        meta = {"legacy_version": r["version"]} if r["version"] else {}
        url = (
            f"https://github.com/jthingelstad/elixir-bot/releases/tag/{r['tag']}"
            if rn._git(["tag", "-l", r["tag"]]).strip()
            else None
        )
        mem = upsert_release_memory(
            name=r["name"],
            date=r["date"],
            tag=r["tag"],
            body=r["body"],
            url=url,
            metadata=meta,
        )
        mid = mem.get("memory_id") if mem else "?"
        print(f"  recorded: {label:<42} tag={r['tag']:<24} memory_id={mid}")
    if not apply:
        print("\n(dry run — pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
