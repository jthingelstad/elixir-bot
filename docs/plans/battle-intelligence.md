# Battle Intelligence — v1 Overview

Status: **designed, being built in features** (2026-07-30). Owner: Jamie.
Design partner: Claude.

This is the index. The buildable work lives in per-feature plans; this doc holds
the shared context, the one architectural principle, the cross-cutting lessons
every feature inherits, and the coverage/cost decision that splits them.

## Feature map (dependency order)

| # | plan | scope | LLM? | cost | ships |
|---|---|---|---|---|---|
| 1 | [`battle-intelligence-1-data.md`](battle-intelligence-1-data.md) | `battle_card_plays` + computed `battle_enrichment` + card/nemesis/battle tool views | **none** | **$0** | card matchup data, nemesis, adoption, battle closeness — clan-wide, all-time |
| 2 | [`battle-intelligence-2-decks.md`](battle-intelligence-2-decks.md) | archetype **by rules** (`decks.py` `_classify`, shipped), `deck_profile` dimensional scores, `matchup_expectation` matrix, calibration/blend, deck+matchup views | Haiku, **matrix only** | ~$1/era | deck archetypes (rules, $0), matchup expectations, upset detector |
| 3 | [`battle-intelligence-3-prose.md`](battle-intelligence-3-prose.md) | per-battle `loss_nature`/`notable`/`commentary`, prose prompt, **allowlist + date gate**, verdict/eval | Haiku, **gated** | ~$1/mo | per-battle commentary for a few members |
| 4 | [`battle-intelligence-4-strength.md`](battle-intelligence-4-strength.md) | compare any two players on 4 axes from the `player` payload (king level, collection level, games won, years played); members ingested, **opponents fetched via CR API** (capped/cached) | none | $0 LLM + modest API | head-to-head player strength, member↔opponent |

Each feature is independently shippable and de-risks the next. **Feature 1 is
pure SQL** — it proves the scary plumbing (the `_apply_v27` migration on an
884 MB live DB, the cursor worker, the tool wiring) at zero model cost and zero
model-quality risk before a dollar of LLM is spent.

## Context

`battle_events` carries full card detail for **both sides** of every battle
(13.5k battles, ~240k card plays, 126 cards), plus per-tower HP and elixir
leaked for both players. Nothing reads the opponent half; the deck capability
still tells the brain "opponent deck lists are unavailable." That prompt line is
stale, not the data: **`opponent_deck_json` is populated back to 2026-04-18**
(verified live) — #216 (2026-07-28) flipped the reader/capability flag, it did
not start the capture. So opponent-deck history is deep, not eight days thin.

Exploration on live data established that the signal is real:

- **Cards**: 1,103 member×card pairs have 30+ encounters. Ditaka loses 75%
  against Bats vs a 50.2% baseline (n=44). Ronin launched 2026-07-06; 49
  members adopted it in three weeks, winning 50.4% with it and losing 52%
  against it.
- **Decks**: exact decks are hopelessly sparse (10,006 distinct in 14,269
  battles; 7-of-8 clustering collapses only 13%). Reachable only through
  archetype — hence Feature 2's LLM labels.
- **Archetype matchups**: 32 of 81 cells clear n=100. Bait into control runs
  −9.9 points off baseline (n=265); hybrid into beatdown +7.3 (n=1,202).
- **The classifier was coarse, not starved (corrected 2026-07-30)**: an earlier
  read claimed `capabilities/decks.py` "never joins `card_catalog`, so
  `_average_elixir` is None for 100% of battle decks, 47% land in `hybrid`."
  That reproduces only on **raw `deck_json`** — the production path *does* join
  the catalog (`storage/cards.py` `_deck_catalog`/`_enrich_deck_cards`), so
  `_average_elixir` is None just ~4.3% (Mirror decks) and the elixir branches
  fire. The real gap was **coarse rules** (43% `hybrid`), because the branch
  logic had no home for Mega Knight / Champions / P.E.K.K.A / Ronin. **Fixed by
  a rules rebuild** (win-condition-driven `_classify`, ~40 conventional named
  archetypes + a coarse `family`, $0, form-ready): the honest residual is now
  **~2% of battles** (decks with genuinely no win condition), measured live.
  **So archetype LABELS are a solved rules problem — Feature 2's LLM shrinks to
  the matchup-advantage matrix, it no longer labels decks.**
- **Haiku enrichment works when framed as knowledge, not narration** (30-battle
  experiment): structural reads and archetype labels were stable and insightful.
  Judgment verdicts (matchup_favours) flip-flopped on identical input — the
  accidental A/B from the dedup bug proved it. Hence: dimensional scores, not
  verdicts (Feature 2); minimized judgment in prose (Feature 3).
- **Most metrics need no model at all**: hp_margin, discipline_delta, level_gap
  computed by SQL cleanly separated a 0–3 stomp (−9,470) from a 54-HP squeaker,
  and reproduced Haiku's "panic spending" read as a number (−5.9). **This is all
  of Feature 1.**

