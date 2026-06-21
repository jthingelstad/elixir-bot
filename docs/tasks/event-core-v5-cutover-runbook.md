# Event Core v5 — Cutover Runbook

Branch: `feat/event-core-v5`. Status at time of writing: full v5 core built and
validated **offline**; Elixir **OFF**; no cutover performed. This runbook takes it
to production.

## Operating philosophy: roll forward, no rollback

We do **not** keep a rollback path. Recovery at every stage is **fix-and-rebuild**,
which is safe because the system is reproducible by construction:

- projections rebuild deterministically from the event store,
- the event store rebuilds from the frozen archive via the one ingest path,
- the CR API re-poll re-establishes current state after restart.

So "if wrong" below always means: stop, fix the code, re-run the build, re-verify —
never restore the old system. The frozen `elixir.db.legacy` is kept untouched as a
data source until decommission, but we roll *forward* off it, not back to it.

## Fixed facts

- Service: launchd `com.poapkings.elixir`, plist
  `~/Library/LaunchAgents/com.poapkings.elixir.plist` (`KeepAlive=true`).
  - Stop: `launchctl bootout gui/$(id -u)/com.poapkings.elixir`
  - Start: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.poapkings.elixir.plist`
- DBs: `elixir-v5-events.db` (library event store), `elixir-v5.db` (projections +
  operational survivors), `elixir-v5-memory.db` (memory/embeddings).
  `elixir.db.legacy` = frozen pre-v5 source; live `elixir.db` is static (Elixir off).
- Build drivers: `python -m event_core.build_foundation`,
  `event_core.mind.build`, `event_core.mind.leadership_build`,
  `event_core.mind.reactive_build`.
- Tests: `pytest tests/test_event_core_*.py`.

Because Elixir has been off the whole effort, the frozen archive is current — **no
fresh freeze is required**. The only data loss is battles that aged out of the API
during downtime (accepted).

---

## Stage 0 — Pre-flight

**Goal:** clean starting line.

- [ ] `feat/event-core-v5` is the intended code; working tree clean; tests green
      (`pytest tests/test_event_core_*.py` → 21 passed).
- [ ] `ruff check event_core/` clean.
- [ ] Confirm Elixir is down: `launchctl list | grep elixir` → absent.
- [ ] Confirm `elixir.db.legacy` integrity: `sqlite3 elixir.db.legacy 'PRAGMA integrity_check'` → ok.
- [ ] `eventsourcing` pinned in requirements (9.5.4).

**Verify:** all boxes checked. **If wrong:** fix before proceeding.

---

## Stage 1 — Memory DB split (`elixir-v5-memory.db`)

**Goal:** move memory/embeddings to their own file (Core Decision 6). Build it
because the FTS5 / sqlite-vec virtual tables can't be file-copied.

**Steps:**
- [ ] Create `elixir-v5-memory.db`; copy the plain memory content tables from
      `elixir.db.legacy` via `ATTACH` + `INSERT … SELECT`: `clan_memories`,
      `clan_memory_embeddings`, `clan_memory_versions`, `clan_memory_*` link/tag
      tables, `memory_episodes`, `memory_facts`, audit/index-status tables.
- [ ] **Recreate** the virtual tables fresh and **rebuild from content**:
      FTS5 `clan_memories_fts` via `INSERT INTO clan_memories_fts(clan_memories_fts) VALUES('rebuild')`;
      sqlite-vec `clan_memory_vec` by re-inserting vectors from
      `clan_memory_embeddings`.
- [ ] Point the memory subsystem at `ELIXIR_V5_MEMORY_DB` (new connection config).

**Verify:** row counts of content tables match legacy; an FTS query and a vector
similarity query return sane results; memory subsystem opens the new DB.
**If wrong:** drop `elixir-v5-memory.db`, fix the rebuild script, re-run.

---

## Stage 2 — v5 schema baseline (squash)

**Goal:** retire the 54-migration chain; `elixir-v5.db` starts from one baseline.

**Steps:**
- [ ] Author `0001_v5_baseline` = the projection schema (all `*_proj`, telemetry,
      tracking, ingest_cursor tables the projections create) **plus** the
      operational survivors that stay in `elixir-v5.db`: Discord plumbing
      (`discord_*`, `messages`, `channel_state`, `conversation_threads`),
      `llm_calls`, improvement/project tracking.
- [ ] Copy operational-survivor **data** from `elixir.db.legacy` (plain tables —
      `ATTACH` + `INSERT … SELECT`).
- [ ] Retire migrations `0`–`53` to git history; new `db/_migrations.py` starts at
      the v5 baseline. (Note: the library owns `elixir-v5-events.db` entirely — it
      is NOT in our migration system.)

**Verify:** `elixir-v5.db` opens; survivor tables present with expected row counts;
`PRAGMA user_version` reflects the new baseline.
**If wrong:** rebuild `elixir-v5.db` from the baseline + re-copy; fix DDL; re-run.

---

## Stage 3 — Full production build (populate the v5 stores)

**Goal:** build all three v5 DBs from the current archive and run the Mind +
reactive layers.

**Steps:**
- [ ] `python -m event_core.build_foundation` → Observed World + all projections
      (profile, roster, collections, clan metrics, war, roster lifecycle).
- [ ] Run detectors + leadership + communication policy + detections projection:
      `python -m event_core.mind.reactive_build` (it runs all of them).
- [ ] (Optional) `event_core.mind.leadership_build` for the leadership summary.

**Verify (the parity gate):** the build prints parity per slice — confirm
player_profile 53/53, roster mismatched=0, battle outcome_mismatch=0, collections
0 mismatch, war 1/1 + 593/593, clan metrics 0 *real* mismatch, roster lifecycle
100% precision. Detections + intents emitted in expected ranges.
**If wrong:** a parity regression here means a code bug — fix and rebuild (clean,
from the archive). This is the last gate before touching the runtime.

---

## Stage 4 — Live runtime wiring — ✅ BUILT (wire the seams at go-live)

The engine is built and tested offline in `event_core/live/` (commit on branch).
What remains at go-live is connecting the two seams to the running service.

Built + validated offline:
- ✅ **Live tick engine** (`live/engine.py`): `apply_payloads` routes fetched
  payloads through the same ingest functions backfill uses; `advance()` runs
  projections + detectors + leadership + policy **incrementally** (Followers resume
  from tracked positions — proven, not a rebuild per tick).
- ✅ **Discord intent consumer** (`live/discord_consumer.py`): follows
  CommunicationIntent → pluggable poster → fulfil/drop; idempotent (no double-post).
- ✅ **Cadence** (`live/cadence.py`): `clan_activity_24h` over projections.
- ✅ **Tick orchestrator** (`live/tick.py`): `run_tick(...)` (tested).

Seams to wire at go-live (the only remaining Stage-4 work):
- [ ] **`fetch_payloads`** → confirm cr_api wiring (`get_clan`, `get_player`,
      `get_player_battle_log`, `get_current_war`) and the member-tag list source.
- [ ] **Real Discord poster**: a `poster(intent)` that renders copy from
      `intent.summary` and posts to the channel for `intent.scope`, returning
      True on success. (The Event Core supplies facts; the poster owns copy.)
- [ ] **Agent reads**: point the agent's data access at `event_core.read.tools`.
- [ ] **Scheduler**: call `run_tick` on the heartbeat interval; cadence on its own.

**Verify:** live tick tests pass (4/4); at go-live, the Stage-5 rehearsal exercises
the wired seams once with Elixir still off.
**If wrong:** iterate on the branch; nothing is live until Stage 6.

---

## Stage 5 — Offline end-to-end rehearsal (Elixir still OFF)

**Goal:** prove the live wiring works against real API once, before going live.

**Steps:**
- [ ] With Elixir still off, run **one** manual live tick: fetch current CR API →
      live ingest → incremental projections/detectors/policy → confirm a
      `CommunicationIntent` would post (dry-run the Discord consumer, no actual post
      or to a test channel).
- [ ] Full suite green on the production-built stores.

**Verify:** the tick advances the notification log, projections update, a sensible
intent is produced, dry-run post renders. No errors in logs.
**If wrong:** fix and repeat. Do not proceed to Stage 6 until a clean dry-run.

---

## Stage 6 — Cutover (go live)

**Goal:** switch the running bot to the v5 core.

**Steps:**
- [ ] Point production config at the three v5 DBs (env: `ELIXIR_V5_EVENTS_DB`,
      `ELIXIR_V5_DB`, `ELIXIR_V5_MEMORY_DB`) and enable the v5 ingest/agent/Discord
      paths (config flags from Stage 4).
- [ ] Start Elixir: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.poapkings.elixir.plist`.
- [ ] The first live tick re-establishes current state from the CR API (the
      downtime battle gap is the accepted loss).

