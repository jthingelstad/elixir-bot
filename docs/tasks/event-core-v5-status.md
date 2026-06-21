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

## Validation results (vs frozen legacy)

| Slice | Result |
|---|---|
| **Player profile** (current-profile projection) | **53/53** reproducible members exact match; 0 mismatch; 4 correctly excluded (pre-archive) |
| **member_current_state** (roster) | **50 exact**, 0 true mismatch, **2 v5-more-current** (explained), 58 outside archive horizon |
| **Battle telemetry** | 6045 ingested, **5898 identity matches (97.6%)**; divergence classified below |
| **Replay determinism** | ✅ two from-zero rebuilds byte-identical |
| **Ingest idempotency** | ✅ re-ingest emits 0 events / 0 rows |

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

## NOT done (remaining work, roughly in dependency order)

- **Cutover** — deliberately not performed. Awaiting your go.
- **Clan & RiverRace aggregates** — only Player exists; war/season slices not built.
- **Granular milestone events** (`card_level_changed`, `badge_earned`, etc.) and
  the §5.6 churn-vs-durable field split — current Player emits coarse
  `ProfileObserved`/`RosterStateObserved`; the keystone proves the pipeline, not
  the final event taxonomy.
- **Full battle column parity** — needs porting `storage/game_modes.classify_battle_mode`
  and `_resolve_battle_outcome` for outcome/mode/2v2/boat identity.
- **Derived-table backfill** — only the ~2-week raw archive is replayed; deeper
  history (member_battle_facts, snapshots) for >2wk timelines is not yet backfilled.
- **Elixir's Mind** — Detection/Recommendation/DecisionCase aggregates, aggregators,
  the reactive communication-policy trigger, agent read-side tools.
- **Memory DB split** + the squash-to-v5-baseline migration for `elixir-v5.db`.

---

## Suggested next steps at check-in

1. Skim the parity results above; decide if the 2 roster + battle divergences are
   acceptable as classified.
2. If yes, the next high-value slice is the Clan/RiverRace aggregates (war), then
   the granular event taxonomy + full battle classifier for column-level parity.
3. Cutover stays gated until the slices you care about reach parity you trust.
