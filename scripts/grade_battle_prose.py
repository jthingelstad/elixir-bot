#!/usr/bin/env python3
"""Grade Battle Intelligence per-battle prose (Feature 3 eval benchmark).

Shows each of a member's enriched battles (default King Thing) with its computed
read + generated commentary + actual result, and records your grade into
``battle_enrichment.verdict`` (accurate | wrong | useful). The rule (plan §6): no
prompt v2 ships until ~20 of Jamie's own battles are graded against v1, so a v2 is
measured by regenerating those 20 and comparing verdict deltas — a real
before/after, not a vibe.

    python scripts/grade_battle_prose.py [--tag '#20JJJ2CCRU'] [--limit 20]
"""

import argparse
import sys

import db as db_facade

_GRADES = {"a": "accurate", "w": "wrong", "u": "useful", "s": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="#20JJJ2CCRU")  # King Thing
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--regrade", action="store_true", help="include already-graded battles")
    args = ap.parse_args()

    conn = db_facade.get_connection()
    where = "e.player_tag = ? AND e.commentary IS NOT NULL"
    if not args.regrade:
        where += " AND e.verdict IS NULL"
    rows = conn.execute(
        f"SELECT e.battle_dedup_key, e.battle_time, b.outcome, b.crowns_for, b.crowns_against, "
        f"op.archetype ours, tp.archetype theirs, e.closeness, e.performance, e.hp_margin, "
        f"e.commentary, e.loss_nature, e.notable, e.confidence, e.verdict "
        f"FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        f"LEFT JOIN deck_profile op ON op.deck_hash = e.our_deck_hash "
        f"LEFT JOIN deck_profile tp ON tp.deck_hash = e.their_deck_hash "
        f"WHERE {where} ORDER BY e.battle_time DESC LIMIT ?",
        (args.tag, args.limit),
    ).fetchall()
    if not rows:
        print("No prose to grade (generate it first, or use --regrade).")
        return 0

    print(
        f"Grading {len(rows)} battles for {args.tag}. Keys: [a]ccurate [w]rong [u]seful [s]kip [q]uit\n"
    )
    graded = 0
    for i, r in enumerate(rows, 1):
        close = {0: "stomp", 1: "clear", 2: "close", 3: "SQUEAKER"}.get(r["closeness"], "?")
        perf = {1: "UPSET WIN", -1: "underperformed", 0: "as expected"}.get(r["performance"], "-")
        print(
            f"── {i}/{len(rows)} ── {r['outcome']} {r['crowns_for']}-{r['crowns_against']}  "
            f"{r['ours']} vs {r['theirs']}  [{close}, {perf}, hp_margin={r['hp_margin']}]"
        )
        print(
            f"   loss_nature={r['loss_nature']} notable={r['notable']} confidence={r['confidence']}"
            + (f"  (was: {r['verdict']})" if r["verdict"] else "")
        )
        print(f'   "{r["commentary"]}"')
        while True:
            choice = input("   grade [a/w/u/s/q]: ").strip().lower()
            if choice == "q":
                print(f"\nStopped. Graded {graded} this session.")
                return 0
            if choice in _GRADES:
                break
            print("   (enter a, w, u, s, or q)")
        if _GRADES[choice] is not None:
            conn.execute(
                "UPDATE battle_enrichment SET verdict = ? WHERE battle_dedup_key = ?",
                (_GRADES[choice], r["battle_dedup_key"]),
            )
            conn.commit()
            graded += 1
        print()

    dist = dict(
        conn.execute(
            "SELECT verdict, COUNT(*) FROM battle_enrichment WHERE player_tag = ? AND verdict IS NOT NULL GROUP BY verdict",
            (args.tag,),
        ).fetchall()
    )
    print(f"Done. Graded {graded}. Verdict distribution for {args.tag}: {dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
