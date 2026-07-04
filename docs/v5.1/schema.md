# Elixir v5.1 — Schema

> **Status:** ✅ Build-ready (completeness pass applied 2026-07-03; feedback.md rev 4).
> **Owner:** Jamie · **Last worked:** 2026-07-03
>
> The concrete data model for the v5.1 engine. Grounded against the live
> `elixir-v5.db` (86 objects: 75 designed tables + 11 FTS/vec/sequence shadows) and
> the storage layer on 2026-07-02. Honors `architecture.md` §7 (tag-keyed identity),
> §8 (emitter/envelope), §14 (layers & retention), and the decisions in
> `open-questions.md` (Q1–Q8, C1–C6). Where this doc and `architecture.md` disagree,
> this doc wins for implementation (README convention).

## 1. Conventions

- **Keys (§7):** if the CR API identifies it with a tag, the tag is the key
  (`player_tag`, `clan_tag`, `tournament_tag`, `discord_user_id`). Internal-only
  entities keep synthetic ids. No table carries `member_id` after the cut.
- **Timestamps:** UTC ISO-8601 `TEXT`. Two kinds, per §8: `occurred_at` (real event
  time; battle stream only) and `observed_at` (when we saw it). Emitted events carry
  `timing = 'estimated'`; battle events `timing = 'exact'`.
- **Scope:** `scope TEXT CHECK (scope IN ('public','leadership'))` on anything
  recognition can surface.
- **Retention constants** (one module, `db/retention.py`, replacing the scattered
  values in `storage/metadata.py`):

| Constant | Value | Applies to |
|---|---|---|
| `RAW_PAYLOAD_RETENTION_DAYS` | 14 | `raw_api_payloads` (unchanged) |
| `BATTLE_EVENT_RETENTION_DAYS` | **180** | `battle_events` (Q8: ≥3 seasons) |
| `PLAYER_EVENT_RETENTION_DAYS` | 180 | `player_events` |
| `CLAN_EVENT_RETENTION_DAYS` | 365 | `clan_events` (rare, load-bearing for history reads) |
| `WAR_RETENTION_DAYS` | 365 | `war_events` + war week/attendance detail (season summaries durable) |
| `CONVERSATION_RETENTION_DAYS` | 30 | `messages` (unchanged; deferred pass owns it) |
| `TOURNAMENT_RETENTION_DAYS` | 365 | tournaments star (unchanged) |
| — durable, never purged | ∞ | rollups, identity/tenure, awards, `war_seasons`, recognition ledger, curated memories |

  *One deliberate asymmetry to know about: war summaries (`war_seasons` durable,
  `war_weeks`/`war_participation` 365d) outlive the battles behind them
  (`battle_events` 180d). Battle-level war reads — war-deck reconstruction,
  `get_member_war_detail` battles — therefore cover only the trailing ~4–6
  seasons; older seasons answer from summaries, participation, and awards.
  Accepted: deck-level detail loses meaning across balance patches, and the Q8
  rollup rule applies (older battle reads are rollup reads).*

- **Naming:** `player_*` = account-scoped (CR account). `member_*` = membership-scoped
  (only meaningful while in POAP KINGS). `war_*` = the bounded war stream. This makes
  §7's player/member distinction visible in the schema itself.

## 2. Layer map — the spine

Every designed table belongs to exactly one layer (architecture §14.2). Counts are
for the engine after the cut; the deferred memory/conversation pass carries its ~19
tables unchanged and is not redesigned here.

| Layer | Tables | Count |
|---|---|---|
| L1 Raw response log (14d) | `raw_api_payloads` | 1 |
| L2 Current-state baselines | `state_baselines` | 1 |
| L3 Event streams | `battle_events`, `player_events`, `clan_events`, `war_events` | 4 |
| L4 Rollups (durable) | `player_daily_metrics`, `player_daily_battle_rollups`, `clan_daily_metrics`, `clan_daily_battle_rollups` | 4 |
| L5 Identity & tenure (durable) | `players`, `player_metadata`, `player_aliases`, `clans`, `discord_users`, `discord_links`, `clan_memberships` | 7 |
| L6 Projections / read models | `player_current_state`, `player_card_collection`, `player_recent_form`, `member_management` | 4 |
| Recognition & delivery | `recognition_ledger`, `communication_intents` | 2 |
| Clan management | `leader_action_recommendations`, `decision_cases`, `revisits` | 3 |
| Bounded stream: war | `war_seasons`, `war_weeks`, `war_week_clans`, `war_participation`, `war_attendance_days` | 5 |
| Bounded stream: tournaments | `tournaments`, `tournament_battles`, `tournament_participants` | 3 |
| Awards (durable) | `awards` | 1 |
| Engine control | `stream_cursors` (durable), `runtime_job_status`, `poll_state` (runtime.md §4) | 3 |
| Ops singletons (carried) | `llm_calls`, `prompt_failures`, `prompt_feedback`, `system_signals`, `api_sentinel_observations`, `arena_relay_screenshot_observations`, `discord_channels`, `channel_state`, `game_mode_contexts`, `card_catalog`, `elixir_improvement_suggestions` | 11 |
| **Engine total** | | **49** |
| Deferred pass (carried unchanged) | `clan_memories` + 9 satellites + FTS/vec, `conversation_threads`, `messages`, `memory_facts`, `memory_episodes` | ~19 designed |

