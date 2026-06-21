# v5 Autonomous Session Log — 2026-06-21 (PM)

Jamie away ~3h; approved autonomous finalization of the v5 architecture shift,
run in production, keep this record for review. Scope: finish retiring the unread
current-state projections, then a v4→v5 feature-gap comparison (excluding POAP
KINGS website publishing, which is being removed).

Running tally of what I did, why, and anything to review.

---

## 1. Retire the unread current-state projections (in progress)

**Why:** They were written every reactive tick but had zero live readers
(confirmed: nothing outside `event_core/` reads them; `event_core/read/tools.py`
has no consumers; the live agent reads v4 `storage/*`). Pure dual-write overhead.
See `event-core-v5-architecture-boundary.md`.

**Retired from the live tick** (`event_core/live/engine.advance`): the 7 World
projections — `player_current_profile`, `member_current_state_proj`,
`player_current_collections`, `clan_daily_metrics_proj` (+ `_clan_agg_tag` helper),
`war_current_state_proj`, `war_participation_proj`, `roster_lifecycle`.

**Kept** (consumed): `detections` (CohortWaveDetector), `battle_telemetry` (battle
detectors), `projection_tracking`/`ingest_cursor` (infra), and the whole Mind
(detectors, leadership, policy).

**Kept as offline harness:** the projection modules + `build_foundation` + parity
checks still build/validate these in isolated temp DBs (the foundation parity test),
so we retain the v5↔legacy parity validation capability without the live cost.

**Safety checks done:** grep across `event_core/` confirmed no Mind/live reference
to the 7 tables; event_core live/reactive/mind tests pass (17).

TODO in this step: drop the 7 tables from live `elixir-v5.db`, redeploy, verify.

---

## 2. Feature-gap comparison v4 → v5 (pending)
(to be filled in)

---

## Review notes / decisions / risks
- (none yet beyond the above)
