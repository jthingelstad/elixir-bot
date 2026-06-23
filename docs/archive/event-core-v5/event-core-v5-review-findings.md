# Event Core v5 — Pre-go-live code-scan findings & dispositions

Three parallel review agents scanned the rebuilt `event_core/` (domain, ingest,
backfill, migrate, parity, projections, mind, live, read). No SQL injection and no
scope leak were found; core event-sourcing discipline (pure mutators, deterministic
ids, no mutable-default/shared state) is sound. Real issues below, with what was
done.

## Fixed (this pass)

| # | Issue | Fix |
|---|---|---|
| 1 | **Battle telemetry NULL-in-PK duplication** — NULL opponent_tag/crowns (boat/PvE) made `INSERT OR IGNORE` re-insert every poll (SQLite treats NULLs in a PK as distinct). | `extract_battles` coalesces identity cols to non-NULL sentinels (`battle_type→"unknown"`, `opponent_tag→""`, crowns→`-1`). Test: dedups on repeat. |
| 2 | **One bad event aborts the whole tick** — projection/follower loops had no per-event isolation. | `ProjectionRunner.run` + `FollowerRunner.run` wrap decode+dispatch in try/except, log + skip, advance position. |
| 3 | **Delivery loss** — IntentConsumer marked intents fulfilled before the actual send, and dropped on any (transient) failure → permanent post loss. | Rewrote IntentConsumer for **at-least-once**: fulfil only after a confirmed send; on failure leave `raised` and stop without advancing (retry next tick). Replaced `CollectingPoster` (fulfil-before-send) with a synchronous `make_agent_poster(send)`. |
| 4 | **Milestone post-burst** — a first/zeroed observation (`old<=0`) backfilled every milestone → dozens of posts for a new member. | `_milestones` returns `[]` when `old` is missing/≤0. |
| 5 | **Scope routing single-layer** — unknown scope defaulted public. | `route_intent` is now **fail-closed**: only explicit `public` reaches the public channel; leadership/unknown → leader-actions. |
| 6 | **Recommendation.observe_outcome** had no guard (could overwrite). | Guard: rejects a second outcome. |
| 7 | **Battle-detector nondeterminism** — dedup keyed on `(tag, battle_time)` (collides on same-second battles) + non-total ORDER BY. | Total order (matches PK) + dedup key includes `battle_type:opponent_tag`. |
| 8 | **RiverRace `None` season → TypeError** in season-inference tuple compare; NULL projection rows. | Aggregate coerces `season_id`/`section_index` to the same sentinels as the id (stored == id). |
| 9 | `suppression_reason`/`drop_reason` AttributeError on read; silent agent-compose fallback. | Initialized to `None`; compose fallback now logs. |

## Deferred (tracked, with rationale)

- **RiverRace live-state identity can split across ingest ordering** (live `currentriverrace` keyed on a *store-inferred* season; before the war log is ingested it lands on the `-1` sentinel, then the real season later). The crash (#8) is fixed and `war_current_state` parity is 1/1; the deeper fix is to key live state on `(clan_tag, section_index)` only (section is always present live). **Do with the deferred war_day/period projections** — same area, needs its own validation.
- **Battle detectors full-table rescan each tick** (latency grows with history). Correctness is fine (idempotent). Bound the scan to a recent window before sustained multi-month operation. Not a go-live blocker.
- **CommunicationPolicy decodes every event** (`aggregate_name=None`) — pre-filter by topic for perf. Steady-state per-tick volume is small (resumes from position); matters mainly during replay.
- **Backfill cursor is cross-DB / saved once at end** — a clean rebuild resets it and content-hash dedup covers re-feeds; recovery is re-run `build_all` (roll-forward). Don't run backfill against the live DB post-cutover (the live path doesn't advance the cursor).
- **migrate build failure leaves a partial DB** — recovered by re-running `build_all` (idempotent; the runbook's roll-forward philosophy).
- Minor: LogStanding participant fold doesn't remove departed participants; card/badge diff silently skips dup/missing-id entries; RosterLifecycle UNIQUE keyed on tick `observed_at`; `configure_eventstore_env`/`war_validate.build` mutate global env (test/tool footgun).