49 engine tables from today's 75, with the memory-pass reduction still to come —
inside the §5 target once the deferred pass lands. 33 of today's table names
cease to exist: 26 dropped outright, 7 transformed to tag-keyed successors
(§8 below).

*(`cake_day_announcements` was originally listed as a carried ops singleton; it
drops instead — verified 2026-07-03: the table is **empty**, on a 7-day purge
(`storage/metadata.py:411`), written only by the Gen-B roster path
(`heartbeat/_roster.py:507`, deleted at the cut), and its dedup role is fully
covered by event dedup keys + the recognition ledger. Cross-cut calendar re-post
protection comes from migration T14's ledger seeding, not this table.)*

## 3. Identity & tenure (L5)

The identity *model* is today's (confidence-scored Discord links, alias history,
tenure spans) re-keyed to tags. Source shapes verified against the live DDL.

```sql
CREATE TABLE players (                      -- was: members (member_id dropped)
    player_tag    TEXT PRIMARY KEY,         -- '#ABC123', canonical uppercase
    current_name  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
-- members.status is dropped: "member" = has an open clan_memberships row (§7).

CREATE TABLE player_metadata (              -- was: member_metadata, 1:1
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    joined_at TEXT, birth_month INTEGER, birth_day INTEGER,
    profile_url TEXT DEFAULT '', note TEXT DEFAULT '',
    generated_bio TEXT DEFAULT '', generated_highlight TEXT DEFAULT '',
    generated_profile_updated_at TEXT,
    -- cr_* enrichment columns carried as-is (account age, games/day, collection
    -- level, badges — see live member_metadata DDL)
    cr_account_age_days INTEGER, cr_account_age_years INTEGER,
    cr_account_age_updated_at TEXT, cr_games_per_day REAL,
    cr_games_per_day_window_days INTEGER, cr_games_per_day_updated_at TEXT,
    cr_collection_level INTEGER, cr_collection_level_badge_tier INTEGER,
    cr_collection_level_badge_max_tier INTEGER, cr_collection_level_updated_at TEXT,
    cr_clan_war_wins INTEGER, cr_battle_wins INTEGER, cr_clan_donations INTEGER,
    cr_banner_count INTEGER, cr_emote_count INTEGER, cr_profile_badges_updated_at TEXT
);
-- poap_address is dropped (Q4: POAP paused; the archive keeps historical values).

CREATE TABLE player_aliases (               -- was: member_aliases
    alias_id    INTEGER PRIMARY KEY,
    player_tag  TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    alias TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
    UNIQUE(player_tag, alias)
);

CREATE TABLE clans (                        -- NEW: our clan + River-Race opponents,
    clan_tag   TEXT PRIMARY KEY,            -- one shape (§7, feedback New-3)
    name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    is_home INTEGER NOT NULL DEFAULT 0      -- 1 for #J2RGCRVG exactly once
);

CREATE TABLE discord_users (                -- carried as-is (already natural-keyed)
    discord_user_id TEXT PRIMARY KEY,
    username TEXT, global_name TEXT, display_name TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);

CREATE TABLE discord_links (                -- re-keyed: discord_user_id ↔ player_tag
    discord_link_id INTEGER PRIMARY KEY,
    discord_user_id TEXT NOT NULL REFERENCES discord_users(discord_user_id) ON DELETE CASCADE,
    player_tag      TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    linked_at TEXT NOT NULL, source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_primary INTEGER NOT NULL DEFAULT 1,
    UNIQUE(discord_user_id, player_tag)
);
-- discord_username / discord_display_name columns drop: they duplicate discord_users.

CREATE TABLE clan_memberships (             -- re-keyed; tenure spans, durable
    membership_id INTEGER PRIMARY KEY,
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    clan_tag   TEXT NOT NULL DEFAULT '#J2RGCRVG' REFERENCES clans(clan_tag),
    joined_at TEXT NOT NULL, left_at TEXT,
    join_source TEXT NOT NULL, leave_source TEXT
);
CREATE INDEX idx_memberships_open ON clan_memberships(player_tag) WHERE left_at IS NULL;
```

