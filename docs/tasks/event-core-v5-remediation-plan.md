# Event Core v5 Remediation Plan

Status: Plan. Tracking issue: #101.

Captured: 2026-06-22.

## Goal

Stabilize the Event Core v5 landing without broadening the rewrite. The target
state is:

- v5 startup cannot silently skip new reactive events after restart
- the hand-built `game_event_stream` is retired if all live readers can be moved
  to v5 sources
- Event Core owns domain observations, detections, recommendations, and cases
- presentation routing and outbound side effects live outside Event Core
- health metrics report real deliverable backlog, not historical drained events

This is not a request to rebuild the architecture. It is cleanup after the
successful v5 cutover.

## Current Assessment

The v5 core is working:

- `event_core/live/engine.py` has a clear tick shape: ingest payloads, advance
  followers, generate detections/recommendations/intents, then consume work.
- `event_core/mind/follower.py` gives detectors deterministic IDs, position
  tracking, evidence links, and a max-notification snapshot so followers do not
  consume their own outputs.
- `event_core/live/discord_consumer.py` only fulfills after a confirmed send,
  which is the right at-least-once delivery posture.
- Targeted Event Core tests pass, and the full test suite passes.

The cleanup issues are boundary and operations issues, not evidence that the
core event-sourcing model failed.

## Finding 1: Startup Catch-Up Runs Every `on_ready`

`runtime/app.py` runs `event_core.live.service.catch_up()` inside `on_ready`.
`catch_up()` is documented as a one-time go-live drain, but the bot can reconnect
or restart many times after go-live.

Observed effect:

- repeated `v5 go-live catch-up` log entries on 2026-06-22
- health shows many `CommunicationIntent.Raised` events but far fewer
  `Fulfilled` events
- `consumer:discord` is at the event-log head, so those raised intents are not
  actually pending for delivery

Risk:

Any event raised during a restart window can be fast-forwarded without posting.
That is silent loss.

### Fix Plan

1. Add an explicit durable cutover marker.
   - Store it in `projection_tracking`, a new small `v5_runtime_state` table, or
     an existing runtime status table.
   - Marker should include `catch_up_completed_at`, event-log position, and build
     hash if available.
2. Change startup behavior.
   - `on_ready` must not call catch-up unless the marker is absent and an explicit
     environment/config flag allows initial go-live drain.
   - Normal restart should run no drain. It should let the consumer resume from
     its tracked position.
3. Make drained work auditable.
   - Prefer marking drained communication work as `Dropped` with reason
     `cutover_drain` or `startup_drain`.
   - If we avoid mutating historical aggregates, health must still distinguish
     drained-before-position from deliverable pending.
4. Add tests.
   - startup calls catch-up only once
   - restart after marker does not fast-forward `consumer:discord`
   - failed poster still retries
   - health pending count excludes drained historical work

Verification:

- restart Elixir twice
- confirm no new `v5 go-live catch-up` on second startup
- confirm `consumer:discord` does not jump to head without either fulfilling or
  explicitly dropping work
- full suite

## Finding 2: `game_event_stream` Is Not Unused Yet

The hand-built `game_event_stream` is probably removable, but it is not currently
unused.

Live or likely-live readers:

- `agent/tool_exec.py`
  - `get_elixir_state(event_summary)`
  - `get_elixir_state(recent_events)`
  - `get_elixir_state(game_modes)`
  - `get_elixir_state(operational_summary)`
- `runtime/jobs/_memory.py`
  - memory synthesis includes event windows, recent events, and game-mode pulse
- `runtime/helpers/_reports.py`
  - weekly recap context includes event windows, recent events, and mode pulse
- `runtime/jobs/_core.py`
  - daily insight context includes public event windows and recent events
- `runtime/situation.py`
  - old awareness Situation still reads event windows, recent events, and mode
    pulse, even if old awareness jobs are disabled
- `runtime/signals/context.py`
  - member signal context reads mode pulse
- `storage/metadata.py`
  - maintenance pruning calls `prune_event_stream_with_rollups`
- `storage/communication_intents.py` and `storage/decision_cases.py`
  - old trace/link helpers look up `game_event_stream.event_key`

Write paths still exist:

- `runtime/signals/delivery.py`
  - records old signal batches before v4 delivery
- `runtime/signals/system.py`
  - records old system signals
- `storage/player.py`
  - records battle-grain rows while snapshotting player battle logs
- `scripts/backfill_battle_events.py`
  - backfills old battle events

This means an immediate table drop would break tools, reports, memory synthesis,
maintenance, tests, and possibly remaining v4 compatibility paths.

### Replacement Sources

Replace `game_event_stream` concepts with v5-native sources:

| Old use | Replacement |
|---|---|
| recent signal events | `detections` projection |
| event windows | aggregate `detections` by type/scope/time |
| per-player recent event history | `detections` projection filtered by subject |
| game-mode pulse | `battle_telemetry` |
| battle-grain stream rows | `battle_telemetry` |
| leadership recommendation history | `Recommendation` and `DecisionCase` events or projections |
| communication trace | v5 evidence links plus downstream outbox/delivery ledger |
| old rollups | either retire or rebuild from v5 projections if still needed |

### Retirement Plan

Phase A: inventory and classify every reference.

- Mark each reference as one of:
  - live reader to replace
  - disabled v4 awareness residue to delete
  - compatibility-only historical query
  - test-only fixture
  - schema/migration history

Phase B: add v5 read facades.

- Add a small read module that answers the old high-level questions without
  touching `game_event_stream`:
  - recent detections
  - detection windows
  - player detection history
  - game-mode pulse from `battle_telemetry`
  - leadership recommendation/case summary
