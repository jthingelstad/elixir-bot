# Battle Intelligence v1

Status: **designed, not built** (2026-07-30). Owner: Jamie. Design partner: Claude.

## Context

`battle_events` now carries full card detail for **both sides** of every battle
(14.5k battles, ~240k card plays, 122 cards), plus per-tower HP and elixir
leaked for both players. Nothing reads the opponent half; the deck capability
still tells the brain "opponent deck lists are unavailable," which was true on
2026-07-15 and false since #216 landed 2026-07-28.

Exploration on live data established that the signal is real:

- **Cards**: 1,103 member×card pairs have 30+ encounters. Ditaka loses 75%
  against Bats vs a 50.2% baseline (n=44). Ronin launched 2026-07-06; 49
  members adopted it in three weeks, winning 50.4% with it and losing 52%
  against it.
- **Decks**: exact decks are hopelessly sparse (10,006 distinct in 14,269
  battles; a 7-of-8-overlap clustering collapses only 13%). The head is ~37
  real decks covering 19% of battles; everything else is only reachable
  through archetype.
- **Archetype matchups**: 32 of 81 cells clear n=100. Bait into control runs
  −9.9 points off baseline (n=265); hybrid into beatdown +7.3 (n=1,202).
- **The existing classifier is broken**: `capabilities/decks.py` declares
  `card_catalog` as a source but never joins it, so `_average_elixir` is None
  for 100% of battle decks, four of ten branches can never fire, and 47% of
  decks land in `hybrid`.
