# Elixir v5.1 — Migration & Cutover

> **Status:** Complete historical cutover record (T14 calendar ledger seed added
> 2026-07-03; feedback.md rev 4).
> **Owner:** Jamie · **Last reviewed:** 2026-07-15
>
> The phased build and cutover: archive, carry-forward transforms, teardown,
> seeding, parity checks, acceptance criteria. Licensed by §6 (clean break,
> downtime fine, no compatibility layer); disciplined by §14.4 (precious history
> is defined explicitly, carried deliberately, backed by an immutable archive).

## Phase 0 — Pre-cut prep (historical plan)

- Create the tracking issue + child issues per arc (AGENTS.md work-tracking
  convention; label the arc `v5.1`).
- Fix C4 now (one line in AGENTS.md: `leadership-action-scan` is enabled, not
  disabled) so the reference docs are honest going into the cut.
- Write and commit the **parity queries** (Phase 6) against the *current* DB
  first — they must produce the expected numbers on the old schema before they
  can validate the new one.
- Offline rehearsal harness: replay the raw-payload window (14 days,
  `raw_api_payloads`) through the new emitters into a throwaway DB (precedent:
  the offline-rehearsal seam noted in `event_core/live/tick.py:25–28`). This
  exercises emit → project → recognize → ledger without Discord.

## Phase 1 — Freeze & archive

1. Stop the bot (launchd unload; downtime begins).
2. Copy `elixir-v5.db` → **`elixir-v5-archive-2026H2.db`**, set read-only. This
   is the permanent cold archive (§14.4) — never written again.
3. Sweep the repo dir of DB sediment in the same pass: `elixir.db`,
   `elixir.db.bak-20260620`, `elixir.db.legacy`,
   `elixir.db.legacy-v2-backup-*`, `elixir-v5.db.premerge`,
   `.migration-rollback/` → move into a single archive directory
   outside the working tree.
   **Corrected at execution (2026-07-03; the 07-02 scan misclassified two
   files):** `elixir-v5-memory.db` is **live, not sediment** — it is the
   authoritative `clan_memories` store (`memory_store/__init__.py`,
   `ELIXIR_V5_MEMORY_DB`; the main DB's copy is stale). It stays in place; the
   deferred memory pass owns it (see the T12 correction). `elixir-v5-events.db`
   is the Gen C eventsourcing write store — live until the cut; it and
   `elixir-v5.db` stay in the working dir untouched until Phase 7 go-live
   succeeds (fastest rollback: flip `.env`, reload plist), then retire at
   Phase 9. The working dir after close-out contains the live DB, the memory
   DB, and the archive.

**Rollback plan (whole migration):** the old code is a git ref; the old data is
the archive. Restore = check out pre-cut ref, copy archive back to
`elixir-v5.db`, launchd load. Cheap, total, tested by doing a dry-run restore
once before Phase 4 deletes code.

## Phase 2 — New schema

Fresh `elixir-v51.db` created from `schema.md` DDL as `_migration_0` (the repo's
baseline-schema convention, AGENTS.md). For tables schema.md marks "carried
as-is / verified live DDL" (leader actions, revisits, the
tournaments star, `raw_api_payloads`, `runtime_job_status`, the ops
singletons), the DDL source is an **export from the archive**
(`sqlite3 archive .schema <table>`), applied with only the renames schema.md
§7.3 lists — schema.md deliberately reproduces DDL only for new or changed
tables. `ELIXIR_DB_PATH` points at the new file. The old file is never migrated
in place — transforms read the archive, write the new DB.

**Post-cut evolution (2026-07-14 amendment):** migration 0 remains the clean
break. Compatible v5.1 databases then advance through the ordered migrations in
`db/schema.py`, keyed by `PRAGMA user_version`; pre-v5.1 databases are still
refused. Runtime modules never lazily `CREATE` or `ALTER`. Fresh builds run the
same forward path and are locked by a committed schema fingerprint test.
The initial cutover also carried `decision_cases`; #216 retired that duplicate
decision store and schema v21 removed it plus the nullable leader-action link.

