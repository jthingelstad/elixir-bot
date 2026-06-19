# Signal Inventory and Guardrails

This document catalogs the signal types Elixir can emit into proactive
awareness and delivery flows. It also defines the guardrails for Phase 0 of the
internal data subsystem pivot: event identity, shadow event-stream retention,
and which workflow layers may write which durable objects.

Last updated: 2026-06-19, after the shadow `game_event_stream` foundation.

## Signal Lifecycle

Current proactive flow:

1. Facts are fetched or derived from the Clash Royale API and local SQLite state.
2. Detectors emit signal dictionaries.
3. `storage.event_stream.record_signal_events()` records those signals in
   `game_event_stream` in shadow mode.
4. Existing awareness/delivery paths continue unchanged.
5. Existing delivery state is still tracked in `signal_log`, `signal_outcomes`,
   `messages`, and leader-action tables.

Important distinction: the event stream is an observation ledger, not a posting
queue and not a delivery ledger.

## Event Identity Policy

Every event-stream row needs deterministic identity. Use this order:

1. `signal_key`, when the detector provides one.
2. `signal_log_type`, when present.
3. Derived identity from stable fields:
   - `type`
   - `signal_date`
   - member/player tag
   - `season_id`
   - `week` or `section_index`
   - `day_number` or `period_index`
   - `milestone`, `card_name`, or `award_type`
4. Compact payload hash only as a final fallback.

Do not use `event_key` as source identity. It is a downstream event-stream
annotation and would make repeated shadow ingestion generate new events.

For batched signals, prefer one aggregate event when the signal is semantically
one observation (`inactive_members`, `war_attacks_complete`,
`season_awards_granted`). Later case/project phases may fan that aggregate into
per-member cases.

## Retention Model

- `game_event_stream` keeps full-fidelity compact event rows for 90 days.
- Standard query windows are 7, 28, 56, and 90 days.
- 7 days is for recent pulse and prompt-friendly context.
- 28 days is one full River Race cycle.
- 56 days supports current-cycle vs prior-cycle comparison.
- 90 days supports broader trend and analytics scans.
- History that must survive beyond 90 days should live in facts, projects,
  cases, memories, or future `event_rollups`.

## Workflow Write Boundaries

These boundaries keep the pivot coherent while the old and new systems overlap:

| Layer | May Write | Notes |
|---|---|---|
| Detectors / ingestion | facts, signal dictionaries | No Discord I/O. |
| Shadow event stream | `game_event_stream` | Best-effort; failures must not block delivery. |
| Awareness loop | future projects/cases/intents; current memories/revisits | Public situations must not include leadership-only state. |
| Delivery layer | `messages`, `signal_outcomes`, Discord/site delivery results | Should not invent new facts. |
| Leader action UI | leader-action cards and decisions | Future work links cards to `decision_cases`. |
| Memory synthesis | contextual memories | Memory summarizes; it is not operational truth for cases/projects. |
| Admin/manual commands | explicit human-requested writes | Must preserve scope and source metadata. |

## Roster and Identity Signals

Source: `heartbeat/_roster.py`, called from `heartbeat.tick(include_nonwar=True)`
unless noted.