**"Is X a member?"** = `EXISTS (open membership)`. The old `members.status` column
and its drift potential are gone.

## 4. Ingest & baselines (L1–L2)

```sql
-- raw_api_payloads: carried exactly as-is (verified DDL), 14-day purge.
-- One change at the cut (C3): the endpoint label alias in cr_api.py
-- (_RAW_PAYLOAD_ENDPOINT_LABELS, cr_api.py:23–25) is deleted; riverracelog
-- payloads are stored as 'riverracelog'.

CREATE TABLE state_baselines (              -- NEW: the §8 diff baseline, one row
    entity_kind  TEXT NOT NULL,             -- 'player' | 'clan' | 'riverrace'
    entity_tag   TEXT NOT NULL,             -- player_tag / clan_tag; for the
                                            -- riverrace kind: our clan_tag, aspect 'race'
                                            -- (the one row every "war clock" read uses —
                                            -- referenced elsewhere as state_baselines('riverrace'))
    aspect       TEXT NOT NULL,             -- e.g. 'profile','cards','roster','race'
    payload_json TEXT NOT NULL,             -- last known state of this aspect
    payload_hash TEXT NOT NULL,             -- fast no-change check
    observed_at  TEXT NOT NULL,
    prev_observed_at TEXT,                  -- bounds the (prev, now] estimate window
    PRIMARY KEY (entity_kind, entity_tag, aspect)
);
```

