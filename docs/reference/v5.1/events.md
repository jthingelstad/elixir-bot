# Elixir v5.1 — Event Catalog

> **Status:** ✅ Build-ready (completeness pass applied 2026-07-03; feedback.md rev 4).
> **Owner:** Jamie · **Last worked:** 2026-07-03
>
> Every `event_type` per stream: payload shape, dedup key, timing, and owner.
> Grounded against the current Gen C detectors (`event_core/mind/detectors.py`,
> line-cited) — the new catalog **carries every live detection concept** and adds
> what §9/§11 designed but never built (arena-up, clan-entity movement,
> demotions). `recognition.md` owns what is *notable*; this doc owns what *exists*.
> Table shapes are in `schema.md` §5.

## 1. Rules that apply to every event

- **Dedup key = identity** (§8): `{event_type}:{subject}:{natural-key-of-change}`.
  Re-processing never double-emits (UNIQUE constraint). *One exception:* the
  battle stream's key is `'{player_tag}:{battle_time}:{opponent_tag}'`
  (schema.md §5.1) — the log *is* the battles, so the battle's own identity is
  the key, with no `event_type` component.
- **First-sight emits nothing** (§8): a new baseline row produces zero events.
- **Timing honesty** (§8): battle-derived facts are `exact`; emitted (diffed) facts
  are `estimated` with `window_start = prev_observed_at`. Composition may say
  "just now" only for `exact`.
- **Payloads are typed facts, presentation-free.** No copy, no adjectives.
- **Scope** is the widest audience a fact may *reach*, not a promise it posts:
  `public` default; nothing in this catalog is leadership-scoped (management
  signals are projections, not events, §13). Whether a public-scope event
  actually posts is recognition's call — e.g. `role_changed(direction=demoted)`
  is scope-public (visible in roster/history reads) but never publicly posted
  (`recognition.md` §4).
- Ownership rule (§7): *leveling up is a player event; joining is a clan event.*

## 2. Battle stream — the log is the battles

The battle stream has **no emitted event types**: its log *is* `battle_events`
(one row per battle, `timing = exact`, §9.1). Everything the old engine derived
from battles (`battle_trophy_push` — `detectors.py:346`, `battle_hot_streak` —
`:164`, `ranked_activity_pulse` — `:495`) becomes a **derived battle moment**:
computed by the battle recognizer from battle rows, claimed in the recognition
ledger, never stored as a second event row. One storage shape per stream; no
"derived event" tables.

Derived battle moments (recognition candidates; keys claimed in the ledger):

| Moment | Ledger key | Computed from | Notes |
|---|---|---|---|
| `arena_up` | `arena_up:{tag}:{arena_id}` | a battle whose `starting_trophies + trophy_change` crosses an arena boundary | **New** (§11: battle primary). Arena thresholds are game knowledge (`cr_knowledge.py`); Path-of-Legend excluded. |
| `trophy_push` | — | — | **Retired 2026-07-05** (Jamie: ancient-version artifact — confirmed: 31 claims / 0 posts / 0 accrual citations since go-live; max score 50 vs threshold 80, structurally unable to post). A climb is a trend — `player_daily_battle_rollups` owns it; the ledger claims moments. Joins `hot_streak` in the retired-concept ledger. |
| `hot_streak` | — | — | **Not computed in v5.1.** Matches today exactly: the detector exists but is intentionally not registered (`detectors.py` `ALL_DETECTORS` comment) — redundant with `trophy_push` (`communication.py:26–31`). It therefore contributes nothing to accrual evidence either. Listed only so the concept's disposition is recorded (C2 mapping §6). |
| `ranked_pulse` | `ranked_pulse:{tag}:{chicago_day}` | ≥N Path-of-Legend battles in a day | Carries `ranked_activity_pulse` (`detectors.py:495`). |

The player Pulse (`pulse.md`) is a **reader, not a stream**: it narrates 8h
windows of battle/player rows and emits no events — its only writes are a
`pulse:player:{window_end}` ledger claim and one battle-feed intent.

War battles are battle rows with war keys set (`schema.md` §5.1); the war stream
consumes them, it does not duplicate them (§12).

## 3. Player stream — emitted from profile diffs

Emitters diff `state_baselines(entity_kind='player')` per aspect. Aspects and the
types they own:

### Aspect `profile` (level, wins, trophies-peak, badges)

| `event_type` | Dedup key | Payload | Carries (Gen C) |
|---|---|---|---|
| `level_up` | `level_up:{tag}:{level}` | `{level, prev_level}` | `player_level_up` (`detectors.py:38`) |
| `career_wins_milestone` | `career_wins_milestone:{tag}:{milestone}` | `{milestone, wins}` | `:75` |
| `best_trophies_peak` | `best_trophies_peak:{tag}:{boundary}` | `{boundary, best_trophies}` | `:56` |
| `badge_earned` | `badge_earned:{tag}:{badge_name}` | `{badge_name, level?}` | `:282` |
| `arena_changed` | `arena_changed:{tag}:{arena_id}` | `{arena_id, arena_name, prev_arena_id}` | **New** — §11's profile-side confirmation of arena-up (estimated timing; backstops a missed deciding battle) |