| Type | Trigger | Identity / Dedup |
|---|---|---|
| `member_join` | Tag appears in current API roster but not previous active roster. | `signal_log_type=member_join:<tag>`. |
| `member_leave` | Tag disappears from current API roster. | `signal_log_type=member_leave:<tag>:<chicago_date>`. |
| `arena_change` | Stored arena milestone diff. | `signal_log_type` from stored milestone row. |
| `elder_promotion` | Role changes from member to elder. | `signal_log_type` from role-change detector. |
| `donation_leaders` | Top daily donors after `DONATION_HIGHLIGHT_HOUR`. | `signal_log` key `donation_leaders:<date>`. |
| `weekly_donation_leader` | Top donors from prior CR week, emitted Mondays. | `signal_log_type=weekly_donation_leader:<isoweek>`. |
| `inactive_members` | Friday leadership-only inactivity report. | `signal_log` key `inactive_members:<date>`; event identity derives from `type` plus signal date when present. |
| `member_active_again` | Previously dormant member has fresh activity. | `signal_log_type=member_active_again:<tag>:<observed_at>`. |
| `clan_war_trophies_record` | New all-time clan war trophy high. | `signal_log_type=clan_war_trophies_record:<date>`. |
| `clan_birthday` | Clan founding month/day matches today. | `cake_day_announcements`. |
| `join_anniversary` | Member join anniversary matches today. | `cake_day_announcements`. |
| `member_birthday` | Stored member birthday matches today. | `cake_day_announcements`. |

Available but not currently wired into the recurring heartbeat tick:

| Type | Helper | Notes |
|---|---|---|
| `deck_archetype_change` | `detect_deck_archetype_changes()` | Tested helper; not currently part of the scheduled proactive pipeline. |
| `recent_form_slump` | `detect_form_slumps()` | Form slump signals were intentionally kept out of proactive posting; form is background context. |

## Player Progression Signals

Source: `storage/player.py` from `snapshot_player_profile()` and
`snapshot_player_battlelog()`, delivered by `player-progression`.

| Type | Trigger | Lane |
|---|---|---|
| `player_level_up` | Experience level increased. | milestone |
| `career_wins_milestone` | Wins crossed a configured milestone. | milestone |
| `best_trophies_peak` | New all-time best trophies. | milestone |
| `challenge_performance_milestone` | Challenge max wins reached a milestone. | milestone |
| `cr_account_anniversary` | Clash Royale account age anniversary. | milestone |
| `new_card_unlocked` | New card in collection. | milestone |
| `new_champion_unlocked` | New champion in collection. | milestone |
| `card_level_milestone` | Card level crossed configured milestone. | milestone |
| `card_evolution_unlocked` | Card evolution or hero evolution level increased. | optional milestone |
| `badge_earned` | New badge appeared. | milestone |
| `badge_level_milestone` | Badge level crossed milestone. | optional milestone |
| `achievement_star_milestone` | Achievement stars crossed milestone. | milestone |
| `battle_hot_streak` | Consecutive wins in ladder/ranked mode. | battle_mode |
| `battle_trophy_push` | Trophy delta crossed push threshold. | battle_mode |
| `path_of_legend_promotion` | Path of Legends league advanced. | battle_mode |
| `path_of_legend_demotion` | Path of Legends league regressed. | battle_mode |
| `ultimate_champion_reached` | Path of Legends league 10 reached. | battle_mode |
| `path_of_legend_global_rank_attained` | Top-1000 global rank appeared. | battle_mode |

## War Awareness Signals

Sources:

- `heartbeat.tick(include_war=True)` for legacy live war detectors.
- `heartbeat.detect_war_signals_from_storage()` for the current stored-war
  awareness pipeline.

