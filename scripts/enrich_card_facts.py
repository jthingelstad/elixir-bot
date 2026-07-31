#!/usr/bin/env python3
"""Enrich Clash Royale card behavior facts with Opus + web search (v2 Layer 1).

The CR API gives us NOTHING behavioral (only id/name/elixirCost/rarity/maxLevel),
so the structured facts every downstream layer needs — what a card targets, whether
it flies, whether it answers air or tanks — cannot be derived and must be enriched.
This is the one genuine LLM job in Battle Intelligence v2 (docs/plans/
battle-intelligence-5-interpreted.md): rare, batch, on immutable card identity.

Design:
  * **Opus + native web_search** so facts come from current sources, not a training
    cutoff. Each card records the source the model cites.
  * **Primitives only.** The model asserts simple, checkable facts (targets,
    domain, tiers); the *roles* a deck needs (air answer, tank answer, splash
    answer) are DERIVED in SQL from these — see `engine/card_roles.py`. That keeps
    the model's error surface small and the roles auditable in one place.
  * **Tiers, not raw stats.** HP/damage numbers are level-dependent and move every
    balance patch; tiers are stable and are all the reads need.
  * **No counter lists.** "Inferno counters Golem" is a staleness/hallucination
    magnet and is derivable from primitives.

Keyed on ``(card_id, evolution_level)`` — Evo/Hero forms behave differently and are
enriched separately (same form-aware identity Features 1-2 use).

Usage:
    python scripts/enrich_card_facts.py --dry-run --limit 8   # print, don't write
    python scripts/enrich_card_facts.py                        # enrich all missing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "claude-opus-4-8"
BATCH = 8  # cards per call — small enough that web search stays focused per card

ENUMS = {
    "unit_domain": ["air", "ground", "none"],
    "targets": ["ground", "air_and_ground", "buildings_only", "none"],
    "attack_style": ["single", "splash_small", "splash_large", "chain", "none"],
    "dps_tier": ["low", "medium", "high"],
    "hp_tier": ["low", "medium", "high"],
    "unit_count": ["one", "few", "many"],
    "range_type": ["melee_short", "melee_medium", "melee_long", "ranged", "siege", "none"],
    "role": [
        "win_condition",
        "tank",
        "mini_tank",
        "support",
        "swarm",
        "spell",
        "building",
        "spawner",
        "champion",
    ],
    "spell_tier": ["small", "medium", "big", "none"],
}
SPECIALS = [
    "knockback",
    "reset",
    "charge",
    "spawns_units",
    "freeze",
    "rage",
    "heal",
    "invulnerable",
    "shield",
    "dash",
]

SYSTEM = f"""You are a Clash Royale data analyst producing a STRUCTURED CARD FACTS table.

Your output feeds a deterministic deck-analysis system: rules code derives "does this
deck have an air answer / tank answer / splash answer" from your fields. Accuracy on the
primitives matters far more than nuance.

Use web search to confirm each card's CURRENT behavior (RoyaleAPI, Deckshop, the Clash
Royale wiki, or Supercell's own pages). Do not rely on memory alone — cards get reworked.

For EACH card return exactly these fields:
- unit_domain: {ENUMS["unit_domain"]}  (where the unit itself lives; spells/buildings = "none")
- targets: {ENUMS["targets"]}  (what it can ATTACK; "buildings_only" = ignores troops, e.g. Hog Rider)
- attack_style: {ENUMS["attack_style"]}
- splash_hits_air: true/false  (does its splash/area damage hit AIR units)
- dps_tier / hp_tier: {ENUMS["dps_tier"]}  (relative to other cards, not absolute numbers)
- unit_count: {ENUMS["unit_count"]}  ("one", "few"=2-3, "many"=4+)
- range_type: {ENUMS["range_type"]}
- role: {ENUMS["role"]}  (its PRIMARY role in a deck)
- spell_tier: {ENUMS["spell_tier"]}  ("none" for non-spells; Zap/Log/Snowball="small",
  Fireball/Poison="medium", Rocket/Lightning="big")
- is_win_condition: true/false  (is this a card decks are BUILT AROUND to damage towers)
- fragile_to_small_spell: true/false  (would a Zap/Log/Arrows wipe or badly hurt it —
  the spell-bait tell; true for most swarms and Princess/Dart Goblin/Goblin Barrel)
- special: any of {SPECIALS} that apply (empty list if none)
- source: the domain you verified from (e.g. "royaleapi.com")
- note: <=100 chars, only if something is genuinely ambiguous; else ""

EVOLUTION / HERO FORMS: when a card is given with form "evo" or "hero", describe THAT
FORM's behavior (it can differ from base — e.g. Evo Bats gain extra hitpoints and lifesteal,
Evo Knight gains a damage-reflecting dash). If a form's behavior is identical to base except
for stat boosts, keep the same primitives but reflect any tier change.