## The one architectural principle

**Each table's key has a different cardinality and stability, and that decides
who fills it.** LLM knowledge attaches only to immutable keys; everything keyed
by the append-only battle stream is arithmetic.

| layer | keyed by | count | filled by | feature |
|---|---|---|---|---|
| `battle_card_plays` | battle × side × card | ~240k | SQL, derived | 1 |
| `battle_enrichment` (computed cols) | battle | all 1v1 | SQL, derived | 1 |
| `deck_profile.archetype` | deck hash | ~2k meaningful | **rules** (`_classify`) | 2 |
| `deck_profile` dimensional scores | deck hash | ~2k, immutable | LLM once per deck (optional) | 2 |
| `matchup_expectation` | archetype pair × era | ~400 | LLM once per era | 2 |
| `battle_enrichment` (prose cols) | battle | gated | LLM per battle | 3 |
| player-strength triad | player | ~150 | ingestion, derived | 4 |

## Coverage & the cost decision (Jamie, 2026-07-30)

Two tiers, and the split is deliberate:

- **Data + intelligence — universal.** All members, all battles, all time. The
  computed layers (Feature 1) and the deck/matchup intelligence (Feature 2) are
  built for everyone — decks and matchups aren't per-player. Cheap because
  **archetype labels are now rules ($0)** and the only LLM spend is the ≤400-cell
  matchup matrix (~$1/era) plus optional dimensional scores on the *meaningful*
  decks (member decks ∪ decks seen ≥3× ≈ 2k). We want the whole structure in
  place so the feature can evolve without re-backfilling data.
- **Per-battle prose — gated.** The one genuinely per-player, iterate-heavy,
  regenerate-often LLM output (`loss_nature`/`notable`/`commentary`, Feature 3)
  is gated to an **allowlist** (`battle_enrichment_enabled` on `player_metadata`;
  seed King Thing `#20JJJ2CCRU`, raquaza `#UL2V9QRG0`) **and a date boundary**
  (`battle_time >= 2026-07-20`). Measured cost at that scope: **~$0.34 backfill
  + ~$1/mo** (94 prose-eligible seed battles since 07-20), versus ~$42/mo if
  prose ran clan-wide. We fully expect to evolve this; spending clan-wide on the
  part we'll keep rewriting is the waste to avoid. Flip the gate open once the
  prompt stabilizes.

The rationale: the expensive, evolving thing is per-battle prose regenerated
across thousands of battles — not the ~$3 of immutable deck profiles. So gate
the prose, build everything else for everyone.

## Cross-cutting lessons (every feature inherits these)

Earned scars — each feature's plan restates the ones it touches, but they live
here so no feature re-learns them:

- **Migration hits the LIVE db.** Any elixir process connecting without an
  explicit path applies a new `_apply_vN` to production, then the running old
  build fails every tick with "schema newer than this build." A migration is a
  deploy: validate on a copy with `ELIXIR_DB_PATH` set, back up, then
  `admin.sh restart`. Ask before restarting.
- **Migration bookkeeping**: update `REQUIRED_SCHEMA` and the simulator's
  `EXPECTED_TABLE_COUNT` (`db/schema.py:17`, currently **59**); bump the
  exception-hygiene baseline for `db/schema.py` (+1 per `_apply_vN`).
- **Migration numbers are assigned at ship time, not hardcoded to feature order.**
  The `_apply_vN` numbers (`v27`…) and `EXPECTED_TABLE_COUNT` deltas in these
  plans assume Features ship 1→2→3→4. They don't have to — **Feature 4 is
  independent**. Whatever ships next takes the next `_apply_vN` off the live
  `user_version` and adds its own table count to the *current* baseline; treat
  the per-feature numbers as illustrative, and reconcile against the live schema
  at build, not against the plan's assumed order.
- **Dedup-key discipline**: every battle-keyed table keys on
  `battle_events.dedup_key` **verbatim** — never re-derive keys from formatted
  values (the v25 lesson; `tests/test_battle_ingest_idempotent.py`).
- **Tool wiring is a checklist, not a definition** (the `raise_clan_chat_relay`
  lesson — a write tool that never reached `AWARENESS_WRITE_TOOL_NAMES` was
  offered 0×): `agent/tool_defs.py` → `_SHARED_TOOL_NAMES` (currently **14**) →
  `agent/tool_exec.py` dispatch → `tests/test_entrypoints_smoke.py` AST parity →
  coverage matrix → prompt documentation. Miss a step and the tool silently
  doesn't exist.
- **Job wiring is a checklist too** (the same lesson, for scheduled workers): a
  `runtime/jobs/` function only runs if registered as an `ActivityDefinition` in
  `runtime/activities.py` (`job_id`, `job_function`, `schedule_kind`/`config`,
  `delivery_targets`) and re-exported through `runtime/jobs/__init__.py`. Miss it
  and the worker is defined but never fires — verify it in the scheduler startup
  summary.
