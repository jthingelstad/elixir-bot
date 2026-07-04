# Elixir v5.1 — Memory System (the deferred pass)

> **Status:** 🟡 Spec'd 2026-07-04 — this executes the pass `architecture.md`
> §0 deferred ("curated clan_memories, conversational memory, the
> interactive-conversation UX"). Decisions D1–D5 below need Jamie; build
> follows. Same conventions as the v5.1 set.
> **Owner:** Jamie · **Last worked:** 2026-07-04
>
> **The problem (Jamie):** memory "suffered through the same multiple
> generations" as the old engine. Assessment (2026-07-04, grounded against
> both live DBs) confirmed it emphatically — see §1. Direction: **build a new
> memory system in v5.1; migrate memories (content), not structure.**

## 1. Ground truth — what exists today

| Layer | State | Verdict |
|---|---|---|
| `clan_memories` (memory DB) — 3,886 rows, 87% confidence ≥0.9; 2,473 inferences + 1,364 system + 45 synthesis + 4 leader notes | Alive, declining (~8/day, last 06-29) | **The content worth keeping** |
| `memory_episodes` (engine DB) — 4,950 rows | **Alive — Gen A survivor**, written daily, read in every chat | The living conversation record |
| `memory_facts` — 20 rows | Dead since 06-16, never sunset | Export-and-drop |
| `clan_memory_member_links` / `event_links` / `evidence_refs` | **0 rows each** | Scaffolding never populated |
| `clan_memory_embeddings` + vec index | **0 of 3,886 embedded** — pipeline never ran | Dead weight |
| `clan_memories_fts` | Wired, unused in live paths | Half-built |
| `clan_memory_versions` (498) + `audit_log` (8,647) | Dual history nobody reads | One log is enough |
| Retrieval | 5-most-recent per category × 4 categories; no search, no ranking | The real capability gap |

Two DBs, two generations, four dead satellites, and the answer-time context is
just recency. The legacy `member_id` links in the memory DB dangle against the
retired synthetic ids (known debt since migration T12).

## 2. Design — lean, engine-native

### 2.1 One store, in the engine DB *(D1)*

New tables live in `elixir-v51.db`. The separate memory DB was a Gen-era
artifact; one DB means one backup (iCloud daily now), real joins
(memories ↔ players ↔ events), and the Observatory sees memory like
everything else. `elixir-v5-memory.db` retires to the archive family after
content migration. This also **undoes the T12 half-move** — conversation and
memory land together, coherently, instead of split across files.

### 2.2 Schema (5 tables replace ~20 objects)

```sql
CREATE TABLE memories (
    memory_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN
        ('leader_note','inference','system','synthesis','conversation_digest')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    summary TEXT,                        -- ≤220 chars, the context-window form
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    confidence REAL NOT NULL DEFAULT 0.9,
    member_tag TEXT,                     -- §7 discipline: tag, never an id
    channel_key TEXT,                    -- lane key when channel-scoped
    source_event_key TEXT,               -- soft ref to stream dedup keys
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    expires_at TEXT,                     -- NULL = durable
    retired_at TEXT                      -- soft-retire; hard-delete on expiry
);
CREATE INDEX idx_memories_member ON memories(member_tag, updated_at DESC);
CREATE INDEX idx_memories_kind ON memories(kind, updated_at DESC);

CREATE TABLE memory_tags (               -- flat; replaces tags + tag_links
    memory_id INTEGER NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);

CREATE TABLE memory_log (                -- ONE history (replaces versions+audit)
    log_id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL,
    action TEXT NOT NULL,                -- created|edited|retired|expired
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    diff_json TEXT
);

CREATE TABLE episodes (                  -- the Gen A survivor, kept ON PURPOSE (D3)
    episode_id INTEGER PRIMARY KEY,
    subject_type TEXT NOT NULL,          -- channel|member|discord_user
    subject_key TEXT NOT NULL,
    workflow TEXT,
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_key, workflow)
);

-- memories_fts: FTS5 over (title, body, summary) with sync triggers (D2).
```

