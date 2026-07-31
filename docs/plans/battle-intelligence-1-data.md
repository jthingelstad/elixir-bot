# Battle Intelligence — Feature 1: Battle data + computed intelligence

Status: **ready to build** (2026-07-30). Part of [Battle Intelligence
v1](battle-intelligence.md). Depends on nothing. Unblocks Features 2, 3, 4.

## Goal

Build the **universal, computed foundation**: every card played on both sides of
every battle, plus per-battle computed metrics, plus a tool the brain can query
— **clan-wide, all-time, zero LLM**. This is the load-bearing data layer the
whole feature stands on, and it proves the risky plumbing (the `_apply_v27`
migration on the live 884 MB DB, the cursor worker, the tool wiring) with **no
model cost and no model-quality risk**.

If the migration or worker has a bug, Feature 1 is where you find out — for free.

### Non-goals (explicitly deferred)

- **No LLM anything.** No archetype labels, no `deck_profile`, no
  `matchup_expectation`, no prose. Those are Features 2–3.
- **No account-strength axes** (collection level / king level / games / years).
  Player strength is battle-bound only (Feature 4): the sole strength signal is
  `level_gap` below, computed from the battle. No profile fetch, ever.
- `expected_advantage`, `performance`, and all prose columns exist in the schema
  but stay **NULL** in Feature 1 (they need Feature 2's profiles/cells).
- Duels and 2v2 get **no rows at all** (not even computed) — see §Duels/2v2.

## What Feature 1 fills

| target | key | filled by | this feature |
|---|---|---|---|
| `battle_card_plays` | battle × side × card | SQL from `deck_json`/`opponent_deck_json` | **all** rows, both sides, clan-wide, all-time |
| `battle_enrichment` computed cols | battle | SQL from `battle_events` | 1v1 battles only; `hp_margin`, `closeness`, `discipline_delta`, `level_gap`, `our_deck_hash`, `their_deck_hash` |
| `battle_enrichment` deferred cols | battle | later features | `expected_advantage`, `performance`, prose, `verdict` → **NULL** |
| `get_battle_intelligence` | tool | capability | views `card`, `nemesis`, `battle`, `member_summary` (computed only) |

## 1. Schema — `_apply_v27`

Two tables. Full `battle_enrichment` column shape ships now (so Features 2–3
fill columns, never `ALTER`), but Feature 1 populates only the computed ones.

```sql
CREATE TABLE battle_card_plays (
    battle_dedup_key TEXT NOT NULL REFERENCES battle_events(dedup_key),
    side             TEXT NOT NULL CHECK (side IN ('member','opponent')),
    card_id          INTEGER NOT NULL,
    level            INTEGER,
    evolution_level  INTEGER,
    star_level       INTEGER,
    -- denormalized for index locality (rebuildable):
    player_tag       TEXT NOT NULL,   -- the SUBJECT member, stamped on BOTH sides
    battle_time      TEXT NOT NULL,
    outcome          TEXT,            -- 'W'|'L'|'D', copied from battle_events
    mode_group       TEXT,
    is_competitive   INTEGER,         -- copied from battle_events.is_competitive
    PRIMARY KEY (battle_dedup_key, side, card_id)
);
CREATE INDEX idx_bcp_card ON battle_card_plays(card_id, side, battle_time);
CREATE INDEX idx_bcp_member_card ON battle_card_plays(player_tag, card_id, side);

CREATE TABLE battle_enrichment (
    battle_dedup_key TEXT PRIMARY KEY REFERENCES battle_events(dedup_key),
    player_tag       TEXT NOT NULL,
    -- computed at insert, no model (Feature 1 fills these):
    hp_margin        INTEGER,   -- (our standing towers − theirs)*3000 + HP delta
    closeness        INTEGER,   -- 0 stomp .. 3 squeaker, banded from |hp_margin|
    discipline_delta REAL,      -- opponent_elixir_leaked − elixir_leaked
    level_gap        REAL,      -- avg our card levels − theirs (see §2 caveat)
    our_deck_hash    TEXT,      -- pure sha256 helper; NO archetype yet
    their_deck_hash  TEXT,
    -- Feature 2 (NULL in Feature 1):
    expected_advantage INTEGER, -- SNAPSHOT from matchup_expectation at enrich time
    performance      INTEGER,   -- −1|0|+1: outcome vs expectation (upset detector)
    -- Feature 3 (NULL in Feature 1):
    loss_nature      TEXT CHECK (loss_nature IS NULL OR loss_nature IN
                     ('structural','piloting','level','close','unclear')),
    notable          INTEGER NOT NULL DEFAULT 0,
    confidence       TEXT,
    commentary       TEXT,
    coaching_note    TEXT,      -- v2
    verdict          TEXT,      -- Feature 3 eval benchmark
    model            TEXT,
    prompt_version   INTEGER,
    input_hash       TEXT,
    enriched_at      TEXT
);
CREATE INDEX idx_be_player_time ON battle_enrichment(player_tag, battle_time)
    -- battle_time isn't a column here; index (player_tag) + join to battle_events,
    -- OR denormalize battle_time. DECIDE at build (see §Open).
```

> **Note the `loss_nature` CHECK** allows NULL explicitly — Feature 1 writes NULL
> and a bare `CHECK (loss_nature IN (...))` rejects NULL on some SQLite builds
> only when NOT NULL; keep the `IS NULL OR` guard so computed-only inserts pass.

Migration mechanics (from the shared lessons):

- Bump `EXPECTED_TABLE_COUNT` **59 → 61** (`db/schema.py:56`); update
  `REQUIRED_SCHEMA`; bump the `db/schema.py` exception-hygiene baseline +1.
- Keys are `battle_events.dedup_key` **verbatim** — never re-derived
  (`tests/test_battle_ingest_idempotent.py` is the pattern to mirror).
- **Validate on a copy** with `ELIXIR_DB_PATH` before any restart; production
  only via `admin.sh restart` after backup. Migration = deploy; ask first.

## 2. Computed metric definitions (exact)

All pure SQL/Python over `battle_events`. Precise so the backfill and the
incremental worker produce identical values.

### Card form is part of card identity (base ≠ Evo ≠ Hero)

`evolution_level` is an **overloaded form discriminator**, not a level — CR devs
reused the field with a new value when Heroes shipped. Confirmed against live
data (2026-07-30):

| `evolution_level` | form | cards | cross-check |
|---|---|---|---|
| NULL / absent | **base** | 122 | — |
| `1` | **Evolution** | 41 | == the 41 catalog cards with `evolution_icon_url` |
| `2` | **Hero** | 14 | == the 14 catalog cards with `hero_icon_url` (exact set, 0 mismatch) |
| `3` | higher tier | 2 (Wizard, Knight; 20 plays) | confirm via cr-api-doc-audit |

A card can hold **both** an Evo and a Hero form — Knight and Wizard appear at
`1` *and* `2`. They play differently and must **never be merged**:

- **Card identity = `(card_id, evolution_level)`.** The `card`/`nemesis` views
  group on it and present "Knight" / "Evo Knight" / "Hero Knight" separately.
  "Evo Wizard win-rate" vs "Wizard win-rate" are different questions, and the
  evolved/hero forms are the meta drivers — merging them would blur exactly the
  signal that matters.
- **`star_level` is NOT form** — it's cosmetic star points (NULL/1/2/3); never in
  identity or the hash.
- Pure helper `card_form(evolution_level) -> 'base'|'evo'|'hero'` lives in the
  new leaf module (`engine/deck_hash.py`), shared by the hash, the views, and
  Features 2–3. Treat `>=2` as `hero` until the level-3 semantics are confirmed.
- **Docs debt**: this `evolution_level` overload (2 = Hero) is an undocumented
  CR API semantic — flag it for `docs/cr-api-docs/` via the cr-api-doc-audit
  skill when this ships.

### Metrics

- **`our_deck_hash` / `their_deck_hash`**: `sha256` (first 16 hex) over the
  sorted **`(card_id, evolution_level)` pairs** of the 8 cards — form-aware, so a
  deck running Evo Knight, one running Hero Knight, and one running base Knight
  are **three distinct decks**. A deck's evo/hero choices are part of its
  strategy and identity. Pure leaf helper (`engine/deck_hash.py`, new) that
  Feature 2's `deck_archetypes` builds on; seeds Feature 2's profiling worklist.
  (`star_level` excluded — cosmetic.)
- **`discipline_delta`** = `opponent_elixir_leaked − elixir_leaked`. Positive =
  we wasted less elixir than them. Both columns exist; NULL if either is NULL.
- **`hp_margin`** = `(our_standing − opp_standing) * 3000 + (our_hp − opp_hp)`
  where `standing` = `(king_tower_hp > 0) + len(princess_towers_hp_json)` and
  `hp` = king + sum of princess HP. **The 3000 is a coarse tower-worth constant**
  (real princess HP scales with tower level ~2400–3600); acceptable because
  `closeness` only reads its band, not its precision.
  - **Verified on live data (2026-07-30, 12,433 1v1 battles):** the array lists
    **only surviving princess towers**, and the standing formula reproduces the
    independent crowns signal — `standing == 3 − crowns_against` in **99.99%** of
    battles. Build it as written.
  - **A destroyed king reports `king_tower_hp = 0`, not NULL** (confirmed: 1,612
    of 1,616 three-crown losses have `0`), so `(king_tower_hp > 0)` evaluates
    cleanly — **no `COALESCE` needed on the king term**, no NULL-propagation.
    Whole-battle tower data is truly absent in only **0.04%** (5/12,433); there,
    and only there, `hp_margin` is NULL — the intended behaviour.
- **`closeness`** = band of `|hp_margin|`: `0` (≥7000, a stomp), `1`
  (3000–6999), `2` (500–2999), `3` (<500, a squeaker). Calibrate the cuts
  against real values during verification (§6), don't trust these numbers blind.
- **`level_gap`** = `avg(member card levels) − avg(opponent card levels)` from
  the two `deck_json`s. **This is the whole player-strength story** (Feature 4):
  strength is bound only to the battle, and this is the battle's level signal —
  no profile fetch, no account axes. It feeds Feature 3's `loss_nature='level'`.
  - **Normalized-mode guard (required):** `deck_json` stores *account* card
    levels even for ranked, but Path of Legends plays every card at level 11, so
    a `level_gap` there is fictional. **Set `level_gap = NULL` with reason
    `levels_normalized` for `is_ranked` (and any level-capped special event);**
    compute it only where levels are real (war, ladder). Verified live: ranked
    `deck_json` carries varied account levels (8–16), confirming they are *not*
    the in-battle values.
  - **Caveat:** raw levels average across rarities, which is rarity-naive (a
    lvl-11 Common ≠ a lvl-11 Legendary). Keep `level_gap` deck-scoped; do not
    overload it into an account-strength claim.

## 3. Worker — `runtime/jobs/_battle_intel.py` (Stage A)

Cursor-driven (reuse the `stream_cursors` table + `engine/db.py` cursor
helpers), **outside the engine tick** so ingestion stays deterministic and
replayable and enrichment catches up behind it. The reference pattern is the
existing cursor jobs in `runtime/jobs/_*.py` (e.g. `_api_sentinel_tick` in
`_maintenance.py`). **Not** `emit_game_from_sentinel` — that one *exists*
(`engine/emitters/game.py:92`, called from `materialize.py`) but runs *inside*
the engine tick, which is exactly the coupling we avoid; mirror the off-tick
cursor jobs instead.

**Job wiring is a checklist too** (the tool-wiring lesson, applied to jobs — miss
a step and the worker is defined but never runs): add an `ActivityDefinition` in
`runtime/activities.py` (`job_id`, `job_function`, `schedule_kind="interval"`,
`schedule_config`, `delivery_targets`) → re-export the function through
`runtime/jobs/__init__.py` → confirm it appears in the scheduler startup summary.
A job function alone, with no `ActivityDefinition`, silently never fires.

**Stage A — computed, every 15 min, no LLM:**

1. Extend `battle_card_plays` for all new battles since the cursor (clan-wide,
   both sides). Skip battles with `teammate_tag IS NOT NULL` (2v2) or
   `rounds_json IS NOT NULL` (duel).
2. Insert `battle_enrichment` computed rows for all new 1v1 battles (same skip),
   LLM/Feature-2 columns NULL.

**Backfill** (one-time, ~13,570 battles → ~12,379 1v1 `battle_enrichment` rows,
~240k card plays). Idempotent via `INSERT OR IGNORE` on the dedup-keyed PKs, so a
re-run or an overlap with the live cursor is safe. **Reaching prod (the open op
question):** validation runs on a copy, but prod still needs the ~240k-row load.
Don't let the first live Stage-A run do it in one transaction — that is a large
write contending with the running bot's WAL (`database is locked` risk). Either
run it as a **gated one-time backfill job in bounded chunks** (commit every N
battles, cursor-advanced so it resumes), or run it off-hours; the incremental
15-min worker then only ever sees a small delta. Log the running total.

**Telemetry**: `mark_job_start/success/failure` with real work-set counts
(`"card_plays +N, enrichment +M, skipped D duels / T 2v2"`) — never a bare
success. A run that inserts 0 because it's caught up says so; a run that inserts
0 because the query is broken looks different.

## 4. Tool — `get_battle_intelligence` (computed views)

New tool in `capabilities/battle_intel.py`. Feature 1 ships the **computed
views**; Features 2–3 extend the same tool with `deck`/`matchup` and prose.

Views this feature delivers:

- **`card`**: per-card-**form** win/loss when a member *plays* it and when they
  *face* it, adoption over time, competitive vs all. Grouped on
  `(card_id, evolution_level)` and labelled base/Evo/Hero (§2) — never merged.
  Floor **n≥30**; below-floor → `insufficient_sample`.
- **`nemesis`**: for a member (or clan), the opponent card-forms with the worst
  member win-rate against them, n-gated. (Facing Evo Wizard vs base Wizard is a
  different nemesis.)
- **`battle`**: one battle's computed read — `hp_margin`, `closeness`,
  `discipline_delta`, `level_gap`, both deck hashes. No prose yet.
- **`member_summary`**: a member's computed rollups — record, closeness
  distribution, discipline, most/least successful cards. No prose, no archetype.

**Wiring checklist** (miss a step → the tool silently doesn't exist):
`agent/tool_defs.py` definition → `_SHARED_TOOL_NAMES` **14 → 15** →
`agent/tool_exec.py` dispatch → `tests/test_entrypoints_smoke.py` AST parity →
coverage matrix → prompt documentation.

**Floors live here, not in the prompt.** n≥30 for card claims; `battle`/
`member_summary` report their sample sizes. A tool cannot be talked out of a
floor.

**Known caveat — intra-clan double count**: a battle between two clan members
appears twice in `battle_events` (once per member as subject, sides swapped), so
clan-wide card win-rate aggregation double-counts it. Rare (war opponents are
other clans; matters mainly for friendlies/2v2, and 2v2 is excluded). Document
it in the `card` view; a `DISTINCT`-on-unordered-pair dedup is a later refinement
if it ever matters.

## 5. Prompt-truth fix (scoped to what Feature 1 delivers)

The brain is currently told opponent decks don't exist, which blocks the whole
opponent half. Feature 1 unblocks **card-level** opponent claims (archetype/deck
guidance waits for Feature 2):

- `agent/prompt_builders.py:530` — replace *"Elixir does NOT store opponent deck
  lists, so never cite specific opponent cards…"* with guidance to call
  `get_battle_intelligence` for opponent **card** matchup data (nemesis, per-card
  win rates). Leave archetype/deck-matchup claims out until Feature 2.
- `capabilities/decks.py` `evidence_limits.opponent_decks_captured` →
  `True` (we do store them now). The `_archetype()` cascade was **rebuilt to
  win-condition rules 2026-07-30** (conventional `label` + coarse `family`, $0);
  Feature 2 consumes it for `deck_profile.archetype` rather than replacing it.

## 6. Verification (validate data, not just errors)

Error-free ≠ correct. Before restart, on a copy:

1. **Reconcile counts**: a 1v1 battle contributes **16 card plays** (8 cards ×
   2 sides — verified: 1v1 `deck_json` is always 8; the 16/24-card rows are
   duels, whose `deck_json` concatenates rounds, and are excluded). Assert
   `battle_card_plays COUNT(*) = 16 × (1v1 battles processed)` and that duels +
   2v2 are absent from **both** new tables.
2. **Sanity the metrics**: wins should skew positive `hp_margin`; a known 3-0
   stomp should band `closeness=0`; spot-check a battle by hand against the game.
   (The exploration's −9,470 stomp / 54-HP squeaker are the reference points.)
3. **Re-derive a card win-rate** (e.g. Ditaka vs Bats ≈ 25% win, the −25-point
   nemesis from exploration) and confirm the `card` view reproduces it.
4. **Idempotency**: run the backfill twice; row counts must not change.
5. Suite + war-week simulator green (the sim asserts `EXPECTED_TABLE_COUNT`).

## 7. Build order (each = one commit, suite green)

1. `engine/deck_hash.py` pure helper + unit test.
2. `_apply_v27` + `REQUIRED_SCHEMA`/`EXPECTED_TABLE_COUNT`/hygiene baseline.
   Validate on a copy. No behaviour change yet.
3. Stage-A worker + backfill, with telemetry. Backfill on the copy; verify (§6).
4. `get_battle_intelligence` capability + full wiring checklist + prompt-truth
   fix. Mutation-check the n≥30 floors.
5. **Restart** (migration reaches prod) — ask first; back up; `admin.sh
   restart`; confirm the running build accepts v27 and Stage A logs real counts.

## 8. Risks (Feature 1 scope)

| risk | guard |
|---|---|
| migration breaks the live tick ("schema newer than build") | validate on a copy w/ `ELIXIR_DB_PATH`; back up; restart is the deploy |
| key drift duplicating rows | keys = `battle_events.dedup_key` verbatim; idempotency test; `INSERT OR IGNORE` |
| silent worker death | work-set counts in job telemetry |
| tool silently absent | full wiring checklist incl. AST-parity smoke test |
| weak-sample card claims | n≥30 floor in the capability |
| intra-clan double count | documented in `card` view; dedup deferred until it matters |
| `hp_margin` constant too coarse | only its band is read; cuts calibrated against real values in §6 |

## 9. Open questions (decide at build)

- **`battle_enrichment.battle_time`**: index `member_summary` queries need a
  time axis. Either denormalize `battle_time` onto `battle_enrichment` (matches
  `battle_card_plays`, simplest) or always join `battle_events`. Lean
  denormalize — it's rebuildable and the table is derived anyway.
- **`closeness` band cuts**: the §2 numbers are a starting guess; set them from
  the real `|hp_margin|` distribution during verification, not a priori.
- ~~**Deck-hash card-id source**~~ **(resolved — form-aware)**: the card `id`
  is evolution-stable (Royal Giant is `26000024` regardless), but form is **not**
  merged away — `deck_hash` and card identity key on `(card_id,
  evolution_level)` so base / Evo / Hero are distinct (§2). Confirmed 14 Hero
  cards ↔ 14 `hero_icon_url` cards. Open sub-point: confirm the level-3 tier
  (Wizard/Knight, 20 plays) via cr-api-doc-audit and decide if it's its own form
  label or folds into `hero`.