| Type | Trigger | Identity / Dedup |
|---|---|---|
| `war_practice_phase_active` | Current phase is practice/training. | War period `signal_log_type`. |
| `war_battle_phase_active` | Current phase is battle. | War period `signal_log_type`. |
| `war_final_practice_day` | Last practice day. | War period `signal_log_type`. |
| `war_final_battle_day` | Last battle day. | War period `signal_log_type`. |
| `war_battle_days_complete` | All battle days complete. | War period `signal_log_type`. |
| `war_week_rollover` | New week/section detected. | `war_week_rollover::s<season>:w<section>`. |
| `war_season_rollover` | New season detected. | `war_season_rollover::s<season>`. |
| `war_practice_day_started` | Practice day period starts. | War period `signal_log_type`. |
| `war_battle_day_started` | Battle day period starts. | War period `signal_log_type`. |
| `war_practice_day_complete` | Practice day period completes. | War period `signal_log_type`. |
| `war_battle_day_complete` | Battle day period completes. | War period `signal_log_type`. |
| `war_battle_day_final_hours` | Battle day is inside final-hours threshold. | War period/checkpoint key. |
| `war_battle_rank_change` | Our race rank changed. | War period key plus rank. |
| `war_attacks_complete` | Members completed all four battle decks. | Per-member nested `signal_log_type`. |
| `war_surprise_participant` | Rare/never war participant played in current week. | Per-member nested `signal_log_type`. |
| `war_rival_woke_up` | Rival clan moves from zero period points to active. | `war_rival_woke_up:<tag>:<battle_date>`. |
| `war_lead_change` | Lead/deficit delta crosses threshold. | `war_lead_change:<battle_date>:<our_points>`. |
| `war_race_finished_live` | Live state shows race finish. | Live-state signal key. |
| `war_completed` | River Race log gained a new completed race. | `war_completed::<season>:<section>`. |
| `war_week_complete` | Completed race gets weekly summary. | `war_week_complete::<season>:<section>`. |
| `war_champ_standings` | War completion refreshes standings. | `war_champ_standings::<season>:<section>`. |
| `war_season_complete` | Terminal season detected. | `war_season_complete::<season>`. |

## Awards Signals

Source: `heartbeat/_awards.py`, called by daily award detection.

| Type | Trigger | Scope |
|---|---|---|
| `award_earned` | New award row inserted. | Member / season / week depending on award. |
| `season_awards_granted` | Aggregated season awards payload. | Season |

Award types currently include `war_champ`, `iron_king`, `donation_champ`,
`rookie_mvp`, `war_participant`, `perfect_week`, and
`donation_champ_weekly`.

## Tournament and Manual Observation Signals

| Type | Source | Trigger |
|---|---|---|
| `tournament_watching_started` | `runtime/discord_commands.py` | Leader starts watching a tournament. |
| `tournament_started` | `storage/tournament.py` | Tournament enters in-progress state. |
| `tournament_ended` | `storage/tournament.py` | Tournament ends. |
| `tournament_lead_change` | `storage/tournament.py` | Our rank changes. |
| `tournament_participant_joined` | `storage/tournament.py` | New participant appears. |
| `tournament_battle_played` | `runtime/jobs/_tournament.py` | New battle appears in watched tournament. |
| `clan_voyage_complete` | `runtime/channel_router.py` | Leader-posted screenshot observation completes a Clan Voyage capture. |

Tournament signals currently route to `#clan-events` through the clan-event
lane.

## System and Maintenance Signals

| Type | Source | Trigger |
|---|---|---|
| `capability_unlock` | `runtime/system_signals.py` | Startup-seeded system signal not yet announced. |
| `api_event_sentinel` | API sentinel | New Clash Royale `/events` observation. |
| `api_schema_sentinel` | API sentinel | First-seen API schema path. |
| `discord_invite_reminder` | Weekly relay job | Weekly no-link clan-chat invite reminder. |

## Leadership Analytics That Are Not Proactive Signals

`storage/war_analytics.py` returns analytics rows such as `inactive`,
`low_donations`, and `low_war_participation` for structured tools and leader
action scans. Those rows are not themselves proactive awareness signals unless a
detector or job wraps them into a signal such as `inactive_members` or a future
decision case.

## Routing Guardrails

Current hard routing, before future cases/intents:

- War signals route to `#river-race`; some also route to `#leader-actions` or
  `#leaders`.
- Battle-mode and milestone signals route to `#player-highlights`.
- Roster/community events route to `#clan-events`, with selected leadership
  side notes.
- Leadership-only signals route to `#leaders`.
- `#leader-actions` remains an action-board projection, not a generic signal
  destination.

The event stream records the same observations before delivery, but it must not
change these routing outcomes in Phase 1 / Phase 0.
