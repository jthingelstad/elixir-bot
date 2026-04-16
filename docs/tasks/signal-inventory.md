# Signal Inventory

A catalog of every signal the Elixir data layer emits into the agentic awareness
loop, grouped by source. Each entry lists the signal `type`, the emitting
function, the trigger, and any dedup key. This is the authoritative list of
"what Elixir can see" — if it's not here, the agent cannot react to it.

Kept fresh as new detectors land. Last updated: 2026-04-16 (v4.7 autonomous
refactor).

---

## Roster & identity — `heartbeat/_roster.py`

| Type | Trigger | Dedup |
|---|---|---|
| `member_join` | Tag in current API roster but not in previous snapshot. | Natural (snapshot diff). |
| `member_leave` | Tag in previous snapshot but not in current API roster. Enriched via `_enrich_leave_signal`. | Natural (snapshot diff). |
| `arena_change` | DB milestone diff on `arena_id`. | `signal_log_type` from `detect_milestones`. |
| `elder_promotion` | Role change from `member` → `elder`. | `signal_log_type` from `detect_role_changes`. |
| `donation_leaders` | Top 3 daily donors, once per day after `DONATION_HIGHLIGHT_HOUR`. | `signal_log` keyed on `donation_leaders:<date>`. |
| `weekly_donation_leader` | Top 3 donors of the prior CR week, emitted Mondays off frozen Sunday `member_daily_metrics.donations_week`. | `signal_log_type` weekly: `weekly_donation_leader:<isoweek>`. |
| `inactive_members` | Members past `INACTIVITY_DAYS` threshold; Fridays only, once per week. | `signal_log` keyed on `inactive_members:<date>`. |
| `member_active_again` | Previous snapshot stale (≥ threshold), current fresh, `last_seen_api` advanced. | `signal_log_type` keyed on `member_active_again:<tag>:<observed_at>`. |
| `clan_rank_top_spot` | Current snapshot has `clan_rank = 1`, previous had `> 1` or NULL. | `signal_log_type` keyed on `clan_rank_top_spot:<tag>:<observed_at>`. |
| `recent_form_slump` | Form crosses top-tier (`hot`/`strong`) → bottom-tier (`slumping`/`cold`). Per-(member,scope) cursor remembers last label. | `signal_log_type` weekly: `recent_form_slump:<tag>:<scope>:<isoweek>`. |
| `deck_archetype_change` | Current deck differs by 4+ cards from the deck fetched 24+ hours ago (mode_scope='overall'). Natural de-flicker via the 24h window. | `signal_log_type` daily: `deck_archetype_change:<tag>:<YYYY-MM-DD>`. |
| `clan_birthday` | Month-day of `clan_founded` matches today. | `cake_day_announcements` unique on (date, type, tag). |
| `join_anniversary` | Members whose `joined_at` anniversary is today. | Same as above. |
| `member_birthday` | Members whose birthday is today. | Same as above. |

---

## Player progression — `storage/player.py`

All fire off `snapshot_player_profile` / `snapshot_player_battlelog` as new CR
API payloads arrive, then flow into heartbeat via `detect_pending_system_signals`
or direct return. Dedup is generally by `signal_log_type` stored on the snapshot
row.

| Type | Trigger |
|---|---|
| `player_level_up` | `exp_level` increased vs. previous profile snapshot. |
| `career_wins_milestone` | Total wins crossed a 1,000-win threshold. |
| `best_trophies_peak` | `best_trophies` set a new all-time high. |
| `challenge_performance_milestone` | `challenge_max_wins` reached a milestone value. |
| `path_of_legend_promotion` | PoL league advanced (1→10). |
| `path_of_legend_demotion` | PoL league regressed. |
| `ultimate_champion_reached` | PoL league 10 reached for the first time. |
| `path_of_legend_global_rank_attained` | Top-1000 global rank appeared (non-null). |
| `new_card_unlocked` | New card ID in collection. |
| `new_champion_unlocked` | New champion card. |
| `card_level_milestone` | Card level crossed 10/14 threshold. |
| `card_evolution_unlocked` | Card's `evolutionLevel` increased (evo 0→1 or hero 0→2/2→3). Payload carries `evolution_kind` ∈ {`evo`, `hero`}. |
| `badge_earned` | New badge in profile. |
| `badge_level_milestone` | Badge level milestone. |
| `achievement_star_milestone` | Achievement stars crossed a threshold. |
| `battle_hot_streak` (mode: `ladder` / `ranked`) | ≥4 consecutive wins in the given mode since last snapshot. |
| `battle_trophy_push` (mode: `ladder` / `ranked`) | Cumulative trophy_change ≥ threshold since last snapshot in the given mode. |

---

## War awareness — `heartbeat/_war.py` and `storage/war_status.py`

These are the heaviest-weight signals. The agent treats war signals as the
highest-priority lane. Dedup is via `war_signal_log` or `signal_detector_cursors`
keyed per war period.

