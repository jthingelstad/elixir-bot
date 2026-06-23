# v4 Signal-System Deletion — Execution Runbook

**Purpose:** a self-contained brief so a FRESH session can finish decommissioning
the v4 signal/awareness system (item 7 + issue #101 "F2") without re-discovering
the footprint. Read this + the cited files; you should not need prior chat context.

## Baseline (start state — verify before starting)
- Branch `main`, clean tree, at commit `3d63475` (or later). `./venv/bin/ruff check .`
  passes; full suite green (`./venv/bin/pytest tests/ -q`, ~987 passing, ~4 min).
- Bot healthy: `./venv/bin/python -m event_core.live.monitor` → service_up, no errors,
  consumer at head. `git check-ignore .env` confirms .env is untracked.
- **Gate every step:** `./venv/bin/ruff check .` AND `./venv/bin/pytest tests/ -q`
  must pass before committing. Commit ONLY green — `main` must stay deployable.
  ruff is in the venv, not on PATH; a `.git/hooks/pre-commit` runs it automatically.

## Already done (do NOT redo)
- All 3 enabled v4 awareness-delivery callers are rewired to direct posts via
  `runtime/discord_posting.compose_and_post`: award-detection (`262a735`),
  tournament-watch / item 2d (`7df45d5`), weekly-invite-relay (`af20c49`).
  **No ENABLED job calls `_deliver_signal_group_via_awareness` anymore** — only the
  DISABLED `_clan_awareness_tick` / `_war_awareness_tick` (in `runtime/jobs/_core.py`)
  still reference it. Those jobs are `enabled_by_default=False` and NOT scheduled.
- #101 F1 (one-time catch_up via `cutover:v5` marker) and F4 (deliverable-pending
  health) are done. Don't touch.

## The ENABLED job list (what must keep working — your live-vs-dead oracle)
A signal-package function is a SURVIVOR (must relocate) only if reached from one of
these enabled/live paths; otherwise it is dead (delete with the package):
v5-reactive-tick, war-poll (ingest only), player-progression (refresh-only),
award-detection, daily-clan-insight, leadership-action-scan, weekly-discord-invite-relay,
memory-synthesis, weekly-recap, promotion-content (`_site`), card-catalog-sync,
api-sentinel (`_maintenance`), db-maintenance, clan-wars-intel (`_intel`),
tournament-watch, plus the agent (`agent/workflows.py`) and admin/discord_commands.

## SURVIVOR SET (relocate in Phase 0 — confirmed live importers)
| Survivor | Defined in | Live importers | Suggested neutral home |
|---|---|---|---|
| `_post_to_elixir` | real: `runtime/discord_posting.py`; shim: `_signals.py` | `_maintenance`, `_site`, `_tournament`, `_intel` | already in `discord_posting` — just repoint imports there |
| `_load_live_clan_context` | shim in `_signals.py` → `_runtime_app()` | `_site` | call `_runtime_app()._load_live_clan_context()` directly, or move shim to `runtime/helpers` |
| `_channel_config_by_key` | `_signals.py` | `_intel`, `_core` | `runtime/helpers/_channels.py` |
| `build_lane_memory_context` | `signal_lanes.py` | `_intel`, `_core` (daily-clan-insight) | `runtime/helpers` |
| `_post_system_signal_updates` | `signals/system.py` | `_maintenance` (api-sentinel) | new small module e.g. `runtime/system_status_post.py` |
| `_format_weekly_recap_post`, `_strip_weekly_recap_header` | `_signals.py` | `_core` (weekly-recap) | `runtime/helpers/_reports.py` |
| `is_war_signal` | `signal_lanes.py` | `agent/workflows.py` | a tiny neutral util (it's a pure predicate) |

VERIFY each before moving (the importer list is from the 2026-06-23 inventory; grep
to confirm). Some `_core` imports (`_mark_delivered_signals`,
`_persist_signal_detector_cursors`, `_publish_pending_system_signal_updates`,
`_deliver_signal_group_via_awareness`) are used ONLY by the disabled awareness jobs
— those go away in Step 2, so they are NOT survivors.

## Sequenced steps (leaves-inward; gate + commit each)

**Step 1 — Phase 0: relocate survivors.** Move the table above to neutral homes;
repoint the live importers (`_maintenance`, `_site`, `_tournament`, `_intel`, `_core`,
`agent/workflows`). Pure refactor, no behavior change. After this, live code imports
nothing from `runtime/signals/`, `runtime/jobs/_signals.py`, `signal_lanes.py`,
`runtime/situation.py`. Gate; commit; deploy; `monitor` clean.

**Step 2 — remove dead awareness entry points.** Delete `_clan_awareness_tick` /
`_war_awareness_tick` from `_core.py` + their `activities.py` entries + `app.py`/
`runtime/jobs/__init__.py` re-exports; remove the admin `/signals` view
(`runtime/admin.py:295,736`) + the `discord_commands.py:470` reference; delete
`_deliver_signal_group_via_awareness` usage. **Delete `tests/test_awareness_loop.py`
wholesale** (it only tests the awareness machinery). Trim re-export blocks FIRST or
`app.py` won't import. Gate; commit; deploy.

**Step 3 — F2 `game_event_stream` retirement.** Use the reader/writer inventory +
replacement-source table already in `event-core-v5-remediation-plan.md` (Finding 2).
Order: add v5 read facades → migrate readers (`runtime/jobs/_memory.py`,
`runtime/helpers/_reports.py`, `runtime/jobs/_core.py` daily-insight, then
`agent/tool_exec.py` `get_elixir_state` LAST — it's live chat) → stop writes
(`record_signal_events`, `storage/player.py` `record_battle_event`, retire
`scripts/backfill_battle_events.py`) → drop `game_event_stream` + `event_rollups` +
`storage/event_stream.py` + `prune_event_stream_with_rollups`. Gate each sub-step.

**Step 4 — delete the package + remaining tests.** Remove `runtime/signals/` (pkg),
`runtime/jobs/_signals.py`, `runtime/signal_lanes.py`, `runtime/situation.py`,
`scripts/replay_awareness.py`. Rewrite/remove the test surface that imports them:
big chunks of `tests/test_elixir_heartbeat.py` (~40 `runtime.signals.delivery`
imports — welcome/arena/war relay copy builders), `test_signal_flow_guardrails.py`,
`test_revisits.py`, `test_game_mode_intelligence.py`, `test_phase2_consumption.py`,
`test_memory_system.py`, `test_war_surprise_dedup.py`, `test_awards.py:529`,
`conftest.py:81`. Drop `signal_log`/`signal_outcomes`/`signal_detector_cursors`/
`awareness_ticks` tables (guard the status-surface readers first). Gate; commit; deploy.

## Known traps
- `runtime/signals/delivery.py` is a LARGE delivery layer (welcome/arena/war relay
  copy builders), not just awareness. Most is dead post-rewire but verify per-function
  (grep each name for non-test, non-disabled callers) before deleting.
- `app.py` and `runtime/jobs/__init__.py` have `_signals` re-export blocks — trim
  them BEFORE deleting `_signals.py` or import fails at startup.
- Deploy = `launchctl bootout gui/$(id -u)/com.poapkings.elixir` then `bootstrap …
  ~/Library/LaunchAgents/com.poapkings.elixir.plist`. catch_up now SKIPS on restart
  (F1) — expected log: `v5 go-live catch-up: {'skipped': ...}`.
- After each deploy: `monitor` clean + bot startup has no Traceback.

## Rollback
Each step is its own commit; `git revert <sha>` + redeploy. The bot reads `main`'s
deployed code; never leave `main` mid-refactor (commit only green).