- **Haiku enrichment works when framed as knowledge, not narration** (30-battle
  experiment): structural reads and archetype labels were stable and genuinely
  insightful ("a lvl5 Witch does not belong in war"; "the loss is structural,
  not mechanical"). Judgment verdicts (matchup_favours) flip-flopped on
  identical input — the accidental A/B from the dedup bug proved it.
- **Most metrics need no model at all**: hp_margin, discipline_delta,
  level_gap computed by SQL cleanly separated a 0–3 stomp (−9,470) from a
  54-HP squeaker, and reproduced Haiku's "panic spending" read as a number
  (−5.9).

Decisions taken (Jamie):

- **Enrollment**: per-member opt-in. Seed: King Thing `#20JJJ2CCRU`,
  raquaza `#UL2V9QRG0`. Enrollment gates only the per-battle LLM prose; the
  intelligence layers (cards, decks, matchups, computed metrics) are clan-wide.
- **Window**: enrich battles with `battle_time >= 2026-07-20` only. We will
  rebuild repeatedly; `prompt_version` makes rebuilds cheap and auditable.
- **Consent split**: `commentary` is public-safe; `coaching_note` is visible
  only to the member it is about, on request, in #ask-elixir. Never in a
  public post. Enforced in the capability, not the prompt.
- **Freshness**: the model is the prior, measured data is the corrector
  (see §6).

## 1. Architecture — one principle

**Each table's key has a different cardinality and stability, and that decides
who fills it.** LLM knowledge attaches only to immutable keys; everything keyed
by the append-only battle stream is arithmetic.

| layer | keyed by | count | filled by | cost |
|---|---|---|---|---|
| `battle_card_plays` | battle × side × card | ~240k | SQL, derived | $0 |
| `deck_profile` | deck hash | ~10k, immutable | LLM once per deck | ~$0.001/deck, amortizes to 0 |
| `matchup_expectation` | archetype pair × era | ~400 | LLM once per era | ~$1/era |
| `battle_enrichment` | battle | enrolled only | computed + 4 small LLM fields | pennies/mo |

Card-level intel (nemesis, adoption, win rates) is **computed views over
`battle_card_plays`** in the capability — no card rollup table in v1, no LLM.
If those queries get slow at 2-year retention, a rollup is a v2 addition.

## 2. Schema (one migration, `_apply_v27`)

All new tables are **derived and rebuildable** from `battle_events` +
`card_catalog`; only the enrollment flag is source-of-truth.

```sql
ALTER TABLE player_metadata ADD COLUMN battle_enrichment_enabled INTEGER NOT NULL DEFAULT 0;
-- seed: UPDATE ... WHERE player_tag IN ('#20JJJ2CCRU', '#UL2V9QRG0')
-- (INSERT OR IGNORE the rows first; player_metadata is sparse)

CREATE TABLE battle_card_plays (
    battle_dedup_key TEXT NOT NULL REFERENCES battle_events(dedup_key),
    side             TEXT NOT NULL CHECK (side IN ('member','opponent')),
    card_id          INTEGER NOT NULL,
    level            INTEGER,
    evolution_level  INTEGER,
    star_level       INTEGER,
    -- denormalized for index locality (rebuildable):
    player_tag       TEXT NOT NULL,   -- the SUBJECT member, both sides
    battle_time      TEXT NOT NULL,
    outcome          TEXT,
    mode_group       TEXT,
    is_competitive   INTEGER,
    PRIMARY KEY (battle_dedup_key, side, card_id)
);
CREATE INDEX idx_bcp_card ON battle_card_plays(card_id, side, battle_time);
CREATE INDEX idx_bcp_member_card ON battle_card_plays(player_tag, card_id, side);

CREATE TABLE deck_profile (
    deck_hash        TEXT NOT NULL,       -- sha256 of sorted card_ids, first 16 hex
    valid_from       TEXT NOT NULL,
    superseded_at    TEXT,
    cards_json       TEXT NOT NULL,
    archetype        TEXT NOT NULL,       -- CHECK against the shared enum
    win_condition_type TEXT,              -- air|ground|siege|spell|bridge|none
    defense_air      INTEGER CHECK (defense_air BETWEEN 0 AND 5),
    defense_tank     INTEGER CHECK (defense_tank BETWEEN 0 AND 5),
    defense_swarm    INTEGER CHECK (defense_swarm BETWEEN 0 AND 5),
    spell_bait_vuln  INTEGER CHECK (spell_bait_vuln BETWEEN 0 AND 5),
    coherence        INTEGER CHECK (coherence BETWEEN 0 AND 5),
    avg_elixir       REAL,                -- COMPUTED from card_catalog, not the model
    model            TEXT NOT NULL,
    prompt_version   INTEGER NOT NULL,
    scored_at        TEXT NOT NULL,
    PRIMARY KEY (deck_hash, valid_from)
);

CREATE TABLE matchup_expectation (
    our_archetype    TEXT NOT NULL,
    their_archetype  TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    superseded_at    TEXT,
    advantage        INTEGER NOT NULL CHECK (advantage BETWEEN -2 AND 2),
    basis            TEXT,                -- one sentence, model
    model            TEXT NOT NULL,
    prompt_version   INTEGER NOT NULL,
    PRIMARY KEY (our_archetype, their_archetype, valid_from)
);

CREATE TABLE battle_enrichment (
    battle_dedup_key TEXT PRIMARY KEY REFERENCES battle_events(dedup_key),
    player_tag       TEXT NOT NULL,
    -- computed at insert, no model:
    hp_margin        INTEGER,   -- (our towers standing − theirs)*3000 + HP delta
    closeness        INTEGER,   -- 0 stomp .. 3 squeaker, banded from |hp_margin|
    discipline_delta REAL,      -- opponent_elixir_leaked − elixir_leaked
    level_gap        REAL,      -- avg our card levels − theirs
    our_deck_hash    TEXT,
    their_deck_hash  TEXT,
    expected_advantage INTEGER, -- SNAPSHOT from matchup_expectation at enrich time
    performance      INTEGER,   -- −1|0|+1: outcome vs expectation (upset detector)
    -- model (per battle, enrolled members only):
    loss_nature      TEXT CHECK (loss_nature IN
                     ('structural','piloting','level','close','unclear')),
    notable          INTEGER NOT NULL DEFAULT 0,
    confidence       TEXT,
    commentary       TEXT,      -- public-safe, 1–2 sentences
    coaching_note    TEXT,      -- PRIVATE to the subject member
    -- iteration machinery:
    verdict          TEXT,      -- Jamie-graded: accurate|wrong|useful (eval benchmark)
    model            TEXT,
    prompt_version   INTEGER,
    input_hash       TEXT,
    enriched_at      TEXT
);
```

Migration mechanics (lessons from today, all of them earned):

- Update `REQUIRED_SCHEMA` and the simulator's `EXPECTED_TABLE_COUNT`
  (`db/schema.py:56`) — the war-week sim asserts the designed table count.
- Bump the exception-hygiene baseline for `db/schema.py` (+1 per `_apply_vN`).
- Validate on a copy with `ELIXIR_DB_PATH` before any restart; the migration
  reaches production only via `admin.sh restart` after a backup.
- **Dedup-key discipline**: `battle_card_plays` and `battle_enrichment` key on
  `battle_dedup_key` — never re-derive keys from formatted values (the v25
  lesson; see `tests/test_battle_ingest_idempotent.py`).

## 3. The shared archetype enum — one home

`engine/deck_archetypes.py` (new, pure, leaf module):

```python
ARCHETYPES = ("log bait", "hog cycle", "hog control", "siege", "golem beatdown",
              "lavaloon", "giant beatdown", "graveyard control",
              "royal giant control", "bridge spam", "miner control",
              "balloon cycle", "x-bow siege", "mortar cycle",
              "pekka bridge spam", "three musketeers", "wall breakers cycle",
              "goblin drill control", "splashyard", "other")
```

Consumed by: the deck-profile prompt, the matchup prompt, both CHECK
constraints (a parity test asserts SQL == Python), and the capability. This is
how we avoid a fourth disagreeing vocabulary (the cascade in
`capabilities/decks.py` is the third; it stays untouched in v1 and its
retirement is a v2 question once LLM labels prove out).

## 4. Workers (`runtime/jobs/_battle_intel.py`)

Cursor-driven, **outside the engine tick** (pattern: `emit_game_from_sentinel`).
Ingestion stays deterministic and replayable; enrichment catches up behind it.

**Stage A — computed (every 15 min, no LLM):**
1. Extend `battle_card_plays` for ALL new battles (clan-wide).
2. Insert computed-metrics rows into `battle_enrichment` for enrolled members'
   battles ≥ 2026-07-20 (LLM columns NULL).

**Stage B — LLM (every 30 min, capped per run, Haiku):**
1. Profile unscored `deck_hash`es appearing in enrolled battles (both sides).
2. Score missing `matchup_expectation` cells (400 one-time, then only on
   re-score triggers).
3. Fill `expected_advantage`/`performance` once both profiles + cell exist.
4. Write prose (`loss_nature`, `notable`, `commentary`, `coaching_note`) for
   enrolled battles missing it at the current `prompt_version`. **The prompt
   receives the computed numbers** — the model explains data, it does not
   invent observations.

Job telemetry: `mark_job_start/success/failure` with real work-set counts in
the message — never a bare success that is indistinguishable from "did
nothing" (the operational-audit class: four jobs today report success while
structurally unable to work).

