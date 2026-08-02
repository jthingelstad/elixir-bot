"""Cross-field consistency audit for card_facts.

Enum conformance only proves each field is spellable. These rules prove the fields
agree with each other -- a spell with a unit_domain, a card that splashes air but
cannot target air, an Evo form byte-identical to its base. Every rule here is a
statement about Clash Royale that must hold regardless of which card it is, so a
violation is a data defect rather than a judgement call.

Read-only. Run: uv run python scripts/audit_card_facts.py [db_path]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict

DB = sys.argv[1] if len(sys.argv) > 1 else "elixir-v51.db"
FORM = {0: "base", 1: "Evo", 2: "Hero"}

# Fields that describe the card itself. Provenance and form are excluded: two forms
# sharing every gameplay fact is the thing we want to detect.
GAMEPLAY = (
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
)


def load(db: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT f.*, c.name, c.elixir_cost, c.rarity FROM card_facts f "
            "LEFT JOIN card_catalog c ON c.card_id = f.card_id"
        )
    ]
    conn.close()
    return rows


def label(r: dict) -> str:
    form = FORM.get(r["evolution_level"], f"?{r['evolution_level']}")
    name = r["name"] or f"card {r['card_id']}"
    return name if form == "base" else f"{form} {name}"


def check(r: dict) -> list[tuple[str, str]]:
    """Return (rule, explanation) for every cross-field contradiction in one row."""
    out: list[tuple[str, str]] = []
    spell = r["spell_tier"] != "none"
    special = json.loads(r["special_json"] or "[]")

    # --- spells ------------------------------------------------------------
    if spell and r["role"] != "spell":
        out.append(("spell/role", f"spell_tier={r['spell_tier']} but role={r['role']}"))
    if r["role"] == "spell" and not spell:
        out.append(("spell/role", "role=spell but spell_tier=none"))
    if spell and r["unit_domain"] != "none":
        out.append(("spell/domain", f"a spell is not a unit, unit_domain={r['unit_domain']}"))
    if spell and r["unit_count"] != "one":
        out.append(("spell/count", f"spell with unit_count={r['unit_count']}"))
    if spell and r["range_type"] != "none":
        out.append(("spell/range", f"spell with range_type={r['range_type']}"))
    if spell and r["fragile_to_small_spell"]:
        out.append(("spell/fragile", "a spell cannot be killed by a small spell"))

    # --- buildings ---------------------------------------------------------
    if r["role"] == "building" and r["unit_domain"] != "none":
        out.append(("building/domain", f"building with unit_domain={r['unit_domain']}"))
    if r["unit_domain"] == "none" and r["role"] not in (
        "building",
        "spell",
        "spawner",
        "win_condition",
    ):
        out.append(("domain/role", f"unit_domain=none but role={r['role']}"))

    # --- targeting ---------------------------------------------------------
    # One direction only. `targets` is overloaded: for most cards it is the attack
    # target, but for Skeleton Barrel and Suspicious Bush -- which never attack at
    # all -- it records the PATHING target, which is exactly why they are win
    # conditions. So no-attack-with-a-building-target is legal; a card that targets
    # nothing having an attack style is not.
    if r["targets"] == "none" and r["attack_style"] != "none":
        out.append(
            (
                "targets/attack",
                f"targets=none but attack_style={r['attack_style']}",
            )
        )
    if r["attack_style"] == "none" and r["targets"] not in ("none", "buildings_only"):
        out.append(
            (
                "targets/attack",
                f"attack_style=none but targets={r['targets']} "
                "(only a pathing target is legal for a card that never attacks)",
            )
        )
    if r["splash_hits_air"] and r["targets"] not in ("air_and_ground",):
        out.append(
            (
                "splash_air/targets",
                f"splash_hits_air=1 but targets={r['targets']} -- cannot splash air it cannot hit",
            )
        )
    # `chain` belongs here with the splash styles: Electro Dragon and Electro Spirit
    # arc to several targets including air, which is area damage by any other name.
    if r["splash_hits_air"] and not (
        str(r["attack_style"]).startswith("splash") or r["attack_style"] == "chain"
    ):
        out.append(
            (
                "splash_air/attack",
                f"splash_hits_air=1 but attack_style={r['attack_style']} "
                "(a death bomb or one-time deploy barrage is not a normal attack)",
            )
        )
    if r["unit_domain"] == "air" and r["role"] == "building":
        out.append(("air/building", "a building cannot be an air unit"))

    # --- win conditions ----------------------------------------------------
    # Only one direction holds. is_win_condition is orthogonal to role by design:
    # Graveyard is a spell, X-Bow and Goblin Drill are buildings, Goblin Giant is a
    # tank, Royal Recruits is a swarm -- every one of them a win condition. The
    # biconditional flagged eight correct rows before this was narrowed.
    if r["role"] == "win_condition" and not r["is_win_condition"]:
        out.append(("wincon/role", "role=win_condition but is_win_condition=0"))

    # --- fragility ---------------------------------------------------------
    # A small spell (Zap/Log/Arrows) wipes cheap low-HP units. High HP contradicts it.
    if r["fragile_to_small_spell"] and r["hp_tier"] == "high":
        out.append(("fragile/hp", "fragile_to_small_spell=1 with hp_tier=high"))

    # --- spawners ----------------------------------------------------------
    if r["role"] == "spawner" and "spawns_units" not in special:
        out.append(("spawner/special", "role=spawner but special_json lacks spawns_units"))

    return out


def main() -> int:
    rows = load(DB)
    findings: list[tuple[dict, str, str]] = []
    for r in rows:
        for rule, why in check(r):
            findings.append((r, rule, why))

    # Forms that carry no gameplay difference from their base form.
    by_card: dict[int, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        by_card[r["card_id"]][r["evolution_level"]] = r
    identical = []
    for _cid, forms in by_card.items():
        base = forms.get(0)
        if not base:
            continue
        for lvl, r in forms.items():
            if lvl == 0:
                continue
            if all(r[k] == base[k] for k in GAMEPLAY):
                identical.append(r)

    print(f"card_facts rows: {len(rows)}   cards: {len(by_card)}\n")
    print(f"=== cross-field contradictions: {len(findings)} ===")
    by_rule: dict[str, list] = defaultdict(list)
    for r, rule, why in findings:
        by_rule[rule].append((r, why))
    for rule in sorted(by_rule, key=lambda k: -len(by_rule[k])):
        items = by_rule[rule]
        print(f"\n  [{rule}]  {len(items)}")
        for r, why in items[:40]:
            print(f"    {label(r):26} {why}")
        if len(items) > 40:
            print(f"    ... and {len(items) - 40} more")

    print(f"\n=== Evo/Hero forms identical to base in every gameplay field: {len(identical)} ===")
    for r in identical:
        print(f"    {label(r):26} (card_id={r['card_id']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
