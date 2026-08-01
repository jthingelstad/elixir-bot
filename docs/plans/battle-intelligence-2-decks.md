# Battle Intelligence — Feature 2: Deck & matchup intelligence

> **SUPERSEDED IN PART (2026-08-01, schema v32): the matchup matrix is gone.**
> `matchup_expectation`, `battle_enrichment.expected_advantage` / `performance`,
> the `matchup` tool view and `decisive_factor`'s matchup branch were all removed.
> Two measurements killed it. Symmetrizing revealed the stored advantages were
> largely the clan's own strength (control-vs-control read 58.4% and banded +1; a
> mirror is 50/50 by construction). Then a player-adjustment — each player's rate in
> a matchup minus their own baseline — put family matchup at a mean 3.2 points
> across 20 cells, against 22 points for card levels and a 34-point spread in player
> skill. Siege, the one apparent standout at 40.5% into beatdown, could not clear a
> four-player floor: that number was who plays siege. Deck PROFILING (this doc's
> other half) is alive and load-bearing. The archetype-vs-archetype half is not.


Status: **draft, re-scoped 2026-07-30**. Part of [Battle Intelligence
v1](battle-intelligence.md). **Depends on Feature 1** (form-aware `deck_hash`,
`battle_card_plays`, the `get_battle_intelligence` tool). Unblocks Feature 3
(prose needs both decks' profiles).

> **What changed (2026-07-30): the archetype LABEL is now rules, not LLM.**
> We measured the existing classifier and found the "broken / 47% hybrid" premise
> was a raw-vs-enriched measurement artifact (see overview §Context). A
> win-condition-driven rules rebuild in `capabilities/decks.py` (`_classify` /
> `_archetype`, **shipped**) labels every deck for **$0** with a ~2% honest
> residual. So this feature no longer spends a model on labels. Its LLM scope
> narrows to **(a) the archetype-pair matchup matrix** — the genuine model value
> rules can't produce — and **(b) optional per-deck dimensional scores**, kept
> only if they beat measured stats. The `deck_profile.archetype` column is filled
> by `_classify`, not a prompt.

## Goal

Put an **expected advantage on every archetype matchup** so the brain can reason
about decks it has never seen exactly (10k+ distinct decks, 86% seen once — only
archetype generalizes, and archetype is now a rules lookup). Then fill Feature 1's
deferred `expected_advantage` / `performance` columns, turning the computed layer
into an **upset detector**. Optionally attach LLM **dimensional scores**
(defense/coherence) to each meaningful deck — but only after checking they add
signal over the measured win rates the blend already gives us.

This is the LLM layer, but a thin one now (~$1/era for the matrix; labels are
$0) — **not** the per-battle prose (Feature 3).

### Non-goals

- **No per-battle prose** (`loss_nature`/`commentary`) — Feature 3.
- **The `capabilities/decks.py` `_archetype()` cascade IS the archetype source
  now** (rebuilt 2026-07-30 — win-condition rules, `family` + `label`). We are
  not adding a competing LLM vocabulary; this feature consumes `_classify`. The
  old coarse branch logic (`hybrid`, etc.) is already gone.
- No coaching, no awareness posting.

## 0. The "other"-bucket gate — RESOLVED (rules, not LLM)

This gate existed because an LLM enum might dump real meta decks (Mega Knight,
Goblin Giant, Sparky…) into `other`, making the matrix noise. **Moot now**: the
rules classifier (`capabilities/decks.py`) has explicit homes for those win
conditions, and its residual is measured at **~2% of battles** (`unclassified`,
decks with genuinely no win condition) — far below the 15% that would have
forced an enum revision. The matchup matrix is keyed on the six `family` values
plus the specific labels, and `unclassified` decks are simply excluded from
matrix cells rather than polluting them.

## 1. Schema — `_apply_v28` (two tables)

Each feature owns its migration (Feature 1 = v27). `EXPECTED_TABLE_COUNT`
**61 → 63**; update `REQUIRED_SCHEMA`; bump the `db/schema.py` hygiene baseline.

```sql
CREATE TABLE deck_profile (
    deck_hash        TEXT NOT NULL,       -- Feature 1's FORM-AWARE hash (id,evo pairs)
    valid_from       TEXT NOT NULL,
    superseded_at    TEXT,                -- NULL = current era
    cards_json       TEXT NOT NULL,       -- the (id, evolution_level) set, for audit
    family           TEXT NOT NULL,       -- from _classify; CHECK against decks.py family set
    archetype        TEXT NOT NULL,       -- from _classify label (e.g. "Hog Cycle")
    win_condition_type TEXT,              -- air|ground|siege|spell|bridge|none
    defense_air      INTEGER CHECK (defense_air BETWEEN 0 AND 5),
    defense_tank     INTEGER CHECK (defense_tank BETWEEN 0 AND 5),
    defense_swarm    INTEGER CHECK (defense_swarm BETWEEN 0 AND 5),
    spell_bait_vuln  INTEGER CHECK (spell_bait_vuln BETWEEN 0 AND 5),
    coherence        INTEGER CHECK (coherence BETWEEN 0 AND 5),
    avg_elixir       REAL,                -- COMPUTED from card_catalog, NULL for Mirror decks
    model            TEXT NOT NULL,
    prompt_version   INTEGER NOT NULL,
    scored_at        TEXT NOT NULL,
    PRIMARY KEY (deck_hash, valid_from)
);

CREATE TABLE matchup_expectation (
    our_archetype    TEXT NOT NULL,       -- a `family` value from _classify
    their_archetype  TEXT NOT NULL,       -- a `family` value from _classify
    valid_from       TEXT NOT NULL,
    superseded_at    TEXT,
    advantage        INTEGER NOT NULL CHECK (advantage BETWEEN -2 AND 2),
    basis            TEXT,                -- one sentence, model
    model            TEXT NOT NULL,
    prompt_version   INTEGER NOT NULL,
    PRIMARY KEY (our_archetype, their_archetype, valid_from)
);
```

- **`deck_hash` is Feature 1's form-aware hash** (sorted `(card_id,
  evolution_level)` pairs). So Evo-Knight and base-Knight decks profile
  **separately** — correct, since a card's Evo/Hero form shifts the dimensional
  scores (defense, coherence). It also means `deck_profile.deck_hash` joins
  `battle_enrichment.our_deck_hash`/`their_deck_hash` cleanly (same hash).
- **`avg_elixir` is computed, never the model's** — mean `card_catalog.elixir_cost`
  over the 8 deck cards. **NULL when the deck contains Mirror** (the one playable
  card with no fixed cost; the other 4 cost-less catalog rows are tower troops,
  not deck cards). The profile prompt tolerates a null avg.
- **Era columns** (`valid_from`/`superseded_at`): supersede, never overwrite
  (§5). A deck/cell has exactly one row with `superseded_at IS NULL` — the
  current era.

## 2. Archetype vocabulary — `capabilities/decks.py` (shipped rules)

**No new enum module.** The archetype vocabulary is the `family` set and the
`_classify` label logic already in `capabilities/decks.py` (rebuilt 2026-07-30):

- **`family`** (the matrix axis): `beatdown`, `control`, `cycle`, `bait`,
  `bridge spam`, `siege`, plus `unclassified` (excluded from the matrix).
- **`label`** (~40 conventional names, e.g. `Hog Cycle`, `Lavaloon`, `Log Bait`,
  `Mega Knight Bridge Spam`): the player-facing name shown in tool output.

There is **one** archetype source, in code, deterministic — so nothing can drift
from it and the "fourth disagreeing vocabulary" risk is gone. The matchup matrix
(§4) is keyed on `family` pairs. If the matrix ever needs a finer axis than the
six families, add it in `decks.py` and re-key — but start with `family`, which
the exploration's signal (bait→control, hybrid→beatdown) was already measured at.

`deck_profile.archetype` (§1) stores the `_classify` `family`/`label` for the
deck's hash — a cached projection of the rules output, never a model call.

## 3. Deck coverage — universal, no frequency floor

The old plan gated deck profiling behind a **member ∪ seen ≥3×** frequency floor.
That floor existed for exactly one reason: to cap the cost of an **LLM profiling
each deck** (~$0.001 each, 11k decks). We deleted that cost — archetype is now a
$0 rules call — so **the floor is deleted too. Every 1v1 deck on either side is
classified, any frequency.**

- `deck_profile` is written for **all ~11,152 distinct decks** (trivial storage),
  both sides.
- Feature 1's **`expected_advantage` fills for every battle whose both decks
  classify** (≈98%; only `unclassified` abstains). An opponent's one-off deck
  still has a known archetype and a known matchup cell — nothing is gated on how
  often a deck recurs. The old "hapax deck → NULL profile → NULL advantage"
  degradation is simply gone.

**The only frequency notion that survives is a statistical *claim*-floor, and it
lives in the tool, not in coverage.** An aggregate win-rate claim (card / deck /
matchup) needs **n≥30**; below that the capability returns `insufficient_sample`,
never a weak number. That gates what we *assert* ("this matchup wins 75%"), not
what we *classify* — every deck is labelled; we just don't make a strong claim
off n=4. Coverage is universal; claim strength is floored. Two different things.

*(If dimensional scores are ever revisited — cut from v1, §7 — score decks
**lazily on query** for the few anyone actually asks about, never on a recurrence
floor. Query-driven is a tighter, more honest bound than "seen ≥3×".)*

## 4. Matchup matrix

**DECISION LOCKED (2026-07-30): the matrix axis is `family` — 6 families → 36
ordered cells.** Not the specific labels, not the old 20-value enum. Rationale:
36 cells is where the exploration's measured signal was validated (bait→control,
hybrid→beatdown), it's ~10× cheaper than a label-level matrix, and any label-level
nuance is recoverable later by re-keying without re-backfilling data. Dimensional
scores and the frequency floor are both **cut** (§3, §7), so this matrix is the
*entire* remaining LLM surface of Feature 2.

`our × their`, direction matters (cycle *into* beatdown ≠ the reverse). LLM once
per era, `advantage` in −2..+2 + a one-sentence `basis`.

- **`unclassified`**: never scored — a deck with no win condition has no
  meaningful matchup. Skip those cells entirely; the tool synthesizes an
  `insufficient` response for any pair touching `unclassified`.
- Cells amortize: 36 one-time, then re-scored only on the §5 triggers.

## 5. Freshness — model = prior, measured data = corrector

- **Supersede, never overwrite**: era rows via `valid_from`/`superseded_at`.
  Feature 1's `battle_enrichment.expected_advantage` is a **snapshot** taken at
  enrich time, so changing eras never rewrites battle history.
- **Calibration** (weekly, SQL only): per live cell, measured member win-rate vs
  the predicted `advantage` band, with n. Drift + sufficient n ⇒ flag the cell.
  Detects stat-only balance patches the API can't show and the model may not
  know. Anchor sanity: exploration measured **bait into control −9.9 pts
  (n=265)** and **hybrid into beatdown +7.3 (n=1,202)** — calibration must
  reproduce these directions.
  - **Intra-clan dedup**: a battle between two current members appears twice in
    `battle_events` (once per member as subject, sides swapped, `advantage`
    mirrored), so it double-counts in the win-rate. Measured at **2.3%** of
    member 1v1 battles (264/11,273) — small, but dedup the calibration query on
    the unordered `{player_tag, opponent_tag}` pair so a friendly isn't weighed
    twice. (Same class as the Feature 1 `card`-view double-count.)
- **Blend** (in the `matchup` tool view, not stored): `effective_advantage =
  w·measured + (1−w)·model`, `w = n/(n+30)`, measured mapped to −2..+2.
  Self-heals balance patches without a model call and corrects model error from
  day one. The stored `advantage` stays the model prior; the blend is a read.
- **Re-score triggers**, in order of trust: calibration alarm (one cell) →
  `card_catalog` diff (new card / new evolution / elixir-cost change → re-score
  decks containing it + that archetype's cells) → `season_started` event (cheap
  full re-score) → manual with patch notes pasted into the prompt (the only fix
  for the model's training cutoff).

## 6. Workers — Stage B (LLM), extends `runtime/jobs/_battle_intel.py`

Adds the LLM half behind Feature 1's Stage-A cursor job. **Every 30 min, capped
per run, Haiku:**

1. **Classify** every deck needing a `deck_profile` row via `_classify` — rules,
   both sides, **$0, no frequency floor**. Compute `avg_elixir` from
   `card_catalog` here too (never the model). *(Dimensional scores are cut from
   v1; if ever revisited, score lazily on query, not on a recurrence gate.)*
2. **Score** missing `matchup_expectation` cells for the current era (skip any
   cell touching `unclassified`).
3. **Fill** `expected_advantage` (snapshot the current cell's `advantage`) and
   `performance` (−1|0|+1: outcome vs expectation) into `battle_enrichment`
   wherever both decks' archetypes + the cell now exist — i.e. nearly every 1v1
   battle, since archetype is universal. **Pure SQL, $0.**

- **Telemetry**: `mark_job_start/success/failure` with counts — `"profiled +N
  decks, scored +M cells, filled +K expectations"`. A caught-up run says 0
  distinctly from a broken one.
