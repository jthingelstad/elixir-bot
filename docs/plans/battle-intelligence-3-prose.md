# Battle Intelligence — Feature 3: Per-battle prose (gated)

Status: **draft** (2026-07-30). Part of [Battle Intelligence
v1](battle-intelligence.md). **Depends on Feature 1** (computed metrics) **and
Feature 2** (both decks' profiles feed the prompt). Unblocks nothing in v1; v2
coaching builds on it.

## Goal

Generate a short, public-safe **`commentary`** per battle plus a structured
**`loss_nature`** and a **`notable`** flag — the model *explaining the computed
numbers*, not inventing observations. And ship the **eval benchmark**
(`verdict`) that decides when the prompt is good enough to open the gate.

This is the one genuinely **per-player, iterate-heavy, regenerate-often** LLM
output, so it is the **only** layer that is gated. Everything upstream (Features
1–2) is universal; this is scoped to a tiny allowlist so we can iterate the
prompt for pennies instead of spending ~$42/mo clan-wide on prose we will keep
rewriting.

### Non-goals

- **`coaching_note`** (personal critique) and its requester-identity plumbing —
  v2. This feature writes `commentary` that is safe for **any** asker; it never
  generates personal critique, so it needs no identity scoping.
- **Awareness-loop posting** from `notable` — v2 (the `notable` flag is written
  here; *using* it to post proactively is deferred).
- **Clan-wide prose** — that is *opening the gate* later (an ops flip, §2), not a
  feature. The eval benchmark (§6) is what earns that flip.

## The gate — the whole point of this feature

A battle gets prose iff **all three** hold:

1. It has a Feature-1 `battle_enrichment` row (⇒ 1v1, non-duel, non-2v2).
2. Its member is **allowlisted** (`player_metadata.battle_enrichment_enabled = 1`;
   seed: King Thing `#20JJJ2CCRU`, raquaza `#UL2V9QRG0`).
3. `battle_time >= BATTLE_PROSE_MIN_DATE` (`'2026-07-20'`).

At that scope: **94 battles backfill (~$0.34) + ~$1/mo**, versus ~$42/mo
clan-wide. The gate is a **cost/iteration control, not a privacy control** —
`commentary` is public-safe, so the tool may surface it to any asker; the gate
only limits *what we pay to generate*. Privacy scoping arrives with
`coaching_note` in v2.

**Opening the gate later** is a one-line change, not a rebuild: relax the
allowlist join to all members and drop/lower `BATTLE_PROSE_MIN_DATE` in the
Stage-B prose query. Do it when §6's benchmark says the prompt is stable.

## 1. Schema — `_apply_v29` (one column, no new table)

The prose columns (`loss_nature`, `notable`, `confidence`, `commentary`,
`verdict`, `model`, `prompt_version`, `input_hash`, `enriched_at`) **already
exist** — Feature 1 shipped `battle_enrichment`'s full shape. This migration
adds only the **allowlist flag**, the one bit of source-of-truth state in v1:

```sql
ALTER TABLE player_metadata
  ADD COLUMN battle_enrichment_enabled INTEGER NOT NULL DEFAULT 0;

-- seed the allowlist (both rows already exist in player_metadata):
UPDATE player_metadata SET battle_enrichment_enabled = 1
  WHERE player_tag IN ('#20JJJ2CCRU', '#UL2V9QRG0');
-- (defensive INSERT OR IGNORE first if a future allowlist tag lacks a row;
--  player_metadata is sparse — 132/151 members.)
```

- **`EXPECTED_TABLE_COUNT` unchanged (63)** — no new table. Still update
  `REQUIRED_SCHEMA` (the new column) and bump the `db/schema.py` hygiene
  baseline (+1 for `_apply_v29`).
- Migration = deploy: validate on a copy, back up, `admin.sh restart`, ask first.

## 2. Prompt 3 — battle prose (Haiku, versioned)

**Inputs**: the battle record (mode, outcome, crowns, trophies) + **both deck
profiles** (archetype + dimensional scores, from Feature 2) + **the computed
metrics** (`hp_margin`, `closeness`, `discipline_delta`, `level_gap`,
`expected_advantage`, `performance` — from Features 1–2). Card **forms**
(base/Evo/Hero) are in the deck profiles.

**Outputs** (structured):

- `loss_nature` ∈ `structural | piloting | level | close | unclear` (NULL on a
  win, or when unclear). Anchored to the numbers: `level` must be supported by
  `level_gap`, `close` by `closeness`, etc.
- `notable` ∈ `0|1` — is this battle worth a human's attention (an upset per
  `performance`, a squeaker, a structural stomp)? Drives §4 ordering and v2
  posting.
- `confidence` — the model's own hedge.
- `commentary` — **1–2 sentences, public-safe, insight-flavored, not personal
  critique**. Explains the computed read in words.

**Framing that kept the experiment honest** (carry verbatim from the 30-battle
run): state what the model **cannot** see (no play-by-play, no elixir timeline),
allow `insufficient`, and the hard guard — **final-state claims must reference
the provided numbers**. The two hallucination slips in the experiment both came
from free-form final-state reading; the guard closes that.

The model **explains data, it does not invent observations.** Judgment is
minimized to `loss_nature`/`notable` (dimensional, gradeable); everything
factual comes from the computed columns.

## 3. Worker — Stage B prose step (extends `runtime/jobs/_battle_intel.py`)

Adds one step to Feature 2's Stage-B LLM job. **Every 30 min, capped per run,
Haiku:**

