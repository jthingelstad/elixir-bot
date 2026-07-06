#!/usr/bin/env python3
"""Sweep current members and report preferred-name resolution.

Most names are cleaned live by callable_name (no storage). Only residuals that
callable_name can't resolve (e.g. "...") get a stored nickname. Review with
--dry-run, then persist just the residuals with --apply.

    ./venv/bin/python scripts/sweep_nicknames.py            # dry run (review)
    ./venv/bin/python scripts/sweep_nicknames.py --apply    # persist residuals
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import db as engine_db  # noqa: E402
from engine.nicknames import generate_nickname, needs_nickname  # noqa: E402
from storage._formatting import callable_name  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv
    conn = engine_db.connect()
    try:
        rows = conn.execute(
            """SELECT p.player_tag, p.current_name,
                      pm.preferred_nickname, pm.nickname_source
               FROM players p
               JOIN clan_memberships cm
                 ON cm.player_tag = p.player_tag AND cm.left_at IS NULL
               LEFT JOIN player_metadata pm ON pm.player_tag = p.player_tag
               ORDER BY p.current_name"""
        ).fetchall()

        flagged = []
        for r in rows:
            name = r["current_name"]
            if not needs_nickname(name):
                continue
            residual = not (callable_name(name) and any(
                c.isascii() and c.isalnum() for c in callable_name(name)))
            if residual:
                nick, source = generate_nickname(name)
                how, store = f"stored ({source})", (nick, source)
            else:  # callable_name resolves it live — nothing to store
                nick, how, store = callable_name(name), "callable_name (live)", None
            flagged.append((r, name, nick, how, store))

        print(f"current members: {len(rows)} | flagged: {len(flagged)}\n")
        print(f"{'player_tag':13} {'current_name':22} {'->':2} {'display':16} how")
        for r, name, nick, how, _ in flagged:
            existing = f"  [have: {r['preferred_nickname']!r} {r['nickname_source']}]" if r["preferred_nickname"] else ""
            print(f"{r['player_tag']:13} {name!r:22} -> {nick!r:16} {how}{existing}")

        to_store = [(r, nick, store) for (r, name, nick, how, store) in flagged if store]
        if not apply:
            print(f"\nDRY RUN — would persist {len(to_store)} stored nickname(s) "
                  f"(residuals only). Re-run with --apply to write.")
            return

        from db import set_member_nickname
        now = engine_db.utcnow()
        n = 0
        for r, nick, (value, source) in to_store:
            if r["nickname_source"] == "leader":
                continue  # never override a leader-set name
            set_member_nickname(r["player_tag"], value, source=source,
                                observed_at=now, conn=conn)
            n += 1
        conn.commit()
        print(f"\nAPPLIED — persisted {n} stored nickname(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
