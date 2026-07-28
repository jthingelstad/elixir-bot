#!/usr/bin/env python3
"""Recover battle decks lost before schema v16 (#216).

Ingest dropped ``opponent[0].cards`` on every battle, so ``battle_events`` knew
who a member fought but never what they fought -- and it stored own decks
without the evolution mode, so an Evo Knight and a plain one were the same card
on both sides. v16 captures both going forward. This recovers what it can.

The ONLY surviving source is ``raw_api_payloads`` (endpoint ``player_battlelog``),
a bounded store: battles older than its retention window are gone for good,
because the deck an opponent brought to one specific battle is not re-fetchable
from any endpoint once it ages out of the battle log.

Matching is on the natural key ingest derives its ``dedup_key`` from: polled
player tag + ``battleTime``.

By default only NULL opponent decks are filled, so re-running is idempotent and
cannot clobber live capture. ``--refresh`` also rewrites decks already stored in
the older, thinner shape; that is safe because both come from the same payload
and the new shape is a strict superset.

Usage:
    uv run --locked python scripts/backfill_opponent_decks.py             # dry run
    uv run --locked python scripts/backfill_opponent_decks.py --apply
    uv run --locked python scripts/backfill_opponent_decks.py --apply --refresh
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.ingest import _deck_json  # noqa: E402
from engine.normalize import canon_tag  # noqa: E402

Decks = dict[tuple[str, str], tuple[str | None, str | None]]


def _recover(conn) -> Decks:
    """Map (player_tag, battle_time) -> (own deck, opponent deck) as slim JSON."""
    found: Decks = {}
    rows = conn.execute(
        "SELECT payload_json FROM raw_api_payloads WHERE endpoint = 'player_battlelog'"
    )
    for (payload,) in rows:
        try:
            battles = json.loads(payload)
        except json.JSONDecodeError, TypeError, ValueError:
            continue
        if not isinstance(battles, list):
            continue
        for battle in battles:
            if not isinstance(battle, dict):
                continue
            battle_time = battle.get("battleTime")
            if not battle_time:
                continue
            opponent = (battle.get("opponent") or [{}])[0]
            opp_deck = _deck_json(opponent if isinstance(opponent, dict) else {})
            # A payload is stored per polled player, but the battle inside lists
            # both sides. Attribute to every team member present so 2v2 rows
            # match too -- and give each row ITS OWN deck, not the first
            # teammate's.
            for member in battle.get("team") or []:
                if isinstance(member, dict) and member.get("tag"):
                    found[(canon_tag(member["tag"]), battle_time)] = (
                        _deck_json(member),
                        opp_deck,
                    )
    return found


def _has(recovered: Decks, row) -> bool:
    return (row["player_tag"], row["battle_time"]) in recovered


def main() -> int:
    apply = "--apply" in sys.argv
    refresh = "--refresh" in sys.argv

    import db

    conn = db.get_connection()

    recovered = _recover(conn)
    print(f"battles recoverable from retained payloads: {len(recovered)}")

    where = (
        "1=1"
        if refresh
        else "opponent_deck_json IS NULL OR opponent_deck_json NOT LIKE '%evolution_level%'"
    )
    candidates = conn.execute(
        f"SELECT player_tag, battle_time, opponent_deck_json FROM battle_events WHERE {where}"
    ).fetchall()

    updates = []
    for row in candidates:
        key = (row["player_tag"], row["battle_time"])
        if key not in recovered:
            continue
        own, opp = recovered[key]
        if not refresh and row["opponent_deck_json"] is not None:
            continue
        updates.append((own, opp, row["player_tag"], row["battle_time"]))

    total = conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0]
    missing = conn.execute(
        "SELECT COUNT(*) FROM battle_events WHERE opponent_deck_json IS NULL"
    ).fetchone()[0]
    print(f"battle_events rows: {total}   still missing an opponent deck: {missing}")
    print(f"rows this run would write: {len(updates)}" + ("  (--refresh)" if refresh else ""))
    fillable = sum(1 for r in candidates if r["opponent_deck_json"] is None and _has(recovered, r))
    print(f"of the missing, fillable now: {fillable}   unrecoverable: {missing - fillable}")

    if not apply:
        conn.rollback()
        conn.close()
        print("\ndry run — pass --apply to write")
        return 0

    conn.executemany(
        "UPDATE battle_events SET deck_json = COALESCE(?, deck_json), opponent_deck_json = ? "
        "WHERE player_tag = ? AND battle_time = ?",
        updates,
    )
    conn.commit()
    filled = conn.execute(
        "SELECT COUNT(*) FROM battle_events WHERE opponent_deck_json IS NOT NULL"
    ).fetchone()[0]
    evo = conn.execute(
        "SELECT COUNT(*) FROM battle_events WHERE opponent_deck_json LIKE '%evolution_level%'"
    ).fetchone()[0]
    print(
        f"\napplied. rows with an opponent deck: {filled} of {total} ({evo} carry evolution data)"
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
