"""Apply reviewed corrections to card_facts from the 2026-08 audit.

Dry-run by default: prints every proposed change grouped by confidence and shows
which ones disagree with the value currently stored. Nothing is written without
--apply.

Corrections are recorded in provenance: a corrected row's ``source`` gains an
audit marker so a later reader can tell an audited row from a purely generated
one. The derived layer is NOT refreshed here -- run
``rebuild_deck_intel(force=True)`` afterwards, or the correction reaches only
decks enriched from this point on.

Usage:
    uv run python scripts/apply_card_facts_audit.py                     # review
    uv run python scripts/apply_card_facts_audit.py --apply --min high  # write
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

AUDIT_DIR = pathlib.Path(
    "/private/tmp/claude-502/-Users-otto-Projects/"
    "57ac3bd8-3d96-4bcc-bd25-3edad508c835/scratchpad/cardaudit"
)
RANK = {"high": 3, "medium": 2, "low": 1}

# Reviewed and deliberately NOT applied, regardless of the confidence the agent
# attached. Each is a judgement call where the stored value is defensible.
EXCLUSIONS = {
    # A buildings-only unit that walks at the tower and damages it fits the usual
    # definition of a win condition, even though its 4-elixir body is really there
    # to carry the buff aura. The auditing agent flagged this one itself as
    # "worth a second look" -- so leave it and let a human decide.
    ("Rune Giant", "is_win_condition"),
    ("Rune Giant", "role"),
    # Rocket as a win condition is the same argument in spell form: real Rocket-cycle
    # decks exist, but treating damage spells as win conditions would reclassify
    # Fireball and Lightning too. Consistency beats one card.
    ("Rocket", "is_win_condition"),
}

# Fields whose stored form is an integer flag; the audit reports them as ints,
# strings or bools depending on which agent wrote the row.
BOOL_FIELDS = {"splash_hits_air", "is_win_condition", "fragile_to_small_spell"}
EDITABLE = {
    "unit_domain",
    "targets",
    "attack_style",
    "splash_hits_air",
    "dps_tier",
    "hp_tier",
    "unit_count",
    "range_type",
    "role",
    "spell_tier",
    "is_win_condition",
    "fragile_to_small_spell",
    "special_json",
    "note",
}


def coerce(field: str, value):
    if field in BOOL_FIELDS:
        if isinstance(value, str):
            return 1 if value.strip().lower() in ("1", "true", "yes") else 0
        return 1 if value else 0
    if field == "special_json" and not isinstance(value, str):
        return json.dumps(value)
    return value


def load_changes() -> list[dict]:
    changes: list[dict] = []
    for path in sorted(AUDIT_DIR.glob("batch*.result.json")):
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  !! {path.name} is not valid JSON: {exc}", file=sys.stderr)
            continue
        for c in payload.get("changes", []):
            c["_batch"] = path.name
            changes.append(c)
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min", default="high", choices=["high", "medium", "low"])
    ap.add_argument("--db", default="elixir-v51.db")
    args = ap.parse_args()

    changes = load_changes()
    floor = RANK[args.min]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    applied = skipped_conf = stale = bad_field = 0
    excluded: list[dict] = []
    buckets: dict[str, list] = {"high": [], "medium": [], "low": []}

    for c in changes:
        field = c.get("field")
        if field not in EDITABLE:
            print(f"  !! refusing unknown field {field!r} ({c.get('label')})")
            bad_field += 1
            continue
        row = conn.execute(
            "SELECT * FROM card_facts WHERE card_id=? AND evolution_level=?",
            (c["card_id"], c["evolution_level"]),
        ).fetchone()
        if row is None:
            print(f"  !! no such row: {c.get('label')} ({c['card_id']}/{c['evolution_level']})")
            stale += 1
            continue
        current = row[field]
        proposed = coerce(field, c["proposed"])
        if current == proposed:
            continue  # already correct; another batch may have covered it
        if (c.get("label"), field) in EXCLUSIONS:
            excluded.append(c)
            continue
        conf = str(c.get("confidence", "low")).lower()
        buckets.setdefault(conf, []).append((c, current, proposed))

    for conf in ("high", "medium", "low"):
        items = buckets.get(conf) or []
        if not items:
            continue
        act = "APPLY" if RANK[conf] >= floor else "hold"
        print(f"\n=== {conf.upper()} confidence: {len(items)}  [{act}] ===")
        for c, current, proposed in items:
            print(f"  {c['label']:24} {c['field']:22} {current!r} -> {proposed!r}")
            print(f"    {c.get('evidence', '')[:150]}")
            if RANK[conf] >= floor and args.apply:
                conn.execute(
                    f"UPDATE card_facts SET {c['field']}=?, "
                    "source = CASE WHEN source LIKE '%audited%' THEN source "
                    "             ELSE COALESCE(source,'') || ' (audited 2026-08)' END "
                    "WHERE card_id=? AND evolution_level=?",
                    (proposed, c["card_id"], c["evolution_level"]),
                )
                applied += 1
            elif RANK[conf] < floor:
                skipped_conf += 1

    if args.apply:
        conn.commit()
        print(f"\nAPPLIED {applied} change(s) to {args.db}")
        print("Derived layer is now STALE -- run rebuild_deck_intel(force=True).")
    else:
        total = sum(len(v) for v in buckets.values())
        print(f"\nDRY RUN. {total} change(s) differ from stored values; none written.")
    if excluded:
        print(
            f"\nheld by review ({len(excluded)}): "
            + ", ".join(f"{c['label']}.{c['field']}" for c in excluded)
        )
    if bad_field or stale:
        print(f"rejected: {bad_field} unknown-field, {stale} unmatched-row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