- **Job telemetry with real work-set counts**: `mark_job_start/success/failure`
  with counts in the message — never a bare success indistinguishable from "did
  nothing" (the operational-audit class: four jobs today report success while
  structurally unable to work).
- **Statistical floors live in the capability, not the prompt**: member×card
  claims need n≥30; below-floor queries return an explicit `insufficient_sample`
  reason, never a weak number. A tool cannot be talked out of a floor; a prompt
  can.
- **Validate data, not just errors**: error-free ≠ correct. Sample real values
  and sanity-check against game reality before trusting a rebuild.
- **Card form (base / Evo / Hero) is part of card identity — never merge it.**
  `evolution_level` is an overloaded form discriminator, not a level: `1` = Evo,
  `2` = Hero (CR devs reused the field when Heroes shipped; confirmed 14 Hero
  cards ↔ 14 catalog `hero_icon_url` cards). A card can hold both forms (Knight,
  Wizard). Card identity and `deck_hash` key on `(card_id, evolution_level)`;
  `star_level` is cosmetic and excluded. This is why Feature 1 distinguishes
  "Evo Wizard" from "Wizard", and Feature 2/3 can speak to form choices. Detail:
  [`battle-intelligence-1-data.md`](battle-intelligence-1-data.md) §2.

## The archetype vocabulary lives in `capabilities/decks.py` (shipped, rules)

The archetype label is **rules, not an LLM enum**. `_classify`/`_archetype` in
`capabilities/decks.py` (rebuilt 2026-07-30) name a deck by a priority-ordered
win-condition lookup — a coarse `family` (beatdown / control / cycle / bait /
bridge spam / siege, or `unclassified`) plus a conventional `<win condition>
<family>` `label` (~40 names: `Hog Cycle`, `Lavaloon`, `Log Bait`, `Mega Knight
Bridge Spam`, `X-Bow Siege`, …). $0, deterministic, form-ready (keys on
`(card_id, evolution_level)` once Feature 1 lands). Naming conventions were tuned
live with King Thing + raquaza (see memory `elixir-deck-archetype-classifier`):
Royal Hogs is bridge-pressure, Log Bait requires a real bait package (else Miner
Control), Ronin/Boss Bandit are win conditions.

`deck_profile.archetype` (Feature 2) is filled by this same classifier, **not**
the model. There is one archetype vocabulary, in one place — no LLM enum to drift
from it, which also kills the "fourth disagreeing vocabulary" trap.

**The old `other`-bucket gate is moot** — the rules residual is already measured
at ~2% of battles (decks with genuinely no win condition, honestly labelled
`unclassified`), far below the 15% threshold that would have needed a richer
enum.

## Freshness principle (Feature 2 implements it)

- **Supersede, never overwrite**: era rows via `valid_from`/`superseded_at`.
  `battle_enrichment.expected_advantage` is a snapshot, so history never needs
  rewriting when eras change.
- **Calibration** (weekly, SQL): measured win rate vs predicted band, with n;
  drift + sufficient n ⇒ flag the cell. Detects stat-only balance patches the
  API can't show and the model may not know.
- **Blend**: `effective_advantage = w·measured + (1−w)·model`, `w = n/(n+30)`.
  Self-heals balance patches without a model call.
- **Re-score triggers**, in order of trust: calibration alarm → `card_catalog`
  diff (new card / evolution / elixir-cost change) → `season_started` → manual
  with patch notes pasted in (the only fix for the model's training cutoff).

## Explicitly out of scope for all of v1

- `coaching_note` (personal critique) and the requester-identity plumbing it
  needs (`channel_router → workflow → tool_exec → discord_links`) — v2. The
  interactive workflow passes only `author_name`; the asker's `discord_user_id`
  never reaches `tool_exec`, so there is no hook to scope output to "the member
  it is about." v1 generates commentary that is safe for any asker.
- Awareness-loop proactive posting from enrichment (`notable` exists; using it
  is v2, after the revision-trap snapshot question is settled).
- **Duels (`rounds_json`) — skipped entirely in v1** (no computed metrics, no
  prose): a best-of-3's top-level tower HP and concatenated sub-decks would
  produce confident nonsense, and that failure is as real in a number as in a
  sentence. **v1-only skip, not permanent** — duels are a large share of
  clan-war activity and the priority v2 addition; they need per-round
  decomposition (score each `rounds_json` sub-battle on its own deck pair).
  ~1.3% of battles today, but the highest stakes.
- **2v2 — permanently excluded** (`teammate_tag IS NOT NULL`, ~7.5% of
  battles): two decks per side is a problem the single-subject model does not
  represent, and not worth modelling.
- Retiring the `_archetype()` cascade (Feature 2 supersedes it once labels prove
  out); card rollup table; war-prep opponent-clan precompute — after v1 proves
  out.