**Verify:** see Stage 7. **If wrong:** stop the service, fix, re-run from the
relevant stage (roll forward).

---

## Stage 7 — Post-cutover verification (watch the first hours)

**Goal:** confirm steady-state health on the live core.

- [ ] `launchctl list | grep elixir` shows a running PID; logs clean.
- [ ] Notification log `max_notification_id` advances each tick; follower tracking
      positions advance; projection lag near zero.
- [ ] Detections + communication intents are produced and posted to Discord
      correctly; scope respected (no leadership data in public posts).
- [ ] Spot-check a few players: `event_core.read.tools` output matches reality.
- [ ] Run for several clean cycles (days) before Stage 8.

**Verify:** no errors, sane posts, data current. **If wrong:** fix and roll forward.

---

## Stage 8 — Decommission legacy (after clean cycles)

**Goal:** remove the old system once the new one is trusted.

- [ ] Drop / stop writing legacy tables: `signal_log`, `signal_outcomes`,
      `game_event_stream`, the leader-action/decision side tables, old snapshot
      tables now superseded by projections.
- [ ] Remove legacy code paths (old `snapshot_*`, signal detectors, recompute-first
      recommendation scan, schedule-first awareness loop).
- [ ] Archive `elixir.db.legacy` off-box; remove the dual-read shims (there are
      none by design).

**Verify:** no production path references legacy; tests green; bot healthy.
**If wrong:** it's all in git — fix forward.

---

## Open items folded into cutover

- Deferred tails (do during/after as consumers need them): derived-table backfill
  for >2wk history; the two order-sensitive war projections; detector cooldowns as
  a CommunicationPolicy feature.
- Confirm leadership-scoped event payloads are handled per the sensitivity note
  (library SQLite store is unencrypted by default).
