# Event Core v5 — Build Status (autonomous session, 2026-06-21)

Branch: **`feat/event-core-v5`** (built on the design branch
`docs/event-core-v5-architecture`; carries the full design doc + implementation).

Scope of this session: build the **validated foundation** of the v5 event-sourcing
core, with Elixir stopped, up to but **NOT including production cutover** (per your
three decisions: build-and-validate-stop / full latitude / validated-foundation-deep).

---

## ⚠️ Production state — Elixir is OFF

I stopped the launchd service `com.poapkings.elixir` (it had `KeepAlive=true`, so
a plain kill would have respawned it — used `launchctl bootout`). It is **still
stopped** and will stay down until you decide.

- **To resume Elixir on the OLD system** (legacy `elixir.db`, no v5):
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.poapkings.elixir.plist`
  Note: doing so resumes live data collection, after which `elixir.db` diverges
  from the frozen `elixir.db.legacy`. For a clean cutover later you'd re-freeze.
- **The frozen oracle** `elixir.db.legacy` (769 MB, integrity-checked) is untouched
  and is the backfill source + parity reference. The live `elixir.db` is also
  untouched.

---

## What was built

A bounded `event_core/` package on the `eventsourcing` library (9.5.4, native
sqlite persistence; works on Python 3.14). Three databases, all v5-named:
`elixir-v5-events.db` (event store), `elixir-v5.db` (projections + telemetry),
`elixir-v5-memory.db` (reserved, not used by the foundation).

```
event_core/
  config.py            three v5 DB paths + eventstore env
  domain/player.py     Player aggregate (profile + roster observations)
  application.py       ObservedWorld app (idempotent get-or-create, snapshotting)
  db.py                projection DB helper (tracking + ingest cursor)
  ingest/
    profile.py         /players  -> ProfileObserved
    roster.py          /clans memberList -> RosterStateObserved
    battles.py         /players battlelog -> battle_telemetry (tier 1)
  backfill.py          replay raw_api_payloads through the ingest path
  projections/
    runner.py          notification-log follower, co-located atomic tracking
    player_state.py    player_current_profile projection
    member_state.py    member_current_state projection
  parity.py            deterministic comparison vs frozen legacy
  build_foundation.py  end-to-end driver (from-zero rebuild == replay harness)
