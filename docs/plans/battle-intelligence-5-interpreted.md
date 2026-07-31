# Battle Intelligence — v2: the interpreted layer (card facts → deck facts → battle tags → summarizer)

Status: **designed 2026-07-31**. Owner: Jamie. Supersedes Feature 3 (per-battle prose).
Builds on the deployed Features 1–2 (schema v29).

## Why this pivot (what Feature 3 taught us)

Feature 3 generated a prose paragraph per battle. Shipped, gated, and read on live
data — then judged a failure on its merits, which is what the gate was for:

- **A player will never read a paragraph about each battle they play.** Volume is
  wrong: ~10 battles/day/member.
- **The text added nothing arithmetic didn't.** The model received only computed
  numbers (archetypes + margins + discipline + level_gap), so it could only
  re-narrate them. *"It could just be a template driven by a regular expression"*
  (Jamie). It reached for **elixir discipline in nearly every battle** because that
  was the one number that varied.
- **It narrated invalid facts.** It cited "4+ levels down" on `Showdown_Friendly`
  battles, where levels are normalized — we fed it a meaningless `level_gap` and it
  dutifully explained it.

The root cause is structural, not prompt quality: **the model was never given
anything to judge with.** It never saw the cards.

**The pivot:** the LLM's job is not to write per-battle text. It is to produce
**structured judgment on immutable things** (cards), which computed layers turn
into per-battle facts, which a single on-demand LLM turns into insight *when a
player actually asks*.

## The stack

| layer | what | who fills it | cost |
|---|---|---|---|
| **1. card facts** | per `(card_id, evolution_level)` behavior primitives | **LLM (Opus + web search)**, rare | ~$ one-time, pennies/refresh |
| **2. deck facts** | per `deck_hash` completeness (air answers, tank answers, bait…) | SQL from layer 1 | **$0** |
| **3. battle tags** | per battle structural read (air matchup, win-con pressure, decisive factor) | SQL from layers 1–2 + battle | **$0** |
| **4. summarizer** | "how have I been playing?" | **LLM on demand**, member-triggered | per-ask only |

Layers 2–3 are computed, so the per-battle path has **no model call at all**.

## Layer 1 — card facts (the one real LLM job)

`card_facts`, keyed **`(card_id, evolution_level)`** — Evo/Hero forms are distinct
cards and are enriched separately (the form-aware identity Features 1–2 already use).

**The CR API gives us nothing behavioral** (verified against stored payloads: only
`id, name, elixirCost, rarity, maxLevel, maxEvolutionLevel, iconUrls`), so this
layer cannot be derived — it must be enriched.

### Primitives the model asserts

| field | values |
|---|---|
| `unit_domain` | air / ground / none |
| `targets` | ground / air_and_ground / buildings_only / none |
| `attack_style` | single / splash_small / splash_large / chain / none |
| `splash_hits_air` | bool |
| `dps_tier` | low / medium / high |
| `hp_tier` | low / medium / high |
| `unit_count` | one / few / many |
| `range_type` | melee_short / melee_medium / melee_long / ranged / siege / none |
| `role` | win_condition / tank / mini_tank / support / swarm / spell / building / spawner / champion |
| `spell_tier` | small / medium / big / none |
| `special` | multi: knockback, reset, charge, spawns_units, freeze, rage, heal, invulnerable |
| `is_win_condition` | bool |
| `fragile_to_small_spell` | bool |

**Tiers, not raw stats.** Raw HP/damage are level-dependent and move every balance
patch; tiers are stable and are all the reads need. Asking for exact numbers would
be fragile and pointless.

**No explicit counter lists.** "Inferno counters Golem" is a hallucination and
staleness magnet, and it is *derivable* from primitives (high-dps single-target vs
high-hp ground). Attribute-derived counters stay correct as the meta moves.