LLM calls go through the standard no-tool workflow path (like
`generate_clan_chat_copy`) with new workflow names `deck_profile`,
`matchup_score`, `battle_prose` so `llm_calls` telemetry and the cost skill see
them.

## 5. Prompts (3, all Haiku, all versioned)

Shared framing that made the experiment honest — state what the model CANNOT
see (no play-by-play), allow "insufficient", constrain labels to the enum:

1. **Deck profile**: 8 cards + catalog costs → archetype, win_condition_type,
   five 0–5 scores. Dimensional scores, not verdicts (scores were stable in
   the consistency test; verdicts were not).
2. **Matchup cell**: two archetypes → advantage −2..+2 + one-sentence basis.
3. **Battle prose**: the battle record + BOTH deck profiles + the computed
   metrics → loss_nature, notable, commentary, coaching_note. Explicit rule:
   `final-state claims must reference the provided numbers` (the two
   hallucination slips both came from free-form final-state reading).

## 6. Freshness: model = prior, data = corrector

- **Supersede, never overwrite**: era rows via `valid_from`/`superseded_at`.
  `battle_enrichment.expected_advantage` is a snapshot, so history never needs
  rewriting when eras change.
- **Calibration** (weekly, SQL only): per live cell, measured win rate vs
  predicted band, with n. Drift + sufficient n ⇒ flag the cell. This detects
  stat-only balance patches the API cannot show and the model may not know.
- **Blend**: `effective_advantage = w·measured + (1−w)·model`, `w = n/(n+30)`,
  measured mapped to −2..+2. Self-heals balance patches without a model call;
  also corrects model error from day one.