- Select gated battles (§gate) **missing prose at the current `prompt_version`**,
  **oldest-`notable`-first** — actually: order unenriched by `battle_time` but
  prioritize any whose computed `performance != 0` (likely `notable`) so upsets
  surface first within the cap.
- For each, build the prompt (§2), write `loss_nature`/`notable`/`confidence`/
  `commentary` + `model`/`prompt_version`/`input_hash`/`enriched_at`.
- **Telemetry**: `mark_job_start/success/failure` with counts (`"prose +N,
  refreshed +R stale, gated-eligible-remaining M"`). Workflow name
  **`battle_prose`** through the standard no-tool workflow path, so `llm_calls`
  telemetry and the cost skill see it.

## 4. Idempotency & refresh — `input_hash` semantics

`input_hash = sha256` over everything the prose depends on:

- the computed metrics (`hp_margin`, `closeness`, `discipline_delta`,
  `level_gap`, `expected_advantage`, `performance`),
- **both deck profiles' identity** (`deck_hash` + that profile's
  `prompt_version` + `valid_from`), so a Feature-2 re-profile **auto-refreshes**
  the prose,
- the prose `prompt_version`.

Skip a battle when a row exists with the same `input_hash` **and** current
`prompt_version`. A prompt bump or any upstream change ⇒ hash differs ⇒
regenerate. At 94 gated battles, refresh is free, so we can afford to chase
upstream changes rather than let prose drift stale.

## 5. Tool — surface prose in the existing views (no new wiring)

The `get_battle_intelligence` tool already exists (Feature 1); Feature 3 adds
**no** new tool and **no** `_SHARED_TOOL_NAMES` change. It only makes two
existing views surface prose **where present** (NULL for non-gated battles):

- **`battle`**: include `loss_nature`, `notable`, `confidence`, `commentary`
  alongside the computed read.
- **`member_summary`**: for allowlisted members, a few recent `commentary`
  lines + `loss_nature` distribution. For non-allowlisted members these are
  simply absent — the computed summary still returns.

The capability states plainly when prose is absent ("commentary not generated
for this member") so the brain never treats NULL as "no issues."

## 6. Eval — the `verdict` benchmark (this is what opens the gate)

Prompt iteration without a benchmark is vibes. `battle_enrichment.verdict` ∈
`accurate | wrong | useful` is **Jamie's grade of his own battles' output**.

- **Grading path**: a small script `scripts/grade_battle_prose.py` pulls Jamie's
  recent enriched battles (his tag, prose present), shows each battle's
  `commentary` + the computed read + the actual result, and writes his tag to
  `verdict`. ~20 battles is the target set.
- **The rule**: **no prompt v2 ships until ~20 of Jamie's own battles are graded
  against v1.** v2 is then measured by regenerating those 20 and comparing
  verdict deltas — a real before/after, not a vibe.
- The graded set is also the **gate-opening criterion**: a stable, mostly-
  `accurate`/`useful` verdict distribution is the evidence that clan-wide prose
  is worth turning on.

## 7. Cost

| item | volume | cost |
|---|---|---|
| backfill prose (seed × since 07-20) | 94 | **~$0.34 one-time** |
| ongoing prose (seed members) | ~9/day combined | **~$1/mo** |
| refresh on Feature-2 re-profile | ≤94 | pennies, rare |

Clan-wide would be ~$44 backfill + ~$42/mo — the gate saves ~97%. The
`battle_prose` workflow makes the real number visible in `llm_calls`.

## 8. Build order (each = one commit, suite green)

1. `_apply_v29` (allowlist column + seed) + schema bookkeeping. Validate on a
   copy.
2. Battle-prose prompt (v1) + Stage-B prose step, gated. Backfill the 94 on a
   copy; eyeball the output.
3. `battle`/`member_summary` view prose surfacing + capability "prose absent"
   messaging.
4. `scripts/grade_battle_prose.py` + grade ~20 of Jamie's battles → the v1
   benchmark.
5. **Restart** (v29 migration) — back up, ask first.

## 9. Verification (validate data, not just errors)

1. **Gate holds**: prose rows exist **only** for allowlisted members, only for
   `battle_time >= 2026-07-20`, only for 1v1 non-duel battles. Assert zero prose
   outside the gate.
2. **No hallucinated final states**: spot-read 10 commentaries against the
   actual battle — every final-state claim traces to a provided number.
3. **`loss_nature` consistency**: `level` verdicts have a real `level_gap`;
   `close` verdicts have high `closeness`. Cross-check the enum against the
   computed columns.
4. **Refresh works**: bump `prompt_version`, confirm the 94 re-generate; force a
   Feature-2 re-profile on a deck, confirm its battles' `input_hash` changes.
5. **Cost sanity**: the `battle_prose` rows in `llm_calls` sum to ~$0.34 for the
   backfill; ongoing tracks ~$1/mo.

## 10. Risks

| risk | guard |
|---|---|
| prose hallucinates a final state | "must reference provided numbers" guard; verification §9.2; judgment minimized to loss_nature/notable |
| personal critique leaks to wrong audience | none generated — `commentary` is public-safe; coaching + identity scoping are one v2 unit |
| prompt iteration blindness | `verdict` benchmark graded against Jamie's own games before any v2 |
| prose drifts stale after a re-profile | `input_hash` includes both deck profiles' identity → auto-refresh |
| gate leaks (prose for non-allowlisted / pre-07-20) | gate is a WHERE clause on the Stage-B query + verification §9.1 asserts zero leakage |
| cost creep if gate opened prematurely | opening is a deliberate flip earned by §6; `battle_prose` in `llm_calls` makes spend visible |
| silent worker death | work-set counts in Stage-B telemetry |