This single table replaces the diff-source role of `member_state_snapshots`,
`player_profile_snapshots`, `member_card_collection_snapshots`,
`member_card_usage_snapshots`, `member_deck_snapshots`, `war_current_state`, and
`war_participant_snapshots` (Part I §2's sprawl). It is **not** a read model — reads
go to L6. First-sight rule (§8): inserting a row where none existed emits nothing.

## 5. Event streams (L3)

### 5.1 `battle_events` — native stream

Successor to `battle_telemetry` (the §7-designated canonical survivor; live DDL
carried) with three changes: an explicit dedup key, war-race keys (§14.5 — war-deck
reconstruction and attendance become joins, not inference), and a deliberate 180-day
retention (Q8).

```sql
CREATE TABLE battle_events (
    dedup_key    TEXT PRIMARY KEY,          -- '{player_tag}:{battle_time}:{opponent_tag}'
    player_tag   TEXT NOT NULL,
    battle_time  TEXT NOT NULL,             -- occurred_at; timing is always 'exact'
    observed_at  TEXT NOT NULL,
    -- typed telemetry, carried from battle_telemetry (verified live DDL):
    battle_type TEXT, opponent_tag TEXT, crowns_for INTEGER, crowns_against INTEGER,
    game_mode_id INTEGER, game_mode_name TEXT, mode_group TEXT, outcome TEXT,
    is_war INTEGER, is_ladder INTEGER, is_ranked INTEGER, is_competitive INTEGER,
    is_special_event INTEGER, trophy_change INTEGER, starting_trophies INTEGER,
    deck_selection TEXT, deck_json TEXT,
    arena_id INTEGER, arena_name TEXT, league_number INTEGER,
    is_hosted_match INTEGER, tournament_tag TEXT, event_tag TEXT,
    -- war-race keys, populated when is_war = 1 (§14.5):
    season_id INTEGER, section_index INTEGER, war_day_index INTEGER
);
CREATE INDEX idx_battle_events_player_time ON battle_events(player_tag, battle_time DESC);
CREATE INDEX idx_battle_events_war ON battle_events(season_id, section_index, player_tag) WHERE is_war = 1;
CREATE INDEX idx_battle_events_time ON battle_events(battle_time);  -- purge scan
```

The battle stream realizes the §8 envelope with **typed columns instead of a payload
blob** because it is the read-heavy stream (win rates, form, mode activity, war decks
all query it). `deck_json` is added so war-deck reconstruction stops parsing
`member_battle_facts.raw_json` (C6-adjacent cleanup; raw payload stays in L1 only).

### 5.2 `player_events` / `clan_events` — emitted streams

The §8 envelope, one table per stream (decided, §11: per-stream logs, not one
tagged log; the bounded war stream's log is §5.3).

```sql
CREATE TABLE player_events (
    event_id    INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,       -- '{event_type}:{player_tag}:{natural-key}'
    event_type  TEXT NOT NULL,              -- catalog in events.md
    player_tag  TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,                      -- (prev_observed_at, observed_at] honesty
    evidence_json TEXT,                     -- baseline delta / battle dedup_key(s)
    payload_json  TEXT NOT NULL,            -- typed facts, presentation-free
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_player_events_subject ON player_events(player_tag, observed_at DESC);
CREATE INDEX idx_player_events_type ON player_events(event_type, observed_at DESC);

CREATE TABLE clan_events (                  -- same envelope; subject is a tag that
    event_id    INTEGER PRIMARY KEY,        -- may be a player (join/promote) or the
    dedup_key   TEXT NOT NULL UNIQUE,       -- clan itself (score/league movement)
    event_type  TEXT NOT NULL,
    clan_tag    TEXT NOT NULL,
    subject_tag TEXT,                       -- player_tag for membership events, NULL for clan-entity events
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,
    evidence_json TEXT,
    payload_json  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_clan_events_subject ON clan_events(subject_tag, observed_at DESC);
CREATE INDEX idx_clan_events_type ON clan_events(event_type, observed_at DESC);
```

`clan_events` at 365 days deliberately exceeds the other streams: role changes and
join/leave history are first-class reads (§14.5) and the volume is tiny (a handful of
rows on a busy day).

### 5.3 `war_events` — the bounded war stream's log

Added in the 2026-07-03 review (feedback.md rev 5): events.md §5 catalogs six war
event types and the runtime emits/consumes them, but no table existed — every §12
bounded stream needs "its own log," and the war recognizer needs a normal cursor.
Same envelope as `clan_events`; volume is ~10 rows/week.

```sql
CREATE TABLE war_events (
    event_id    INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,       -- catalog in events.md §5
    event_type  TEXT NOT NULL,
    season_id   INTEGER NOT NULL,
    section_index INTEGER,                  -- NULL for season-scoped events
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,
    evidence_json TEXT,
    payload_json  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_war_events_season ON war_events(season_id, section_index, observed_at);
CREATE INDEX idx_war_events_type ON war_events(event_type, observed_at DESC);
```

Retention: `WAR_RETENTION_DAYS` (365), same as the war detail tables. Tournament
bounded streams do **not** get an event table — their lifecycle moments
(`tournament_watch_started` / `tournament_completed`) are ledger-claimed
recognition moments computed from the `tournaments` star, the same pattern as
derived battle moments (events.md §5).

## 6. Rollups (L4, durable) and projections (L6)

### 6.1 Rollups — carried, re-keyed

`member_daily_battle_rollups` and `clan_daily_battle_rollups` already exist with the
right shape — including `captured_battles` / `expected_battle_delta` /
`completeness_ratio`, which is the honest accounting for the battlelog ceiling
(§17.7; observed depth mode 30, range 12–59). They carry forward with one change:
`member_id` → `player_tag` (→ `player_daily_battle_rollups`). Likewise
`member_daily_metrics` → `player_daily_metrics` (same columns, tag-keyed);
`clan_daily_metrics` is already `clan_tag`-keyed and carries as-is minus its
`raw_json` column (L1 owns raw).

### 6.2 Projections — rebuilt from streams, disposable

```sql
CREATE TABLE player_current_state (         -- was: member_current_state; O(1) "now" read
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    role TEXT, exp_level INTEGER, trophies INTEGER, best_trophies INTEGER,
    clan_rank INTEGER, previous_clan_rank INTEGER,
    donations_week INTEGER, donations_received_week INTEGER,
    arena_id INTEGER, arena_name TEXT,
    ranked_league INTEGER, ranked_trophies INTEGER,   -- PoL, was in profile snapshots
    current_deck_json TEXT                            -- replaces member_deck_snapshots
);
-- last_seen_api is dropped: §13.6 — we deliberately don't use lastSeen.

CREATE TABLE player_card_collection (       -- current collection per (player, card);
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    card_id INTEGER NOT NULL,               -- FK → card_catalog
    level INTEGER, count INTEGER, star_level INTEGER, evolution_level INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (player_tag, card_id)
);  -- replaces member_card_collection_snapshots for all "current cards" reads;
    -- history = player_events card milestones. ~120 cards × 50 members: trivial.

CREATE TABLE player_recent_form (           -- was: member_recent_form; same shape,
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    scope TEXT NOT NULL,                    -- 'all' | mode_group
    computed_at TEXT NOT NULL, sample_size INTEGER NOT NULL,
    wins INTEGER NOT NULL, losses INTEGER NOT NULL, draws INTEGER NOT NULL,
    current_streak INTEGER NOT NULL DEFAULT 0, current_streak_type TEXT,
    win_rate REAL NOT NULL DEFAULT 0, avg_crown_diff REAL, avg_trophy_change REAL,
    form_label TEXT, summary TEXT,
    PRIMARY KEY (player_tag, scope)
);  -- §14.5: pre-materialized (refreshed when new battles land), never per-call.
```

### 6.3 `member_management` — the §13.3 projection

One authoritative row per **member** (open membership), refreshed weekly plus on
new war/battle data (Q1). Columns are the Layer-1 evaluator outputs and Layer-2
candidacy states, not raw metrics — the LLM reads *state*, code owns math.

```sql
CREATE TABLE member_management (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    computed_at TEXT NOT NULL,
    week_anchor TEXT NOT NULL,              -- ISO date of the weekly grain
    -- deterministic metrics (inputs, kept for evidence rendering):
    tenure_days INTEGER, role TEXT,
    donations_4wk_avg REAL, war_fame_3season_avg REAL,
    war_attendance_rate REAL,               -- decks used / decks available, window in CLAN.md
    battle_days_last_28 INTEGER,            -- §13.6: battling, never lastSeen
    -- Layer-1 sustained-signal evaluator states ('holding'|'building'|'lapsed'):
    sustained_donor TEXT, war_reliable TEXT, battle_active TEXT,
    -- Layer-2 candidacy state machines with hysteresis:
    promote_state TEXT NOT NULL DEFAULT 'none',   -- none|building|eligible|recommended
    demote_state  TEXT NOT NULL DEFAULT 'none',
    kick_state    TEXT NOT NULL DEFAULT 'none',   -- none|watch|at_risk|recommended
    promote_qualifying_weeks INTEGER NOT NULL DEFAULT 0,
    kick_state_since TEXT,
    state_json TEXT NOT NULL DEFAULT '{}'   -- evaluator internals for auto-withdraw (§13.4)
);
```

Thresholds and windows live in `CLAN.md` (§13.3; precedent: `CLAN.md:120–127`);
the evaluator and candidacy **transition rules** are specified in
`management.md`. Kick-risk transitions (`kick_state` → `recommended`) fire the
Q1 reactive path; everything else waits for the weekly review.

## 7. Recognition, delivery, clan management, war, awards

### 7.1 Recognition ledger — durable engine state (§10, feedback New-4)

```sql
CREATE TABLE recognition_ledger (
    recognition_key TEXT PRIMARY KEY,       -- 'arena_up:{tag}:{arena}', 'champion_unlock:{tag}:{card}'
    stream TEXT NOT NULL,                   -- which stream claimed it first
    event_refs_json TEXT NOT NULL,          -- contributing event dedup_keys (cross-stream)
    score INTEGER NOT NULL,                 -- notability score at claim time
    claimed_at TEXT NOT NULL,
    intent_id INTEGER                       -- NULL until an intent is raised (claim ≠ post)
);
```

First claim wins via the PK; the second stream's `INSERT` conflicts and backs off.
Never purged (reset ⇒ double-posts, §14.2).

### 7.2 `communication_intents` — the single implementation

Replaces both `storage/communication_intents.py` (Gen B, ~29 KB) and
`event_core/domain/communication_intent.py` (Gen C) — Part I §1's duplicated concept
collapses to one table with the §17.3 delivery semantics built in.

```sql
CREATE TABLE communication_intents (
    intent_id INTEGER PRIMARY KEY,
    recognition_key TEXT REFERENCES recognition_ledger(recognition_key),
    intent_type TEXT NOT NULL,              -- routing prefix (recognition.md owns map)
    lane TEXT NOT NULL,                     -- resolved destination lane
    scope TEXT NOT NULL CHECK (scope IN ('public','leadership')),
    payload_json TEXT NOT NULL,             -- presentation-free facts for composition
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','fulfilled','failed','expired')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,   -- created + 6h (§17.3)
    fulfilled_at TEXT, discord_message_id TEXT, last_error TEXT
);
CREATE INDEX idx_intents_pending ON communication_intents(status, expires_at) WHERE status IN ('pending','failed');
```

At-least-once contract (§17.3): mark `fulfilled` only on confirmed send; `failed`
retries next tick; past `expires_at` → `expired` (drop stale). `runtime.md` owns the
loop; this table is its state.

### 7.3 Clan management & leader actions

- `leader_action_recommendations` — **carried with its full feedback apparatus**
  (status flow, decision emoji/notes, copy-edit diffs, baseline/outcome JSON,
  `is_test`; verified live DDL). Changes: none structural — it is already
  `target_player_tag`-keyed. Kick-suppression (C1) keeps reading it.
- `decision_cases` — **carried, slimmed to one implementation** (the live table is
  already generic + tag-keyed; verified DDL). It serves the clanops write tools
  (`flag_member_watch`, `record_leadership_followup`) and the member-review queue.
  Columns `source_signal_key` / `source_signal_type` are renamed
  `source_event_key` / `source_event_type` (Gen B naming retired).
- `revisits` — carried as-is (`signal_key` renamed `revisit_key`; it is an opaque
  dedup string, not a Gen B FK — verified: `UNIQUE(signal_key, due_at)`, no FK).

### 7.4 Bounded war stream

```sql
CREATE TABLE war_seasons (                  -- durable; one row per bounded instance
    season_id INTEGER PRIMARY KEY,          -- CR seasonId
    started_at TEXT NOT NULL,               -- first observation of the seasonId
    colosseum_detected_at TEXT,             -- §16.1: end is discovered, not known
    ended_at TEXT,
    final_rank INTEGER, weeks INTEGER,
    war_champ_tag TEXT,                     -- Q2: always top fame
    free_pass_tag TEXT,                     -- Q2/C5: rotation rule; usually = champ
    recap_json TEXT                         -- §16.6 season recap payload
);

CREATE TABLE war_weeks (                    -- was: war_races (transform); 365d
    season_id INTEGER NOT NULL REFERENCES war_seasons(season_id),
    section_index INTEGER NOT NULL,
    period_type TEXT,                       -- 'training'|'warDay'|'colosseum' (verified payloads)
    created_date TEXT, finish_time TEXT,
    our_rank INTEGER, our_fame INTEGER, trophy_change INTEGER, our_clan_score INTEGER,
    PRIMARY KEY (season_id, section_index)
);

CREATE TABLE war_week_clans (               -- was: war_period_clan_status; the 5-clan
    season_id INTEGER NOT NULL,             -- race in one shape for ours + opponents (§7)
    section_index INTEGER NOT NULL,
    clan_tag TEXT NOT NULL REFERENCES clans(clan_tag),
    fame INTEGER, rank INTEGER, clan_score INTEGER, completed_at TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, clan_tag)
);

CREATE TABLE war_participation (            -- re-keyed; week-cumulative per member
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    player_tag TEXT NOT NULL,
    fame INTEGER, repair_points INTEGER, boat_attacks INTEGER,
    decks_used INTEGER, decks_used_today INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, player_tag)
);

CREATE TABLE war_attendance_days (          -- NEW: per-day attendance as data, not
    season_id INTEGER NOT NULL,             -- inference (§14.5); feeds war-reliable
    section_index INTEGER NOT NULL,         -- evaluator + Iron King
    war_day_index INTEGER NOT NULL,
    player_tag TEXT NOT NULL,
    decks_used INTEGER NOT NULL DEFAULT 0, decks_available INTEGER NOT NULL DEFAULT 4,
    fame_delta INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, war_day_index, player_tag)
);
```

**War Champ standings are a query, not a table:** cumulative fame per player over
`war_participation` for a season — cheap at this scale, always consistent. At season
close, code writes the outcome into `war_seasons` (honor + free-pass, applying Q2's
rotation via last season's `free_pass_tag`) and the awards pass records the durable
rows. The **war clock** (§16.2) is computed in code from the live race baseline
(`state_baselines` aspect `race`) — it has no table.

### 7.5 Awards (Q5, durable)

```sql
CREATE TABLE awards (                       -- re-keyed: member_id dropped
    award_id INTEGER PRIMARY KEY,
    award_type TEXT NOT NULL,               -- war_champ | free_pass | iron_king |
                                            -- donation_champ | rookie_mvp | war_participant
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL DEFAULT -1,
    player_tag TEXT NOT NULL REFERENCES players(player_tag),
    rank INTEGER NOT NULL DEFAULT 1,
    metric_value REAL, metric_unit TEXT, metadata_json TEXT,
    awarded_at TEXT NOT NULL,
    UNIQUE(award_type, season_id, section_index, player_tag)
);
```

**`war_champ` is a podium**, carried behavior: three rows per season, ranks 1–3
(`heartbeat/_awards.py` grants top-3; verified live — 4 seasons × 3 rows). The
**War Champ honor is rank 1**; ranks 2–3 are podium records for `get_awards`
leaderboards. `free_pass` is the new Q2/C5 ledger row — **exactly one per
season**, seeded at the cut from archived **rank-1** `war_champ` rows only
(historically champ = pass recipient; seeding all podium rows would mint three
passes per season). The three deprecated types (`perfect_week`, `victory_lap`,
`donation_champ_weekly`, `heartbeat/_awards.py:32`) do not carry. Grants fire on
the war stream's season-death event (Q5), keyed idempotently by the UNIQUE
constraint.

### 7.6 Engine control & tournaments

- `stream_cursors` — successor to `signal_detector_cursors` (same proven shape,
  verified DDL): `(consumer_key, scope_key, cursor_text, cursor_int, updated_at,
  metadata_json)`, PK `(consumer_key, scope_key)`. Every emitter, recognizer, and
  projector tracks its position here. Durable. `ingest_cursor` and
  `projection_tracking` fold in and drop.
- `poll_state` — per-player adaptive-polling state (temperature, heat counter,
  last-poll timestamps). DDL and semantics in `runtime.md` §4; listed here so the
  layer map is complete. Rebuildable (seeds warm), not durable-precious.
- Tournaments star (`tournaments`, `tournament_battles`, `tournament_participants`) —
  carried; `tournament_battles.player1_member_id`/`player2_member_id` drop (tags
  already present in the same rows; verified DDL).

## 8. Gone at the cut — 26 dropped, 7 transformed

All 33 names verified present in the live DB (2026-07-03); all preserved in the
cold archive (§14.4). Two distinct dispositions — the acceptance check is the
same (no listed name exists post-cut), but only the transforms carry data.

### 8.1 Dropped outright (26) — no data carried

| Dropped | Replaced by |
|---|---|
| `game_event_stream`, `event_rollups`, `project_event_links`, `communication_intent_event_links` (Gen A + bridges) | streams (L3), rollups (L4), `recognition_ledger.event_refs_json` |
| `signal_log`, `signal_outcomes`, `awareness_ticks` (Gen B) | `communication_intents` + `stream_cursors` |
| `detections`, `elixir_projects` (Gen C engine internals) | stream events + recognition ledger (projects retire with no v5.1 equivalent; decision support lives in `decision_cases` / leader actions). The archive's `detections` also seeds the ledger's calendar claims (migration T14). |
| `member_battle_facts` | `battle_events` (§7: tag-keyed survivor; stream starts fresh) |
| `member_state_snapshots`, `player_profile_snapshots` | `state_baselines` + `player_events` + `player_daily_metrics` |
| `member_card_collection_snapshots`, `member_card_usage_snapshots`, `member_deck_snapshots` | `player_card_collection`, `battle_events.deck_json`, `player_current_state.current_deck_json` |
| `member_current_state`, `member_recent_form` | `player_current_state`, `player_recent_form` — rebuilt fresh from streams (L6 is disposable) |
| `war_current_state`, `war_day_status`, `war_participant_snapshots` | `state_baselines('riverrace')`, `war_attendance_days` (starts empty, T9) |
| `clan_voyages`, `clan_voyage_entries` (Q6/C6) | — (dead end; archive only) |
| `ingest_cursor`, `projection_tracking`, `signal_detector_cursors` | `stream_cursors` (§7.6; positions initialize at stream head, `runtime.md` §6) |
| `cake_day_announcements` | event dedup keys + recognition ledger (empty Gen-B dedup table; see §2 note) |

### 8.2 Transformed (7) — data carried under a tag-keyed successor

| Old name | Successor | Transform |
|---|---|---|
| `members` | `players` | T1 |
| `member_metadata` | `player_metadata` | T2 |
| `member_aliases` | `player_aliases` | T1 |
| `member_daily_metrics` | `player_daily_metrics` | T7 |
| `member_daily_battle_rollups` | `player_daily_battle_rollups` | T7 |
| `war_races` | `war_seasons` + `war_weeks` | T9 |
| `war_period_clan_status` | `war_week_clans` | T9 |

*(Re-keyed in place, name unchanged — not in this list: `discord_links`,
`clan_memberships`, `war_participation`, `awards`, `tournaments` star,
`decision_cases`, `revisits`.)*

## 9. Read-model coverage matrix (§14.5 / §17.1)

Every query tool and aspect, mapped from its current tables (traced through
`agent/tool_defs.py` → `agent/tool_exec.py` → `storage/` on 2026-07-02) to its v5.1
source. `get_clan_voyage` is removed (C6). Live-API tools (`cr_api`,
`get_member(chests)`, `get_clan_intel_report`) are unaffected by the schema and
omitted.

| Tool | Aspect | Reads today | Reads in v5.1 |
|---|---|---|---|
| `resolve_member` | — | members, member_aliases, discord_links, discord_users | players, player_aliases, discord_links, discord_users |
| `get_member` | profile | members, member_state_snapshots, player_profile_snapshots | players, player_current_state, player_metadata |
| | form | member_recent_form | player_recent_form |
| | battles / losses / mode_activity | member_battle_facts | battle_events |
| | war | war_current_state, war_races, war_participation | war clock (code), war_weeks, war_participation |
| | trend | member_daily_metrics | player_daily_metrics |
| | deck | member_deck_snapshots, cards | player_current_state.current_deck_json, card_catalog |
| | history | member_state_snapshots | player_events + clan_events (role) + player_daily_metrics |
| | ranked | player_profile_snapshots | player_current_state (ranked_* cols) |
| | memories | clan_memories | unchanged (deferred pass) |
| | awards | awards | awards (tag-keyed) |
| `get_member_war_detail` | summary / attendance / missed_days | war_participation, war_races | war_participation, war_attendance_days, war_weeks |
| | battles / war_decks | member_battle_facts (raw_json inference) | battle_events (war keys + deck_json — a join, per §14.5) |
| | vs_clan_avg | war_participation (+ snapshots) | war_participation |
| `get_river_race` | standings / engagement | war_current_state, war_day_status, war_races, war_participation | state_baselines('riverrace') via war clock, war_week_clans, war_participation |
| `get_war_season` | summary / score_trend / season_comparison / perfect_attendance / no_participation | war_races, war_participation, members | war_seasons, war_weeks, war_participation, war_attendance_days, players |
| | standings | war_participation, awards | war_participation (standings query, §7.4), awards |
| | win_rates / boat_battles | member_battle_facts, war_participation | battle_events (war keys), war_participation |
| | trending | war_participation, member_state_snapshots | war_participation, player_daily_metrics |
| `get_clan_roster` | list / summary | members, member_state_snapshots, member_metadata | players + player_current_state + player_metadata + open memberships |
| | recent_joins / longest_tenure | clan_memberships (+ members) | clan_memberships (tag-keyed) |
| | role_changes | member_state_snapshots (snapshot diffing) | clan_events(role_change) — first-class (§14.5) |
| | max_cards | member_card_collection_snapshots | player_card_collection |
| | trends | member_daily_metrics | player_daily_metrics + clan_daily_metrics |
| `get_clan_health` | at_risk | members, member_state_snapshots, war_participation | member_management (kick_state + evidence cols) |
| | hot_streaks / losing_streaks | member_battle_facts | player_recent_form (streak cols) |
| | trophy_drops | member_daily_metrics | player_daily_metrics |
| | promotion_candidates | members, war_participation, member_daily_metrics | member_management (promote_state) |
| `get_clan_game_modes` | — | member_battle_facts | player_daily_battle_rollups (+ battle_events for recent) |
| `lookup_cards` / card tools | — | card_catalog, member_card_collection_snapshots | card_catalog, player_card_collection |
| `get_awards` | list / leaderboard / current_standings | awards, war_participation | awards, war_participation standings query |
| `get_elixir_state` | event_summary / recent_events / game_modes | detections + event_core tables | player_events / clan_events / battle_events / war_events + game_mode_contexts |
| | decision_cases / communication_* | decision_cases, communication_intents (+ Gen A/B links) | decision_cases, communication_intents, recognition_ledger |
| | season_window / war_season | war_races | war_seasons, war_weeks |
| write: `update_member` | 4 fields | member_metadata | player_metadata |
| write: `save_clan_memory` | — | clan_memories (+ satellites) | unchanged (deferred pass) |
| write: `flag_member_watch` / `record_leadership_followup` | — | decision_cases, clan_memories | decision_cases (renamed source cols), clan_memories |
| write: `schedule_revisit` | — | revisits | revisits |

**Notable upgrades over today:** `at_risk` and `promotion_candidates` stop
recomputing eligibility per call and read the §13.3 projection — the tool output and
the leader-action pipeline can no longer disagree. `hot_streaks`/`losing_streaks`
read pre-materialized form instead of scanning battles. `role_changes` and `history`
stop diffing snapshot pairs.

## 10. What this doc leaves to the next docs

- **`events.md`:** the `event_type` catalog per stream, payload shapes, dedup-key
  formulas, and which stream owns each type (including the C2 mapping input).
- **`recognition.md`:** scorer constants port, `recognition_key` formulas,
  intent-prefix → lane map, composition enrichment.
- **`runtime.md`:** the tick loop that reads/writes these tables, cursor discipline,
  the §17.3 delivery contract, and adaptive polling (its `poll_state` table is
  defined there, §7.6 here lists it).
- **`migration.md`:** the transform scripts implied by every "re-key/transform" above,
  baseline seeding (§17.7), archive freeze, and the drop list (§8).