## Phase 3 — Durable carry-forward (transform, not copy — feedback New-6)

The cutover transforms were one-time, idempotent scripts and now live only in
Git history. `member_id → player_tag` resolution
joins through the archive's `members` table.

| # | Target | Source | Transform notes |
|---|---|---|---|
| T1 | `players`, `player_aliases` | `members`, `member_aliases` | drop `member_id`, `status`; tag becomes PK |
| T2 | `player_metadata` | `member_metadata` | drop `poap_address` (Q4); keep cr_* enrichment |
| T3 | `clans` | distinct clan tags from `clan_daily_metrics` + **`war_period_clan_status`** (the full opponent-tag history T9 needs — the raw-payload archive is only a 14-day window and would under-seed the FK) | `is_home` for `#J2RGCRVG`. **Ordering: T3 runs before T9** so every `war_week_clans.clan_tag` has its parent row |
| T4 | `discord_users`, `discord_links` | same | links re-keyed to `player_tag`; drop duplicate name columns |
| T5 | `clan_memberships` | same | re-key; add `clan_tag` |
| T6 | `awards` | `awards` | re-key; **seed one `free_pass` row per season from the rank-1 `war_champ` row** (Q2/C5: historically champ = recipient; `war_champ` is a podium — ranks 1–3 — so seeding every row would mint three passes/season); drop deprecated-type rows (already deleted live, verify zero) |
| T7 | `player_daily_metrics`, `player_daily_battle_rollups` | `member_daily_metrics`, `member_daily_battle_rollups` | re-key only |
| T8 | `clan_daily_metrics`, `clan_daily_battle_rollups` | same | drop `raw_json` column |
| T9 | `war_seasons`, `war_weeks`, `war_week_clans`, `war_participation` | `war_races`, `war_participation`, `war_period_clan_status` | seasons synthesized per distinct `season_id` (rank/weeks from its `war_races` rows; `war_champ_tag`/`free_pass_tag` from T6); participation re-keyed to `(season, section, tag)`. `war_attendance_days` starts **empty** — per-day history isn't reliably reconstructable; the evaluators tolerate a warm-up season |
| T10 | tournaments star | same | drop `player*_member_id` columns |
| T11 | `leader_action_recommendations`, `decision_cases`, `revisits` | same | rename `source_signal_*` → `source_event_*`, `signal_key` → `revisit_key`; keep full feedback history (C1 needs the kick records) |
| T12 | conversation set (`conversation_threads`, `messages`, `memory_facts`, `memory_episodes`) | same | **verbatim copy** (deferred pass owns redesign). **Corrected at execution:** `clan_memories` + satellites + FTS/vec do **not** transform — they live in the separate `elixir-v5-memory.db` (the main DB's copy is stale by 23 rows) and that file simply stays in place; the memory tools keep reading it through the `memory_store` seam. `clan_memory_event_links.event_id` dangling refs remain the memory pass's to reconcile |
| T13 | ops singletons (`llm_calls`, `prompt_failures`, `prompt_feedback`, `system_signals`, `api_sentinel_observations`, `arena_relay_screenshot_observations`, `discord_channels`, `channel_state`, `game_mode_contexts`, `card_catalog`, `elixir_improvement_suggestions`, `runtime_job_status`) | same | verbatim copy (schema.md §2's ops singletons plus `runtime_job_status`, which §2 groups under engine control). `cake_day_announcements` is **not** carried — it is empty with a 7-day purge and its dedup role belongs to the ledger (schema.md §2 note); T14 owns cross-cut calendar protection |
| T14 | `recognition_ledger` (calendar seed rows only) | archive `detections` | insert a ledger claim for every archived detection of type `member_birthday`, `clan_birthday`, `join_anniversary`, `weekly_donation_leader` with `occurred_at` in the trailing **14 days** before the cut. Old dedup keys are format-identical to the new event keys (events.md §4/§6: "unchanged names"), so keys copy verbatim; `stream='clan'`, `event_refs_json=[archived dedup_key]`, `score=0` (seed, not scored), `claimed_at=occurred_at`, `intent_id=NULL`. Blocks day-of-cut birthday/anniversary/donation-leader re-posts — the one moment class whose dedup key can recur across the cut (milestones are protected by first-sight baselines instead) |

**Not carried (fresh):** all four event streams, `state_baselines`,
`communication_intents`, `stream_cursors`, `poll_state`,
`player_current_state`, `player_card_collection`, `player_recent_form`,
`member_management`; `recognition_ledger` starts fresh **except** the T14
calendar seed. **Why a near-empty ledger is safe:** streams start empty and
baselines seed silently (first-sight, §8), so no historical *milestone* can
re-enter recognition. Calendar moments are the exception — their dedup keys
embed the date, so a birthday already posted on cut day would re-post from a
truly empty ledger; T14's seed claims exactly that window.

`raw_api_payloads` is also **fresh** (DDL identical to the archive's; data not
copied — it is a 14-day forward-only buffer and the archive keeps the old
window; the Phase 0 rehearsal harness reads the *archive's* buffer directly).

**Accepted warm-up (Jamie, 2026-07-03):** `member_management` starts fresh, so
every candidacy state opens at `none` and the hysteresis counters at zero — no
promotion/demotion recommendations for the first N qualifying weeks after the
cut (kick-risk resumes as soon as the inactivity windows fill, ~1–2 weeks).
Same class of concession as `war_attendance_days`' warm-up season (T9);
leadership promotes manually in the interim, exactly as today. Deliberately
**not** seeded from the archive — approximated hysteresis would make the first
post-cut recommendations rest on reconstructed state.

## Phase 4 — Teardown (the clean break, §6)

Delete in one arc, same as the v4 teardown precedent:

- **Gen A/B/C engine code:** `event_core/` (the `eventsourcing` framework, mind/,
  live/, domain/), `heartbeat/` detectors and `heartbeat/_awards.py` (Q5 moves
  grants into the engine), Gen B storage (`storage/communication_intents.py`,
  `storage/decision_cases.py` Gen-B halves, `storage/signal*`,
  `storage/war_ingest.py`, snapshot stores per `schema.md` §8),
  `storage/clan_voyages.py` + voyage tool defs/exec (C6).
- **Dependencies:** drop `eventsourcing` from `pyproject.toml` and regenerate
  `uv.lock`.
- **C3:** delete the `_RAW_PAYLOAD_ENDPOINT_LABELS` alias (`cr_api.py:23–25`);
  riverracelog payloads stored under their true name from the first new poll.
- New engine lands as `engine/` (streams, emitters, recognizers, scheduler,
  delivery) behind the existing facade discipline: `elixir_agent.py` and
  `runtime.app` remain the public surfaces (AGENTS.md).
- Activity registry updated per `runtime.md` §3 (retire `award-detection`,
  `war-poll`, `v5-reactive-tick`, `player-progression`, `leadership-action-scan`;
  add `engine-tick`, `weekly-leadership-review`, `war-attendance-snapshot`,
  `action-outcome-refresh`).

## Phase 5 — Read-layer port (§17.1 / §14.5)

Repoint the ~30 query tools per the `schema.md` §9 coverage matrix — the matrix
*is* the checklist; port row by row. Same pass: slash commands, content jobs
(`weekly-recap`, `promotion-content`, `daily-clan-insight` read paths),
`scripts/review_agent_feedback.py`, and the `#leader-actions` screenshot readout
(kept per Q6). Remove `get_clan_voyage` from tool defs/exec/prompts.

Same pass, `CLAN.md` updates (it is the live prompt, so these land with the
engine that reads them): add the ratified `management.md` §5 constants to the
Thresholds section, and apply C5's Free-Pass-rotation prose fix (plus the Q4
POAP present-tense wording note).

## Phase 6 — Parity checks (archive vs new DB)

Run before go-live; every check is a script with an expected-vs-actual print.

| Check | Rule |
|---|---|
| Identity | `COUNT(players)` = archive `COUNT(members)`; every open membership's tag resolves; `resolve_member` succeeds for all current roster names |
| Links | `COUNT(discord_links)` preserved; confidence values byte-identical |
| Tenure | per-tag `(joined_at, left_at)` spans identical |
| Awards | per `(award_type, season_id)` counts identical **plus** one `free_pass` row per archived **rank-1** `war_champ` row (4 at Phase-0 validation — seasons 129–132) |
| War history | per season: final rank + weeks in `war_seasons` match the archive's `war_races`; distinct-season count = the archive's `war_races` count (**5** at Phase-0 validation — the war tables purge at 180 d, so only recent seasons have race detail; the 12 award seasons stay awards-only, which is correct: the rotation seed reads `awards`, not `war_seasons`) |
| Rollups | sampled sums (10 random players × donations/trophies/battle counts per month) identical |
| Q&A smoke | every tool × aspect in the coverage matrix returns non-error on live data; leadership-gated aspects still refuse public workflows (AGENTS.md tool policy) |
| Calendar seed | every archived calendar/donation-leader detection in the trailing 14 days has a ledger claim under the identical key (T14) |
| Recognition rehearsal | Phase 0 replay produces **zero duplicate ledger claims** and no first-sight flood |

## Phase 7 — Go-live & bake

1. Seed poll: first `engine-tick` populates baselines silently (§8), roster
   read models fill, `poll_state` seeds warm.
2. Watch one full **war week** with thresholds live: arena-ups post once,
   `#leader-actions` reactive kick-risk works, weekly review posts Monday.
3. First **season close** after go-live: `season_closed` fires, War Champ +
   free-pass rotation computes (Q2), awards land, recap posts. This is the last
   acceptance gate — the season boundary can't be rehearsed offline honestly.

## Phase 8 — Tests (§17.7: ~2,100 lines / 49 files invalidated)

Delete with their subjects; rewrite against the new engine:

- **Emitters:** golden-pair tests — (previous_state, observed_state) → exact
  event list, including first-sight-emits-nothing and milestone-ladder edges.
- **Scorer parity:** the ported constants replayed against synthetic candidate
  sets copied from the old tests' scenarios — same post/suppress decisions as
  `communication.py` produced (this is the port's regression net).
- **Ledger:** two streams claim one key — exactly one intent.
- **Delivery:** fail-stop-retry ordering; 6 h expiry; fulfil-only-after-send.
- **Clock:** Colosseum detection, 4-vs-5-week seasons, phase gating.
- **Management:** hysteresis (one good week ≠ eligible; sustained slippage to
  lose it), kick-suppression (C1), auto-withdraw.
- **Coverage matrix smoke:** one test per tool aspect against a seeded fixture DB.
- Conventions carried: `uv run pytest`, in-memory SQLite, fixtures own
  connection lifecycle (AGENTS.md).

## Phase 9 — Docs & close-out

- Rewrite AGENTS.md architecture sections (streams/emitter/recognition/engine
  tick), activity list pointer, DB section (new table spine), and fix §17.8
  website-publishing drift in the same pass.
- Move `docs/v5.1/` → `docs/reference/` per the docs-lifecycle convention
  (`docs/README.md`): the build spec becomes the stable-system reference;
  `open-questions.md` stays as the decision record.
- Close the GitHub issue arc.

## Acceptance criteria (the definition of done)

1. All Phase 6 parity checks pass.
2. One war week + one season close observed live with correct behavior (Phase 7).
3. Zero double-posts over the bake period (ledger holds; the §10 bug class is
   dead).
4. `sqlite_master` designed-table count ≤ 49 engine tables + carried memory set;
   **none of the 33 names in `schema.md` §8 (dropped or transformed) exists** in
   the live DB.
5. `grep -r member_id --include='*.py'` (non-test, non-archive) returns nothing.
6. Every AGENTS.md-listed query tool answers on live data; `get_clan_voyage` is
   gone from defs, exec, prompts, and docs.
7. Test suite green; `eventsourcing` absent from the venv.
8. AGENTS.md describes the running system (C4 and §17.8 included).