- **Workflow names** `deck_profile`, `matchup_score` through the standard
  no-tool workflow path (like `generate_clan_chat_copy`) so `llm_calls`
  telemetry and the cost skill see them.

## 7. Prompts (2, Haiku, versioned)

Shared honest framing (the framing that made the experiment stable): state what
the model **cannot** see (no play-by-play, no ladder results), allow an explicit
`insufficient`, constrain labels to the enum. Return **dimensional scores, not
verdicts** — scores were stable across the consistency test; verdicts
flip-flopped on identical input.

1. **Deck dimensional scores** *(optional — keep only if they beat measured
   stats)* — 8 cards *with form* (base/Evo/Hero, per Feature 1's `card_form`) +
   catalog elixir costs → `win_condition_type` + five 0–5 scores
   (`defense_air/tank/swarm`, `spell_bait_vuln`, `coherence`). **`archetype` is
   NOT asked — it comes from `_classify`.** Form is in the prompt because
   Evo/Hero shift defense and coherence.
2. **Matchup cell** — two `family` values → `advantage` −2..+2 + one-sentence
   `basis`. No card lists; archetype-level only. This is the load-bearing LLM
   call now that labels are free.

Both versioned; a bump is an era re-score, auditable via `prompt_version`.

## 8. Tool — extend `get_battle_intelligence` with `deck` + `matchup` views

Same tool from Feature 1 (no new wiring — the tool already exists); add two
views to its dispatch and prompt docs:

- **`deck`**: a deck's profile (archetype/`family`, avg_elixir) + its observed
  W/L from `battle_card_plays`. **Every deck has an archetype** (no "unprofiled"
  state); the only gap is a thin sample — a deck seen `n<30` returns its label
  but flags the W/L `insufficient_sample`, never a weak rate.
- **`matchup`**: archetype pair → stored `advantage`, the blended
  `effective_advantage`, measured n, and calibration state. Below-n cells report
  the model prior only, flagged low-confidence. Floors live here.

## 9. Prompt-truth — extend Feature 1's fix

Feature 1 unblocked opponent **card** claims. Feature 2 unblocks **archetype /
deck-matchup** claims:

- `agent/prompt_builders.py` — extend the `get_battle_intelligence` guidance to
  cover deck archetype + matchup expectation (with the calibration/​n caveats).
- `capabilities/decks.py` — the `_archetype()` cascade still stays untouched;
  only the prompt now points at the richer tool for archetype matchups.

## 10. Cost (measured basis)

| item | volume | cost |
|---|---|---|
| archetype labels (all decks) | universal | **$0** (rules) |
| matchup matrix | ≤~36 `family`-pair cells/era | **~$1/era** |
| `expected_advantage` / `performance` | ~all 1v1 battles | **$0** (SQL) |
| deck classification (all ~11k decks) | universal, no floor | **$0** (rules) |
| deck dimensional scores | cut from v1 (lazy-on-query if ever) | $0 in v1 |

## 11. Build order (each = one commit, suite + simulator green)

1. ~~`engine/deck_archetypes.py` enum + parity test~~ **DONE differently** —
   archetype is `capabilities/decks.py` `_classify` (shipped). Nothing to build;
   the `deck_profile.archetype` CHECK, if any, validates against the `family`
   set exported from `decks.py`.
2. `_apply_v28` (deck_profile + matchup_expectation) + schema bookkeeping.
   Validate on a copy.
3. Stage-B classify+cache step: write `deck_profile` rows via `_classify` ($0),
   both sides, for all decks. *(No `other`-bucket gate — it's resolved; residual
   is ~2% `unclassified`, excluded from the matrix.)*
4. Matchup prompt + `family`-pair cell scoring + `expected_advantage`/
   `performance` fill.
5. Calibration job (weekly) + blend in the `matchup` view.
6. `deck` + `matchup` tool views + prompt-truth extension.
7. No restart needed unless the v28 migration ships separately from a code
   deploy — it does (migration = deploy); back up, `admin.sh restart`, ask first.

## 12. Verification (validate data, not just errors)

1. **Archetype residual** — `unclassified` share stays ~2% of battles (already
   measured live); no fabricated labels. (The old `other`-bucket gate is retired.)
2. **Sanity known decks by hand**: Hog 2.6 → `Hog Cycle`; a Lava Hound + Balloon
   deck → `Lavaloon`; a Mirror deck classifies with `avg_elixir` averaged over
   its other 7 cards (not NULL) and no crash.
3. **Calibration reproduces exploration**: bait→control negative, hybrid→beatdown
   positive, at the measured magnitudes.
4. **Join integrity**: every `battle_enrichment.our_deck_hash` resolves to a
   `deck_profile` row with a `family`/`archetype` from `_classify` (universal —
   only `unclassified` decks abstain from matrix cells).
5. **Idempotency + era discipline**: re-run profiling → no new rows; an era
   re-score leaves the old row with `superseded_at` set, never deletes it.
6. Suite + war-week simulator green (asserts `EXPECTED_TABLE_COUNT` = 63).

## 13. Risks

| risk | guard |
|---|---|
| ~~enum too coarse → matrix is noise~~ | **retired** — archetype is rules; ~2% `unclassified` residual, measured, excluded from the matrix |
| model archetype instability | **retired** — labels are deterministic rules, not a model; calibration + blend still correct the matrix `advantage` |
| model staleness after balance patch | calibration alarm + blend + era supersede + `card_catalog`-diff re-score |
| ~~form-aware hash inflates deck count~~ | **not a problem** — classifying all ~11k decks is a $0 rules pass; storage is trivial, no floor needed |
| ~~unprofiled opponent decks~~ | **gone** — every deck is classified ($0 rules); only `unclassified` (no win condition, ~2%) abstains from matrix cells |
| era snapshot drift (performance judged vs a stale era) | documented: `performance` is point-in-time; re-derivable by re-snapshotting if ever needed |
| silent worker death | work-set counts in Stage-B telemetry |