tests/test_event_core_foundation.py   8 tests, all passing
```

Run it: `./venv/bin/python -m event_core.build_foundation`
Test it: `./venv/bin/python -m pytest tests/test_event_core_foundation.py -q`

---

## Phase 1 (Observed World) — COMPLETE as of this update

The entire Observed World is now modeled and parity-proven against the frozen
legacy DB, across six slices in one unified event store + projection DB. The last
three (war / collections / clan) were built in parallel via subagent fanout and
integrated. Aggregates: Player, PlayerCollections, Clan, RiverRace. Battles remain
telemetry (not event-sourced).

## Validation results (vs frozen legacy)

| Slice | Result |
|---|---|
| **Player profile** | **53/53** exact; 4 excluded (pre-archive) |
| **member_current_state** (roster) | **50 exact**, 0 true mismatch, 2 v5-more-current (explained) |
| **Battle telemetry** | 5898 matched identities, **outcome_mismatch=0** (deterministic field exact); 499 classification-drift (legacy historical classify logic); 147 only-in-projection (pre-tracking history); 2428 only-in-legacy (rolling-window loss) |
| **Collections** (cards/badges/achievements) | **53/53 exact** for all three |
| **Clan daily metrics** | 8 matched, 7 explained (3 legacy persisted a broken API response → v5 more correct; 4 last-observation-of-day timing); joins/leaves deferred |
| **War current state** | **1/1 exact** |
| **War participation** | **593/593 exact** (254 outside the 2-archived-log horizon) |
| **Replay determinism** | ✅ from-zero rebuilds byte-identical |
| **Ingest idempotency** | ✅ re-ingest across all slices emits 0 events / 0 rows |

Tests: 8/8 pass. Ruff clean. Build ~22s; full suite ~2min (builds twice).

Integration note: event class names (e.g. `Registered`) collide across aggregates
in the shared notification log; `ProjectionRunner` now filters by aggregate.

Documented deferrals within Phase 1 (data is in the event stream; only the
order-sensitive projection logic remains): war_day_status / war_period_clan_status
(season-inference replay), clan joins_today/leaves_today (roster lifecycle).

The three architectural guarantees the design rests on — **replay determinism,
idempotent ingest, co-located atomic projection tracking** — are all proven on
real archived data. The full pipeline works end to end:
aggregate → event store → notification log → follower → projection → exact parity.

### Divergences found and classified (the "this is the work" part of §12)

1. **roster: 2 `v5_more_current`.** Legacy `member_current_state` is updated only
   by the heartbeat's `snapshot_members`; the archive contains clan fetches from
   other paths (and one after the final heartbeat). Backfill consumes all of them,
   so two members show a *later real observation* than legacy recorded. The v5
   behavior is more correct, not wrong.
2. **battle `only_in_legacy` (2428).** The battlelog API is a rolling ~35-battle
   window; battles played between archived fetches never reached
   `raw_api_payloads`, so backfill can't recover them — exactly the archive-bound
   loss §5.3/§10 predicted. Legacy accumulated them live.
3. **battle `only_in_projection` (147).** Concentrated in special types
   (boatBattle 55, plus window-boundary PvP/trail) whose identity convention
   differs from the legacy `_classify_battle` logic. Known follow-up (see below).

---

## Decisions made autonomously (flag any you'd reverse)

- **No SQLAlchemy** (you asked). Native `eventsourcing.sqlite` + stdlib sqlite3
  for projections, behind the single `event_core/db.py` helper so SQLAlchemy Core
  can be confined there later if ever justified. Rationale in `config.py`.
- **Roster-stat observations attach to the Player aggregate** (not Clan), so a
  player's profile + roster share one timeline and writes distribute across
  aggregates. Clan-level membership lifecycle (join/left/role-as-event) remains a
  separate Clan concern — not built yet.
- **Battles are telemetry, not events** — written straight to `battle_telemetry`,
  proving the §5.3 tier split.
- **Backfill idempotency via an ingest cursor** (high-water on
  `raw_api_payloads.payload_id`), because content-hash dedup alone is not
  replay-idempotent (a real bug the test caught — see commit history).

---

## Phase 2 (Elixir's Mind) — substantially COMPLETE

Mechanism, detector breadth, and the leadership decision layer are all built and
validated. Remaining: detector cooldowns and a few long-tail signal types; the
reactive trigger + agent tools belong to Phase 3.

**Detectors (8), validated by date-overlap vs legacy `signal_log`:**

| detector | emitted | overlap / legacy dates |
|---|---|---|
| best_trophies_peak | 115 | 10 / 19 |
| battle_hot_streak | 234 | 22 / 28 |
| battle_trophy_push | 150 | 17 / 26 |
| card_level_milestone | 52 | 12 / 26 |
| new_card_unlocked / champion | 21 | 9 / 19, 5 / 10 |
| badge_earned | 75 | 3 / 7 |
| player_level_up | 0 | (no level-ups in 2wk window) |
| inactive_member_risk | 5 | (feeds leadership, below) |

Both detector shapes proven: **log-following** (consume base events) and
**telemetry-scanning** (battle_telemetry). Divergences dominated by the archive
horizon (legacy signals run to 2026-05-22; archive starts 06-07) + un-ported
cooldowns + legacy's mastery-badge exclusion.

**Leadership decision layer (the high-stakes part):**
- `Recommendation` + `DecisionCase` aggregates — command-driven state machines
  with real invariants (terminal states reject transitions), evidence + policy
  version + scope='leadership'.
- Pipeline proven end to end: roster `lastSeen` → `inactive_member_risk` detection
  → `kick` Recommendation + `inactivity_review` DecisionCase, idempotent.
- Validation vs legacy: **5 flagged, 5/5 overlap (100% precision — no false
  positives)**; recall 5/13 because the other 8 are pre-archive or below the 7-day
  threshold (legacy's recompute-first policy used broader history; §6 replaces it,
  so this is structural validation, not row-for-row).

### Mechanism (for reference)
- **Granular base events** emitted from `observe_profile`/collection diffs — the
  Mind's contract. Coarse observation events retained so current-state parity is
  untouched.
- **Detection / Recommendation / DecisionCase** aggregates: deterministic ids
  (idempotent emission), evidence links, UTC, scope.
- **FollowerRunner**: reads the notification log forward with co-located tracking,
  emits via the shared app, snapshots `max_notification_id` so it never consumes
  its own output, filters by aggregate. `signal_log` is date-level only, so
  validation is date-overlap, not row parity.

**Remaining in Phase 2:** Recommendation + DecisionCase aggregates (the leadership
decision state machines; parity vs leader_action_recommendations/decision_cases);
detector breadth (card/badge milestones, trophy-push, ranked promotion, inactivity,
cohort waves); detector cooldowns. Then Phase 3 (reactive trigger + agent tools +
runtime rewire) and Phase 4 (cutover).

## NOT done (remaining work, roughly in dependency order)

- **Cutover** — deliberately not performed. Awaiting your go.
- **Phase 1 deferrals** (low risk): derived-table backfill for >2wk history; the
  two order-sensitive war projections; clan joins/leaves. Best done when their
  consumers exist so we know the needed grain.
- **Granular milestone event taxonomy** + the §5.6 churn-vs-durable split — current
  aggregates emit coarse observation events. The Mind layer needs granular base
  events as its contract; define them at the start of Phase 2.
- **Phase 2 — Elixir's Mind** — Detection / Recommendation / DecisionCase
  aggregates, aggregators (Followers), the reactive communication-policy trigger,
  agent read-side tools.
  - ⚠ **Validation finding:** legacy `signal_log` is thin — `(signal_date,
    signal_type)` only, date-level dedup, no per-event evidence. It cannot serve
    as a rich parity oracle. Phase 2 detections will be richer than legacy signals;
    validate via detection presence/timing vs signal_log dates + re-derivation from
    the (more complete) event log, not row-for-row parity.
- **Phase 3 — runtime pivot**: heartbeat→ingest path, reactive loop, agent reads,
  Discord via communication intents.
- **Phase 4 — cutover**: memory-DB split, squash v5 baseline, fresh freeze, full
  backfill, decommission legacy.

---

## Suggested next steps at check-in

1. Skim the parity results above; decide if the 2 roster + battle divergences are
   acceptable as classified.
2. If yes, the next high-value slice is the Clan/RiverRace aggregates (war), then
   the granular event taxonomy + full battle classifier for column-level parity.
3. Cutover stays gated until the slices you care about reach parity you trust.