| Type | Trigger |
|---|---|
| `war_battle_phase_active` / `war_practice_phase_active` | Phase marker at start of war day. |
| `war_final_practice_day` / `war_final_battle_day` | Last day of a phase. |
| `war_battle_days_complete` | All battle days ended for the week. |
| `war_week_rollover` | New `periodIndex` begins vs stored cursor. |
| `war_season_rollover` | New season detected from war log. |
| `war_practice_day_started` / `war_battle_day_started` | Period start crossed. |
| `war_practice_day_complete` / `war_battle_day_complete` | Period end crossed. |
| `war_battle_day_final_hours` | ≤ N hours remaining in a battle day. |
| `war_battle_rank_change` | Clan leaderboard rank changed during battle day. |
| `war_week_complete` | Week finalized in war log. |
| `war_season_complete` | Season finalized (terminal `periodType`). |
| `war_completed` | War log gained a new entry (post-hoc result). |
| `war_champ_standings` | War completion → fresh War Champ standings. |
| `war_race_finished_live` | Live state shows a race close event. |

See `heartbeat/_helpers.py:BATTLE_DAY_SECONDS` + `_BATTLE_DAY_CHECKPOINTS` for
the time-based thresholds; wall-clock computation in `build_situation_time`.

---

## Tournament — `storage/tournament.py`

Fires inside the tournament poller; surfaced into `#tournaments` via
`detect_pending_system_signals`.

| Type | Trigger |
|---|---|
| `tournament_started` | Tournament entered `inProgress` state. |
| `tournament_ended` | Tournament reached `ended` state. |
| `tournament_lead_change` | Our rank in the leaderboard changed. |

---

## War participation analytics — `storage/war_analytics.py`

Weekly post-war analysis; surfaces leadership-scope concerns.

| Type | Trigger |
|---|---|
| `inactive` | Member with 0 war battles in the completed week. |
| `low_donations` | Member below weekly donation threshold. |
| `low_war_participation` | Member with fewer than expected battles. |

---

## System signals — `runtime/system_signals.py`

Startup-time signals (v4.7 capability unlocks, release announcements, etc.).
Delivered into `#announcements` or matching channel based on `audience`.

| Type | Trigger |
|---|---|
| `capability_unlock` | New `STARTUP_SYSTEM_SIGNALS` entry with unused `signal_key`; fires once per version. |

---

## Deficits noted during the v4.7 audit

These are things the DB stores that had no signal until the v4.7 refactor, or
that remain unsurfaced:

- **`member_recent_form`** — four scopes × ~30 members. Only upward transitions
  fed `battle_hot_streak`; downward was dark. **Closed by #27
  (`recent_form_slump`).**
- **`clan_rank` on every snapshot** — tracked but never signaled. **Closed by
  #29 (`clan_rank_top_spot`).**
- **`member_state_snapshots.last_seen_api` returning to fresh** — no "welcome
  back" counterpart to the inactivity detector. **Closed by #26
  (`member_active_again`).**
- **Path of Legends full lifecycle** — promotion existed; demotion, UC reach,
  and top-1000 global rank did not. **Closed by #23 / #24 / #25.**
- **Best-trophies peak and challenge-max-wins** — stored on every profile
  snapshot but not diffed. **Closed by #28 / #30.**
- **Battle pulse mode conflation** — ladder and ranked battles were merged into
  one signal stream, making it impossible to tell where progress was happening.
  **Closed by #22 (per-mode `mode` field on `battle_hot_streak` /
  `battle_trophy_push`).**

Still open / future work:

- None at the moment. Signal-flow backlog is cleared.

Closed this sprint (v4.7 autonomous signal refactor):

- **Card evolution unlocks.** Previously the card-diff loop tracked levels but
  not `evolutionLevel`. **Closed** — now emits `card_evolution_unlocked` with
  `evolution_kind` ∈ {`evo`, `hero`}.
- **Member deck-style trends.** 18k `member_deck_snapshots` rows nobody read.
  **Closed** — `deck_archetype_change` fires when the current deck differs by
  4+ cards from the deck 24h ago.
- **Silent lane routing bug.** All ten v4.7-added signal types were falling
  into the "unknown" lane because none were added to `PROGRESSION_SIGNAL_TYPES`
  / `BATTLE_MODE_SIGNAL_TYPES` / `CLAN_EVENT_SIGNAL_TYPES` /
  `LEADERSHIP_ONLY_SIGNAL_TYPES`. **Closed** — all ten now route correctly
  (locked down by a regression test in `test_awareness_loop.py`).
- **Clan-level trophy records.** `clan_daily_metrics` had totals but no
  "new clan-score high" or "new clan-war-trophy high" signal. **Closed** —
  `clan_score_record` / `clan_war_trophies_record` fire once per new
  all-time high (roughly every 2-3 days at current trajectory).
- **Longer-window donation leaders.** Only daily top-3; nothing captured
  the weekly carriage. **Closed** — `weekly_donation_leader` fires on
  Mondays with the prior CR week's top-3.

---

## How dedup works, in brief

Three conventions are used across detectors:

1. **`signal_log` table** via `was_signal_sent` / `mark_signal_sent` — for
   calendar-bounded events (daily/weekly). Example: `donation_leaders:<date>`.
2. **`signal_log_type`** on a signal dict — the downstream publisher writes the
   type to `signal_log` automatically when the post lands. Used for
   per-observation dedup like `clan_rank_top_spot:<tag>:<observed_at>`.
3. **`signal_detector_cursors`** — keyed `(detector_key, scope_key)` with free
   text / int payloads. Used when the detector needs remembered state that
   isn't a table-level field. Example: `form_slump` remembers last label per
   (member, scope).

When adding a new detector, choose the dedup style that matches the event
cadence: calendar → (1), observation → (2), needs state → (3).