- **Re-score triggers**, in order of trust: calibration alarm (cell) →
  `card_catalog` diff: new card / evolution / elixir-cost change (decks
  containing it + its archetype's cells) → `season_started` event (cheap full
  re-score) → manual with patch notes pasted into the prompt (the only fix for
  the model's training cutoff).

## 7. Tool surface + privacy

**One new tool**: `get_battle_intelligence` in `capabilities/battle_intel.py`,
views: `member_summary` | `battle` | `card` | `deck` | `matchup` | `nemesis`.

Wiring checklist (every step, or the tool silently doesn't exist — the
`raise_clan_chat_relay` lesson):
`agent/tool_defs.py` definition → `_SHARED_TOOL_NAMES` (14→15) →
`agent/tool_exec.py` dispatch → `tests/test_entrypoints_smoke.py` AST parity →
coverage matrix → prompt documentation.

**Statistical floors live in the capability**: member×card claims need n≥30;
matchup cells report their n and calibration state; below-floor queries return
an explicit `insufficient_sample` reason, never a weak number. A tool cannot
be talked out of a floor; a prompt can.

**Coaching privacy — structural prerequisite**: the interactive workflow
currently passes only `author_name` (a display string) into the prompt; the
asker's `discord_user_id` never reaches `tool_exec`, so there is no hook to
enforce "coaching only for the member it is about." v1 threads
`requester_discord_user_id` from `channel_router` through the workflow into
tool execution; the capability resolves it via `discord_links` (is_primary)
and includes `coaching_note` only when the resolved tag == subject tag and the
workflow is interactive. Awareness/leader workflows get `commentary` only.
Tests assert both directions.

**Prompt-truth fixes shipped with the tool** (currently blocking any use of
the opponent data):
- `agent/prompt_builders.py:530` — delete "Elixir does NOT store opponent deck
  lists"; replace with guidance to use `get_battle_intelligence`.
- `capabilities/decks.py` `evidence_limits` — `opponent_decks_captured: True`.

## 8. Explicitly out of scope for v1

- Awareness-loop proactive posting from enrichment (`notable` exists; using it
  is v2, after the revision-trap snapshot question is settled).
- Duels: battles with `rounds_json` get computed metrics but are **excluded
  from prose and coaching aggregation** (concatenated decks would produce
  confident nonsense about war's highest-stakes battles).
- 2v2 prose (teammate deck context) — computed metrics only.
- Retiring the `_archetype()` cascade; card rollup table; war-prep
  opponent-clan precompute (the data supports it; build after v1 proves out).

## 9. Cost (measured, not estimated)

Haiku at $1/$5 per M; experiment measured 881 in / 534 out per battle-prose
call, less for profiles/cells.

| item | volume | cost |
|---|---|---|
| backfill: enrolled battles since 07-20 | ~94 + ~9/day | ~$0.35 + ~$1/mo |
| deck profiles (enrolled + opponents) | ≤~200 backfill, trickle after | <$1, amortizes |
| matchup matrix | 400 cells | ~$1 one-time per era |
| computed layers | everything else | $0 |

## 10. Build order (each phase = one commit, suite + simulator green)

1. **Foundation**: enum module, `_apply_v27`, Stage-A worker, computed
   backfill. Validate migration on a copy. No LLM, no behaviour change.
   *Restart needed (migration) — ask first.*
2. **LLM scoring**: three prompts, Stage-B worker, telemetry workflows,
   backfill profiles/cells/prose for enrolled members. Mutation-check the
   floors.
3. **The tool**: capability + wiring checklist + identity threading + privacy
   tests + prompt-truth fixes.
4. **Calibration + eval**: weekly calibration job, blend in the matchup view,
   `verdict` grading path (Jamie grades ~20 of his own battles before any
   prompt v2 — the benchmark that makes iteration non-vibes).

## 11. Risks

| risk | guard |
|---|---|
| model judgment instability | judgments minimized to loss_nature/notable; verdicts computed; dimensional scores |
| model staleness after balance patches | calibration + blend; supersede eras |
| coaching text leaking publicly | capability-level scope on requester identity; tests both directions |
| weak-sample confident claims | floors in the capability |
| silent worker death | work-set counts in job telemetry; missing rows are self-evident |
| prompt iteration blindness | verdict column graded against Jamie's own games |
| key drift duplicating rows | keys taken from `battle_events.dedup_key` verbatim; ingest idempotency suite |