Return ONLY a JSON array, one object per card, each including the "name" and "form" you
were given so results can be matched. No prose, no markdown fence."""


def _client() -> anthropic.Anthropic:
    # Same key the bot uses (agent/core.py) — CLAUDE_API_KEY, not ANTHROPIC_API_KEY.
    return anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"), timeout=600)


def _extract_json(text: str):
    """Pull the JSON array out of the model's response text."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def enrich_batch(client: anthropic.Anthropic, cards: list[dict]) -> list[dict]:
    """One Opus call with web search over a small batch of card forms."""
    listing = "\n".join(
        f"- {c['name']} (form: {c['form']}, elixir: {c['elixir_cost']}, "
        f"type: {c['card_type']}, rarity: {c['rarity']})"
        for c in cards
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": f"Produce card facts for these {len(cards)} Clash Royale "
                f"card forms:\n\n{listing}\n\nReturn the JSON array.",
            }
        ],
    )
    # Server-tool turns can pause; resume until the model finishes.
    messages = [{"role": "user", "content": f"Produce card facts:\n\n{listing}"}]
    rounds = 0
    while resp.stop_reason == "pause_turn" and rounds < 5:
        rounds += 1
        messages = messages + [{"role": "assistant", "content": resp.content}]
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages,
        )
    text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = _extract_json(text)
    if parsed is None:
        raise ValueError(f"could not parse JSON from response: {text[:300]}")
    return parsed


def _forms_to_enrich(conn, limit: int | None) -> list[dict]:
    """Card forms observed in real decks that lack a card_facts row.

    Enriches only forms the clan has actually played or faced (from
    battle_card_plays), so we never spend on cards nobody sees.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT p.card_id, COALESCE(p.evolution_level, 0) evo,
               c.name, c.elixir_cost, c.card_type, c.rarity
          FROM battle_card_plays p
          JOIN card_catalog c ON c.card_id = p.card_id
         WHERE NOT EXISTS (
                   SELECT 1 FROM card_facts f
                    WHERE f.card_id = p.card_id
                      AND f.evolution_level = COALESCE(p.evolution_level, 0))
         ORDER BY c.name
        """
    ).fetchall()
    forms = [
        {
            "card_id": r["card_id"],
            "evolution_level": r["evo"],
            "form": {0: "base", 1: "evo"}.get(r["evo"], "hero"),
            "name": r["name"],
            "elixir_cost": r["elixir_cost"],
            "card_type": r["card_type"],
            "rarity": r["rarity"],
        }
        for r in rows
    ]
    return forms[:limit] if limit else forms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="max card forms to enrich")
    ap.add_argument("--dry-run", action="store_true", help="print results, don't write")
    ap.add_argument("--db", help="explicit DB path (else the default)")
    args = ap.parse_args()

    import db as db_facade

    conn = db_facade.get_connection(args.db) if args.db else db_facade.get_connection()
    try:
        forms = _forms_to_enrich(conn, args.limit)
    except Exception as exc:  # noqa: BLE001 - card_facts may not exist yet
        print(f"cannot read work list ({exc}); has the card_facts migration run?")
        return 1
    if not forms:
        print("nothing to enrich — every observed card form has facts.")
        return 0

    print(f"enriching {len(forms)} card form(s) with {MODEL} + web search...")
    client = _client()
    written = failed = 0
    for i in range(0, len(forms), BATCH):
        chunk = forms[i : i + BATCH]
        names = ", ".join(f"{c['name']}/{c['form']}" for c in chunk)
        print(f"  [{i // BATCH + 1}] {names}")
        started = time.time()
        try:
            facts = enrich_batch(client, chunk)
        except Exception as exc:  # noqa: BLE001 - one batch must not kill the run
            print(f"      FAILED: {exc}")
            failed += len(chunk)
            continue
        by_key = {(f.get("name"), f.get("form")): f for f in facts}
        for card in chunk:
            fact = by_key.get((card["name"], card["form"]))
            if not fact:
                print(f"      missing result for {card['name']}/{card['form']}")
                failed += 1
                continue
            if args.dry_run:
                print(f"      {card['name']}/{card['form']}: {json.dumps(fact)}")
            else:
                _write(conn, card, fact)
            written += 1
        print(f"      ({time.time() - started:.0f}s)")
    if not args.dry_run:
        conn.commit()
    conn.close()
    print(
        f"\ndone: {written} enriched, {failed} failed"
        + (" (dry run — nothing written)" if args.dry_run else "")
    )
    return 0


def _write(conn, card: dict, fact: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO card_facts "
        "(card_id, evolution_level, unit_domain, targets, attack_style, splash_hits_air, "
        " dps_tier, hp_tier, unit_count, range_type, role, spell_tier, is_win_condition, "
        " fragile_to_small_spell, special_json, source, note, model, enriched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            card["card_id"],
            card["evolution_level"],
            fact.get("unit_domain"),
            fact.get("targets"),
            fact.get("attack_style"),
            1 if fact.get("splash_hits_air") else 0,
            fact.get("dps_tier"),
            fact.get("hp_tier"),
            fact.get("unit_count"),
            fact.get("range_type"),
            fact.get("role"),
            fact.get("spell_tier"),
            1 if fact.get("is_win_condition") else 0,
            1 if fact.get("fragile_to_small_spell") else 0,
            json.dumps(fact.get("special") or []),
            str(fact.get("source") or "")[:100],
            str(fact.get("note") or "")[:200],
            MODEL,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