`conversation_threads` + `messages` stay as-is (the transcript store works;
webapp chat already writes it).

### 2.3 Retrieval — deterministic ranking, finally

`build_memory_context` v2 (same call shape, new internals):
- **Candidates:** member-tag match, channel-key match, tag match on the
  query's subjects, plus FTS hits when the caller has a query string (the
  chat panel and `#ask-elixir` do).
- **Rank:** `score = w_match·(match strength) + w_conf·confidence +
  w_recency·decay(updated_at)` — pure code, tunable constants, no embeddings
  (v1.1 candidate if FTS proves insufficient).
- **Budget:** ~20 items as today, but *selected* instead of just recent.

### 2.4 Writers (same writers, one store)

Inference extraction (`agent/memory_tasks`), system observations, the weekly
synthesis job, leader notes / `save_clan_memory` tools, and — new — the
engine's recognition layer may write `system` memories for season recaps
(the §16.6 season-history seam). Episodes keep their current writer.
Expiry: `db-maintenance` hard-deletes past `expires_at`/`retired_at`+30d.

## 3. Migration (content, not structure)

| # | What | How |
|---|---|---|
| M1 | `clan_memories` 3,886 → `memories` | kind mapped (`elixir_synthesis`→`synthesis`), confidence/scope carried, `member_tag` from the *text* tag column (the id links were never populated — nothing to lose), tags flattened into `memory_tags` |
| M2 | `memory_episodes` 4,950 → `episodes` | wholesale (it's alive); no digest-conversion needed since the table survives *(D3)* |
| M3 | `memory_facts` 20 rows | one-time export into a single `system` memory ("legacy facts"), table dropped |
| M4 | Versions/audit | **not migrated** — the memory DB archive keeps them (D5: history starts fresh in `memory_log`) |
| M5 | `elixir-v5-memory.db` | archived (`elixir-v5-memory-archive-2026H2.db`, read-only) after M1 verifies; `memory_store/` seam repointed to the engine DB; `ELIXIR_V5_MEMORY_DB` retired |
| M6 | Backup + Observatory | db-backup drops the second DB from its set; Observatory gains a `/memories` view (browse/search — the memory half of "see what Elixir knows") |

Parity: `COUNT(memories) == 3,886 + 1` (M3), per-kind counts match the source
distribution, per-tag link counts match, every episode row carried, FTS
returns a known memory by a word in its body.

## 4. Decisions — D1–D5

| # | Question | Recommendation |
|---|---|---|
| D1 | Location: engine DB vs keep separate memory DB | **Engine DB** (one backup, joins, Observatory) |
| D2 | Search: FTS5 index? | **Yes** — cheap, serves chat/tools; **no embeddings in v1** |
| D3 | Episodes: keep as own table vs digest into memories | **Keep** — it's the living layer; digesting adds loss for no gain |
| D4 | Migrate all 3,886 memories vs curated subset | **All** — storage is trivial; ranking (not deletion) handles noise |
| D5 | History: single `memory_log` vs none | **Single log**, fresh from cut; archive keeps the old dual history |

## 5. Build plan (after D1–D5)

1. Schema: tables above into `schema_v51.py` (+count) and live CREATEs.
2. `engine/memory.py` (or evolve `memory_store/`): writers + ranked retrieval;
   `build_memory_context` v2 behind the existing call shape.
3. Migration script `scripts/migrate_v51/memory_migrate.py` (M1–M4, idempotent,
   parity prints), then M5 archive + seam repoint.
4. Observatory `/memories` (browse, search, member filter, kind filter).
5. Tests: ranking golden cases, migration parity, FTS round-trip, retention;
   grep gate: no `ELIXIR_V5_MEMORY_DB` readers left.
6. Docs: this file flips ✅; AGENTS.md memory section rewritten.