### Aspect `cards` (collection)

| `event_type` | Dedup key | Payload | Carries |
|---|---|---|---|
| `card_unlocked` | `card_unlocked:{tag}:{card_id}` | `{card_id, card_name, rarity}` | `new_card_unlocked` (`:239`). Rarity in payload drives scoring (champion 90 / legendary 65, recognition.md). The redundant `new_champion_unlocked` (`:252`) — the source of the live double-post (`communication.py:38–42`) — is **not carried**; the ledger + rarity payload cover it. |
| `card_level_milestone` | `card_level_milestone:{tag}:{card_id}:{milestone}` | `{card_id, card_name, milestone}` | `:200` |
| `collection_level_milestone` | `collection_level_milestone:{tag}:{milestone}` | `{milestone, collection_level}` | `:308` |

### Aspect `ranked` (Path of Legend)

| `event_type` | Dedup key | Payload | Carries |
|---|---|---|---|
| `pol_promotion` | `pol_promotion:{tag}:{league}` | `{league, prev_league}` | `path_of_legend_promotion` (`:103`) |
| `ultimate_champion_reached` | `ultimate_champion_reached:{tag}` | `{season}` | `:113` |
| `pol_global_rank_attained` | `pol_global_rank_attained:{tag}:{to_rank}` | `{from_rank, to_rank, league}` | `:123` (old key prefix `path_of_legend_global_rank` differed from its type name; v5.1 aligns key prefix = event_type everywhere; the key's rank is the rank *attained*) |

## 4. Clan stream — emitted from clan/roster diffs, plus calendar

### Aspect `roster` (membership & roles — one clan poll, per-member diffs)

| `event_type` | Dedup key | Payload | Carries |
|---|---|---|---|
| `member_joined` | `member_joined:{tag}:{observed_at}` | `{name, trophies, role}` | `:532`. Also opens a `clan_memberships` row. |
| `member_left` | `member_left:{tag}:{observed_at}` | `{name, role, trophies, tenure_days}` | `:600`. Kick-suppression is **recognition's** job (C1), not the emitter's — the event always exists; the post may not. Closes the membership row. |
| `role_changed` | `role_changed:{tag}:{new_role}:{observed_at}` | `{new_role, prev_role, direction: promoted\|demoted}` | Generalizes `member_promoted` (`:629`) — demotions become first-class instead of invisible (§14.5 role-change reads). |

### Aspect `clan_entity` (the clan itself; ours and River-Race opponents, one shape)

| `event_type` | Dedup key | Payload | Carries |
|---|---|---|---|
| `clan_score_milestone` | `clan_score_milestone:{clan_tag}:{milestone}` | `{milestone, clan_score}` | **New** (§9.3 owns clan movement; no Gen C detector existed) |
| `clan_league_changed` | `clan_league_changed:{clan_tag}:{league}` | `{league, prev_league}` | **New** (§9.3) |
| `weekly_donation_leader` | `weekly_donation_leader:{iso_week}` | `{week_ending, leaders: [{tag, name, donations}]}` — **top 3**, carried from `TOP_N = 3` (`detectors.py:1000`); subject = rank 1 | `:1031`. **Owner: the roster emitter** (tick step 3, `runtime.md` §2): when a roster diff shows the weekly donation reset (donations collapse toward zero vs the baseline), the emitter computes the leader from the **previous** baseline — inside the same transaction, before the new baseline overwrites it — and stamps the just-closed `{iso_week}`. No separate scheduled scan. |

### Calendar sub-source (clock-driven, not API-diffed; evidence marks `source: calendar`)

**Owner: the calendar emitter** — runs inside tick step 3 (`runtime.md` §2) on the
**first tick of each America/Chicago day**, reading `player_metadata`
(birth month/day), `clans` (founded date), and `clan_memberships` (anniversaries).
No API call, no baseline; the date embedded in the dedup key is the idempotency.

| `event_type` | Dedup key | Payload | Carries |
|---|---|---|---|
| `member_birthday` | `member_birthday:{tag}:{date}` | `{name}` | `:963` (reads `player_metadata` birth month/day) |
| `clan_birthday` | `clan_birthday:{date}` | `{years}` | `:948` |
| `join_anniversary` | `join_anniversary:{tag}:{date}` | `{name, years}` | `:985` (reads `clan_memberships`) |

`cohort_wave` (`:876`) is **not an event type in v5.1** — it aggregates other
events (3+ members, same milestone, same Chicago day) and belongs to the
notability layer. It moves to `recognition.md` with its constants
(`MIN_MEMBERS = 3`, `WAVE_TYPES`, `detectors.py:832–835`).

## 5. War stream — bounded (§12/§16)

Sourced from `currentriverrace` + `riverracelog` diffs against the
`state_baselines('riverrace')` aspect. **Stored in `war_events`** (schema.md §5.3
— added 2026-07-03; previously these types had no table). All events carry
`{season_id, section_index}`.

| `event_type` | Dedup key | Payload | Carries / notes |
|---|---|---|---|
| `season_started` | `season_started:{season_id}` | `{season_id}` | `new_season` (`:818`). Birth of the bounded instance = death of the prior (§16.1). |
| `war_day_opened` | `war_day_opened:{season_id}:{section_index}:{day}` | `{period_type, day_index}` | Carries the day-transition half of `war_update` (`:774`); the pace/momentum half is the war clock's job (§16.2), not an event. |
| `colosseum_detected` | `colosseum_detected:{season_id}` | `{section_index}` | **New** — §16.1's end-is-discovered rule as a first-class fact (`periodType == 'colosseum'`, verified in live payloads, feedback New-1). |
| `week_finished` | `week_finished:{season_id}:{section_index}` | `{our_rank, our_fame, standings: [{clan_tag, fame, rank}]}` | `war_complete` (`:743`); also writes `war_weeks` / `war_week_clans`. |
| `race_finished` | `race_finished:{season_id}:{section_index}` | `{finished_at}` | **New** — we crossed the finish line mid-week; drives the §16.4 urgency→recognition tone shift. **Normal weeks only (10,000 fame):** Colosseum has no finish line, so this event never fires there (revised 2026-07-04). |
| `season_closed` | `season_closed:{season_id}` | `{final_rank, weeks, war_champ_tag, free_pass_tag, standings_top: […]}` | **New** — fires after Colosseum war days complete; computes Q2's honor + rotation outcome into `war_seasons`; the Q5 award pass consumes exactly this event (writing the `war_champ` **podium** — ranks 1–3 from `standings_top`, carried behavior — plus the single `free_pass` row). |

Tournament bounded streams keep their existing tidy capture (Part I §4) **and
their existing trigger**: the watch is leader-started/stopped via the current
commands (`start_tournament_watch` / `stop_tournament_watch`,
`runtime/jobs/_tournament.py` — a dynamic APScheduler job that resumes on
restart; Q7-style port-and-repoint). Their lifecycle moments —
`tournament_watch_started:{tournament_tag}` and
`tournament_completed:{tournament_tag}` (recap trigger) — are **ledger-claimed
recognition moments computed from the `tournaments` star**, the derived-moment
pattern (§2), not stored events; payloads from the `tournaments` row.

## 6. C2 mapping — old `detection_type` → new home

The complete disposition of every live Gen C detection type (all 25, enumerated
from `detectors.py`), so no tuned behavior is silently dropped. `recognition.md`
keys its ported scores to the **new** names via this table.

| Gen C `detection_type` | v5.1 home | New name |
|---|---|---|
| `player_level_up` | player event | `level_up` |
| `best_trophies_peak` | player event | `best_trophies_peak` |
| `career_wins_milestone` | player event | `career_wins_milestone` |
| `path_of_legend_promotion` | player event | `pol_promotion` |
| `ultimate_champion_reached` | player event | `ultimate_champion_reached` |
| `path_of_legend_global_rank_attained` | player event | `pol_global_rank_attained` |
| `card_level_milestone` | player event | `card_level_milestone` |
| `new_card_unlocked` | player event | `card_unlocked` |
| `new_champion_unlocked` | **dropped** — subsumed by `card_unlocked.rarity` + ledger (double-post fix, `communication.py:38–42`) | — |
| `badge_earned` | player event | `badge_earned` |
| `collection_level_milestone` | player event | `collection_level_milestone` |
| `battle_trophy_push` | derived battle moment | `trophy_push` |
| `battle_hot_streak` | derived battle moment (retired from public, unchanged) | `hot_streak` |
| `ranked_activity_pulse` | derived battle moment | `ranked_pulse` |
| `member_joined` | clan event | `member_joined` |
| `member_left` | clan event | `member_left` |
| `member_promoted` | clan event | `role_changed` (direction=promoted) |
| `war_update` | war clock (pace) + `war_day_opened` (transitions) | split |
| `war_complete` | war event | `week_finished` |
| `new_season` | war event | `season_started` |
| `cohort_wave` | recognition layer (coalescing/cohort) | ledger key `cohort_wave:{type}:{day}` |
| `clan_birthday` / `member_birthday` / `join_anniversary` | clan calendar events | unchanged names |
| `weekly_donation_leader` | clan event | unchanged name |

New in v5.1 with no Gen C ancestor: `arena_up` + `arena_changed` (§11),
`role_changed(demoted)`, `clan_score_milestone`, `clan_league_changed`,
`colosseum_detected`, `race_finished`, `season_closed`.

## 7. What events.md deliberately does not decide

- **Scores, thresholds, coalescing, cohort waves, routing** → `recognition.md`.
- **Emitter scheduling, poll cadence, cursor discipline** → `runtime.md`.
- **Milestone ladders** (which levels/win-counts/boundaries count) — carried from
  the current detectors' constants; `recognition.md` cites them with the scores,
  since a milestone ladder without a score is meaningless.