### Enricher
- **Model: Opus** (Jamie: *"quality is the differentiator… one of the most important
  data assets we'll have"*), with the **native web_search tool** so it reads current
  card behavior rather than trusting a training cutoff. Cites its source per card.
- Runs as a **script/job**, batched, never on a hot path. ~126 forms.
- **Refresh triggers** (mirroring Feature 2): `card_catalog` diff (new card / new
  evolution / elixir-cost change) → re-enrich that form; season/balance event →
  optional sweep; manual re-run with patch notes for a rework.
- `source` column (`llm_web`) is recorded. **Leader override is deliberately NOT
  built** (Jamie, 2026-07-31) — a web-search-grounded enricher should be good
  enough, and the column makes override a later widening, not a migration.

### Why no slowly-changing-dimension versioning
Two different things change, and only one is a card fact:
- **Intrinsic behavior** (air/ground, targets, role) is *very* stable — Balloon has
  always been an air building-targeting win condition. Only a true **rework**
  changes it, which is rare and effectively makes a new card.
- **Meta strength** changes constantly — but we already *measure* that in Feature
  2's matchup matrix from real win rates. It must not be duplicated onto the card.

So `card_facts` is **current-state**, and era-faithfulness comes from **snapshotting
the derived battle tags** (layer 3) at enrichment time — the same "snapshot, never
rewrite history" move Feature 1 uses for `expected_advantage`. Full temporal
versioning would be heavy machinery for a dimension that barely moves.

## Layer 2 — deck facts (computed, $0)

Per `deck_hash`, from its 8 cards' facts. Anchored on the community **deck formula**
(*one win condition, one big spell, one small spell, one tank answer, one air
answer, one splash answer, two cheap cycle cards*):

`air_answer_count`, `tank_answer_count`, `splash_answer_count`, `has_win_condition`,
`win_condition_types`, `has_big_spell`, `has_small_spell`, `swarm_count`,
`bait_unit_count`, `cycle_card_count`.

Derived roles (SQL, not model): `is_air_answer` = targets ⊇ air; `is_tank_answer` =
`dps_tier=high ∧ attack_style=single`; `is_splash_answer` = splash; `is_swarm` =
`unit_count=many`; `is_cycle_card` = elixir ≤ 2.

This is what makes *"your deck has zero air answers"* a **computed fact**, not a
model opinion.

## Layer 3 — per-battle structural tags (computed, $0, snapshotted)

Replaces prose. On `battle_enrichment`:

- `air_matchup` — favored / even / stressed (their air threat vs our air answers)
- `wincon_pressure` — countered / contested / clear (their defense vs our win condition)
- `spell_bait_exposed` — did we bring bait units into their small spell
- **`level_validity`** — real / normalized. **Fixes the Feature-3 bug**: suppress
  level claims on `is_ranked` *and* level-capped special events (Showdown), not just
  ranked.
- `decisive_factor` — a ranked heuristic over level gap / discipline / margin /
  matchup. **This is what kills the elixir crutch**: the driver becomes a computed
  ranking, not the model's fallback narrative.

## Layer 4 — the summarizer (the only reader-facing LLM)

Triggered when a member asks ("how have I been playing?"). **Open to any member for
themselves** (Jamie's call) via `#ask-elixir`; it is the core use.

Input: recent battles + layer-3 tags + aggregates (record, upset rate, family
matchup performance, recurring weaknesses). Output: a real coaching read across
*many* battles —

> *"Your Royal Hogs deck has an air hole — Musketeer is your only air answer, and 4
> of your last 6 losses were to Balloon/Lava decks. Against building-heavy control
> you're 0-for-4: your win condition gets walled. When you experiment (Mortar,
> X-Bow) you're actually winning upsets."*

Cost scales with **asks**, not with battle volume.

## What retires

- The `battle_prose` workflow, `_battle_intel_prose` job, `generate_prose_batch`,
  and the `battle_enrichment_enabled` allowlist gate — **fully retired** (Jamie).
- The 63 generated prose rows stay as a reference artifact; `commentary`/
  `loss_nature` columns are repurposed or left dormant.
- `scripts/grade_battle_prose.py` is superseded (the summarizer is what gets graded
  now, and it is graded by being *asked*).

## Build order

1. `card_facts` schema + **Opus web-search enricher** + spot-check a sample against
   Deckshop/Fandom. *Everything stands on this.*
2. Layer 2 deck facts (computed) on `deck_profile`.
3. Layer 3 battle tags (computed, snapshot) + `level_validity` fix; **retire prose**.
4. Layer 4 summarizer + `#ask-elixir` routing.

## Risks

| risk | guard |
|---|---|
| enriched card facts are wrong | web search grounds them; spot-check a sample vs Deckshop/Fandom; tiers not raw stats; primitives are simple + checkable |
| meta shifts and facts go stale | refresh triggers (catalog diff / season / manual rework re-run); strength lives in the *measured* matrix, not the card |
| derived roles disagree with intuition | roles are SQL over primitives — auditable and fixable in one place, no re-enrichment |
| summarizer over-claims | it reads computed tags + aggregates; same "claims trace to provided facts" guard, now with real structure behind it |
| Evo/Hero treated as base | keyed `(card_id, evolution_level)` throughout |