- Keep return shapes close enough that callers can migrate one at a time.

Phase C: migrate live readers.

- `agent/tool_exec.py`
- memory synthesis
- weekly recap/report context
- daily insight context
- signal/member context if still needed
- admin/state scripts

Phase D: stop writes.

- Remove or bypass `record_signal_events` from v4 delivery/system paths after
  remaining v4 delivery callers are rewired.
- Remove `record_battle_event` from `storage/player.py` once `battle_telemetry`
  coverage is verified.
- Retire `scripts/backfill_battle_events.py`.

Phase E: remove old rollup and maintenance behavior.

- Delete `prune_event_stream_with_rollups` from the maintenance path.
- Decide whether `event_rollups` remains for another purpose. If not, retire it
  with `game_event_stream`.

Phase F: schema/API cleanup.

- Remove `storage/event_stream.py`.
- Remove the `storage.event_stream` export from `db/__init__.py`.
- Add a migration to drop `game_event_stream` and indexes in live databases.
- Leave historical migrations intact.
- Rewrite/delete tests that exist only for the old stream.
- Update docs and AGENTS database table list.

Exit criteria:

- `rg "game_event_stream|storage\\.event_stream|record_signal_events|record_battle_event|summarize_events_by_window|list_recent_events|summarize_battle_modes"` has no production hits outside migration history and archived docs.
- full suite passes
- live v5 health still reports fresh detections and battle telemetry
- weekly recap, memory synthesis, and `get_elixir_state` still work

## Finding 3: Presentation Routing Lives Too Close To Event Core

The event payloads are mostly presentation-free, which is good. The package
boundary is weaker:

- `event_core/live/runtime.py` hardcodes channel IDs, channel names, and lane
  names.
- `event_core/mind/communication.py` maps detection types to routing prefixes.
- `event_core/live/discord.py` renders fallback copy.

This does not corrupt the event store, but it makes the `event_core` package own
too much surface behavior.

### Fix Plan

1. Define a package boundary:
   - `event_core/domain`: observed domain events, detections, recommendations,
     decision cases
   - `event_core/mind`: detection and recommendation policy only
   - `runtime/reactive_*`: routing, copy composition, outbound posting, Discord
     channel lookup
2. Move routing out of `event_core`.
   - Replace hardcoded channel IDs with `prompts/DISCORD.md` lane lookup via
     existing runtime helpers.
   - Keep fail-closed behavior for unknown/leadership scope.
3. Move copy/fallback renderers out of `event_core/live`.
4. Decide the fate of `CommunicationIntent`.
   - Short term: keep it as a transitional outbox aggregate but document it as
     downstream from Event Core, not part of the authoritative domain stream.
   - Better end state: replace it with a conventional `reactive_outbox` table
     owned by runtime. Event Core emits detections/recommendations; the runtime
     adapter creates delivery work.
5. Add boundary tests.
   - no channel IDs or lane names in Event Core event payloads
   - routing lives in runtime only
   - leadership scope always fails closed

Exit criteria:

- `event_core/domain` and `event_core/mind` have no channel IDs, channel names,
  lane names, or Discord-specific copy
- event payloads contain no presentation-specific fields
- posting still works through runtime adapter

## Finding 4: Health Metrics Need Delivery State, Not Event Counts

Current health computes pending as:

```
CommunicationIntent.Raised - Fulfilled - Dropped
```

That is not reliable after `fast_forward()`, because fast-forward moves the
consumer cursor without changing aggregate status.

### Fix Plan

1. Add a real intent/outbox projection.
   - One row per current communication work item.
   - Columns: key, scope, kind, current status, raised position, fulfilled
     position, dropped position, drain reason, updated_at.
2. Compute health from the projection, not raw event counts.
3. Split health metrics:
   - event-store head
   - follower lag
   - deliverable pending
   - drained historical
   - fulfilled
   - failed/retry blocked
4. Split scan-style detectors from log-following lag.
   - `WarUpdateDetector` is scan-style and should not show as event-log lag.
   - Track last successful scan time instead.

Exit criteria:

- health pending means "will be delivered on a future tick"
- historical drained work is explicit
- scan-style detectors do not create false lag alerts

## Recommended Sequence

1. **Immediate safety fix:** make `catch_up()` one-time/manual and repair health
   pending semantics enough to avoid false confidence.
2. **Routing boundary cleanup:** move Discord/channel/lane routing out of
   `event_core`.
3. **Add v5 read facades:** create replacements for old event-window, recent
   event, and game-mode APIs.
4. **Migrate live readers off `game_event_stream`.**
5. **Stop old writes to `game_event_stream`.**
6. **Remove old stream APIs, tests, pruning, and table.**
7. **Run full verification and restart Elixir.**

## Suggested Issues

Create separate implementation issues so each can land safely:

1. Stop repeated v5 catch-up drains on restart.
2. Replace `game_event_stream` readers with v5 read facades.
3. Stop `game_event_stream` writes and retire old stream storage.
4. Move reactive routing/copy out of `event_core`.
5. Fix v5 health pending/lag metrics.

## Verification Gates

For each implementation issue:

- targeted tests for the changed boundary
- `./venv/bin/pytest tests/ -q`
- no runtime restart until code is committed
- restart only for runtime behavior changes
- after restart:
  - `bash scripts/admin.sh status`
  - `python -m event_core.live.health`
  - `python -m event_core.live.monitor`
  - fresh `elixir-v5.log` confirms reactive tick behavior
