# Data Model V2

This document defines the new SQLite schema for Elixir as a breaking change.

Assumptions:
- Existing `elixir.db` data will be discarded.
- We are optimizing for correct retrieval and analytics, not migration compatibility.
- Official Clash Royale API is the operational source of truth.
- Discord-provided data is curated metadata layered on top of CR API facts.

## Goals

Elixir should be able to answer questions like:

- "List everyone in the clan with level and join date."
- "What deck is King Levy running right now?"
- "What cards does Vijay use most?"
- "Who has lost 8 of their last 10 matches?"
- "Who used all 4 war decks today?"
- "Who looks ready for Elder?"
- "Who just leveled up?"
- "Who upgraded a card to level 16?"

The current schema does not support these well because it stores sparse snapshots and makes the model reconstruct facts indirectly.

V2 fixes that by separating:

- raw API ingest
- normalized current state
- normalized historical facts
- Discord-curated metadata
- Discord identity and conversational memory
- derived analytics

## Source Of Truth

### Official CR API

Use these endpoints as the primary factual source:

- `Clan`
- `Player`
- `Player Battles`
- `Player Chests`
- `Clan War`
- `Clan War Log`
- `Cards`

Optional supporting endpoints:

- `Locations`
- `Top Clans`
- `Top Players`
- `Top War Clans`

### Discord / Human Metadata

Use Discord inputs for facts CR API does not know:

- birthday
- join date override
- profile URL
- POAP wallet address
- note / title
- Discord account linkage
- nickname aliases if needed

### Derived Analytics

These should never be hand-authored:

- recent form
- streaks
- war readiness
- war attendance
- donation leaders
- promotion candidates
- deck / card signatures
- progression milestones

## Design Rules

1. Use one canonical internal key: `member_id`.
2. Preserve the CR player tag as a unique natural key.
3. Persist raw payloads before or alongside normalization.
4. Normalize only what Elixir must query often.
5. Keep large, low-value payload sections in JSON when full normalization does not help.
6. Precompute common analytics so the tools can answer quickly and deterministically.
7. Do not make the LLM reconstruct facts from giant roster context.
8. Treat Discord identity and conversation history as first-class data.
9. Do not fabricate join dates on a fresh reset; bootstrap roster observations are not authoritative tenure facts.

## ERD

For a schema-reference-only version of the implemented diagram, see [data-model-v2-erd.md](/Users/jamie/Projects/elixir-bot/docs/data-model-v2-erd.md).

```mermaid
erDiagram
    members {
        integer member_id PK
        text player_tag UNIQUE
        text current_name
        text status
        text first_seen_at
        text last_seen_at
    }

    member_metadata {
        integer member_id PK, FK
        text joined_at_override
        integer birth_month
        integer birth_day
        text profile_url
        text poap_address
        text note
    }

    member_aliases {
        integer alias_id PK
        integer member_id FK
        text alias
        text source
        text observed_at
    }

    discord_users {
        integer discord_user_id PK
        text username
        text global_name
        text display_name
        text first_seen_at
        text last_seen_at
    }

    discord_links {
        integer discord_link_id PK
        integer discord_user_id FK
        integer member_id FK
        text discord_username
        text discord_display_name
        text linked_at
        text source
        real confidence
        integer is_primary
    }

    discord_channels {
        integer channel_id PK
        text channel_name
        text channel_kind
        text first_seen_at
        text last_seen_at
    }

    conversation_threads {
        integer thread_id PK
        text scope_type
        text scope_key
        integer channel_id FK
        integer discord_user_id FK
        integer member_id FK
        text created_at
        text last_active_at
    }

    messages {
        integer message_id PK
        text discord_message_id UNIQUE
        integer thread_id FK
        integer channel_id FK
        integer discord_user_id FK
        integer member_id FK
        text author_type
        text workflow
        text event_type
        text content
        text summary
        text created_at
        text raw_json
    }

    memory_facts {
        integer fact_id PK
        text subject_type
        text subject_key
        text fact_type
        text fact_value
        real confidence
        integer source_message_id FK
        text created_at
        text updated_at
        text expires_at
    }

    memory_episodes {
        integer episode_id PK
        text subject_type
        text subject_key
        text episode_type
        text summary
        integer importance
        text source_message_ids_json
        text created_at
    }

    channel_state {
        integer channel_id PK, FK
        text last_elixir_post_at
        text last_topics_json
        text recent_style_notes_json
        text last_summary
    }

    clan_memberships {
        integer membership_id PK
        integer member_id FK
        text joined_at
        text left_at
        text join_source
        text leave_source
    }

    member_current_state {
        integer member_id PK, FK
        text observed_at
        text role
        integer exp_level
        integer trophies
        integer best_trophies
        integer clan_rank
        integer previous_clan_rank
        integer donations_week
        integer donations_received_week
        integer arena_id
        text arena_name
        text arena_raw_name
        text last_seen_api
        text source
        text raw_json
    }

    member_daily_metrics {
        integer metric_id PK
        integer member_id FK
        text metric_date
        integer exp_level
        integer trophies
        integer best_trophies
        integer clan_rank
        integer donations_week
        integer donations_received_week
        text last_seen_api
    }

    player_profile_snapshots {
        integer snapshot_id PK
        integer member_id FK
        text fetched_at
        integer exp_level
        integer trophies
        integer best_trophies
        integer wins
        integer losses
        integer battle_count
        integer total_donations
        integer donations
        integer donations_received
        integer war_day_wins
        integer challenge_max_wins
        integer challenge_cards_won
        integer tournament_battle_count
        integer tournament_cards_won
        integer three_crown_wins
        integer current_favourite_card_id
        text current_favourite_card_name
        text league_statistics_json
        text current_deck_json
        text cards_json
        text badges_json
        text achievements_json
        text raw_json
    }

    member_card_collection_snapshots {
        integer snapshot_id PK
        integer member_id FK
        text fetched_at
        text cards_json
    }

    member_deck_snapshots {
        integer snapshot_id PK
        integer member_id FK
        text fetched_at
        text source
        text mode_scope
        text deck_hash
        text deck_json
        integer sample_size
    }

    member_card_usage_snapshots {
        integer snapshot_id PK
        integer member_id FK
        text fetched_at
        text source
        text mode_scope
        integer sample_battles
        text cards_json
    }

    member_battle_facts {
        integer battle_fact_id PK
        integer member_id FK
        text battle_time
        text battle_type
        text game_mode_name
        integer game_mode_id
        text deck_selection
        integer arena_id
        text arena_name
        integer crowns_for
        integer crowns_against
        text outcome
        integer trophy_change
        integer starting_trophies
        integer is_competitive
        integer is_ladder
        integer is_ranked
        integer is_war
        integer is_special_event
        text deck_json
        text support_cards_json
        text opponent_name
        text opponent_tag
        text opponent_clan_tag
        text raw_json
    }

    member_recent_form {
        integer form_id PK
        integer member_id FK
        text computed_at
        text scope
        integer sample_size
        integer wins
        integer losses
        integer draws
        integer current_streak
        text current_streak_type
        real win_rate
        real avg_crown_diff
        real avg_trophy_change
        text form_label
        text summary
    }

    war_current_state {
        integer war_id PK
        text observed_at
        text war_state
        text clan_tag
        text clan_name
        integer fame
        integer repair_points
        integer period_points
        integer clan_score
        text raw_json
    }

    war_day_status {
        integer status_id PK
        integer member_id FK
        text battle_date
        text observed_at
        integer fame
        integer repair_points
        integer boat_attacks
        integer decks_used_total
        integer decks_used_today
        text raw_json
    }

    war_races {
        integer war_race_id PK
        integer season_id
        integer section_index
        text created_date
        integer our_rank
        integer trophy_change
        integer our_fame
        integer total_clans
        text finish_time
        text raw_json
    }

    war_participation {
        integer participation_id PK
        integer war_race_id FK
        integer member_id FK
        text player_tag
        text player_name
        integer fame
        integer repair_points
        integer boat_attacks
        integer decks_used
        integer decks_used_today
        text raw_json
    }

    raw_api_payloads {
        integer payload_id PK
        text endpoint
        text entity_key
        text fetched_at
        text payload_hash
        text payload_json
    }

    conversations {
        integer id PK
        text scope
        text role
        text author_name
        text content
        text recorded_at
    }

    members ||--|| member_metadata : has
    members ||--o{ member_aliases : has
    discord_users ||--o{ discord_links : linked
    members ||--o{ discord_links : linked
    discord_channels ||--o{ conversation_threads : contains
    discord_users ||--o{ conversation_threads : participates
    members ||--o{ clan_memberships : has
    members ||--|| member_current_state : current
    members ||--o{ member_daily_metrics : rolls_up
    members ||--o{ player_profile_snapshots : snapshots
    members ||--o{ member_card_collection_snapshots : snapshots
    members ||--o{ member_deck_snapshots : snapshots
    members ||--o{ member_card_usage_snapshots : snapshots
    members ||--o{ member_battle_facts : has
    members ||--o{ member_recent_form : derives
    members ||--o{ war_day_status : tracks
    members ||--o{ war_participation : participates
    conversation_threads ||--o{ messages : contains
    discord_channels ||--o{ messages : contains
    discord_users ||--o{ messages : authors
    members ||--o{ messages : relates
    messages ||--o{ memory_facts : sources
    discord_channels ||--|| channel_state : tracks
    war_races ||--o{ war_participation : has
```

## Table Definitions

### `members`

Canonical player identity.

Columns:
- `member_id`
- `player_tag`
- `current_name`
- `status` - `active`, `left`, `unknown`
- `first_seen_at`
- `last_seen_at`

Notes:
- `player_tag` should be stored in canonical form with `#`.
- Every other table joins through `member_id`.

### `member_metadata`

Human-curated data that CR API does not provide.

Columns:
- `member_id`
- `joined_at_override`
- `birth_month`
- `birth_day`
- `profile_url`
- `poap_address`
- `note`

Notes:
- Keep override fields separate from observed facts.
- Effective join date is derived from override or membership history.

### `member_aliases`

Observed name history and optional nickname mapping support.

Columns:
- `alias_id`
- `member_id`
- `alias`
- `source` - `clan_api`, `discord`, `manual`
- `observed_at`

### `discord_users`

Canonical Discord identity table.

Columns:
- `discord_user_id`
- `username`
- `global_name`
- `display_name`
- `first_seen_at`
- `last_seen_at`

Use cases:
- direct mentions
- display formatting
- linking clan members to Discord users
- long-term user memory

### `discord_links`

Mapping between Discord users and clan members.

Columns:
- `discord_link_id`
- `discord_user_id`
- `member_id`
- `discord_username`
- `discord_display_name`
- `linked_at`
- `source`
- `confidence`
- `is_primary`

Recommended `source` values:
- `manual_link`
- `verified_nickname_match`
- `leader_override`
- `alias_match`

Notes:
- This is the table that lets Elixir say `King Levy (@jamie)` or `King Levy (<@123...>)`.
- Keep link confidence explicit so low-confidence automatic matches are not treated as certain.

### `discord_channels`

Known Discord channels Elixir interacts in.

Columns:
- `channel_id`
- `channel_name`
- `channel_kind`
- `first_seen_at`
- `last_seen_at`

Use cases:
- channel-scoped memory
- anti-repetition
- workflow routing context

### `conversation_threads`

Logical conversation scopes.

Columns:
- `thread_id`
- `scope_type` - `leader`, `reception`, `channel`, `member`, `dm`
- `scope_key`
- `channel_id`
- `discord_user_id`
- `member_id`
- `created_at`
- `last_active_at`

Use cases:
- stable retrieval of relevant turns
- per-user or per-channel conversational continuity

### `messages`

Full conversation and message log for users and Elixir.

Columns:
- `message_id`
- `discord_message_id`
- `thread_id`
- `channel_id`
- `discord_user_id`
- `member_id`
- `author_type` - `user`, `assistant`, `system`
- `workflow`
- `event_type`
- `content`
- `summary`
- `created_at`
- `raw_json`

Use cases:
- retrieve recent turns
- avoid repeating prior answers
- build durable memory facts and episodes

### `memory_facts`

Durable structured memory about users, members, or channels.

Columns:
- `fact_id`
- `subject_type` - `discord_user`, `member`, `channel`
- `subject_key`
- `fact_type`
- `fact_value`
- `confidence`
- `source_message_id`
- `created_at`
- `updated_at`
- `expires_at`

Examples:
- a Discord user is linked to a member
- a leader often asks about promotions
- a member prefers short answers
- a channel recently focused on war prep

### `memory_episodes`

Summarized episodic memory built from conversations.

Columns:
- `episode_id`
- `subject_type`
- `subject_key`
- `episode_type`
- `summary`
- `importance`
- `source_message_ids_json`
- `created_at`

Use cases:
- carry forward important past conversations
- preserve context without replaying all raw turns

### `channel_state`

Rolling memory for each channel Elixir speaks in.

Columns:
- `channel_id`
- `last_elixir_post_at`
- `last_topics_json`
- `recent_style_notes_json`
- `last_summary`

Use cases:
- prevent repetitive posting patterns
- keep channel-specific continuity
- give Elixir lightweight broadcast memory

### `clan_memberships`

Tracks join and leave cycles. This is the durable tenure source.

Columns:
- `membership_id`
- `member_id`
- `joined_at`
- `left_at`
- `join_source`
- `leave_source`

Notes:
- If a member leaves and rejoins, create a new row.
- Current membership is `left_at IS NULL`.

### `member_current_state`

Fast path for direct factual queries.

Columns:
- `member_id`
- `observed_at`
- `role`
- `exp_level`
- `trophies`
- `best_trophies`
- `clan_rank`
- `previous_clan_rank`
- `donations_week`
- `donations_received_week`
- `arena_id`
- `arena_name`
- `arena_raw_name`
- `last_seen_api`
- `source`
- `raw_json`

Use cases:
- list all clan members
- answer current level, role, trophies, join status
- power leaderboard and promotion review

### `member_daily_metrics`

Daily rollup of current-state fields for long-term trend analysis without storing every poll forever.

Columns:
- `metric_id`
- `member_id`
- `metric_date`
- `exp_level`
- `trophies`
- `best_trophies`
- `clan_rank`
- `donations_week`
- `donations_received_week`
- `last_seen_api`

Use cases:
- trophy progress over time
- donation consistency
- rank movement
- level-up detection

### `player_profile_snapshots`

Snapshot of the `Player` endpoint. Keep large semi-structured sections in JSON.

Columns:
- `snapshot_id`
- `member_id`
- `fetched_at`
- scalar profile stats
- `current_favourite_card_id`
- `current_favourite_card_name`
- `league_statistics_json`
- `current_deck_json`
- `cards_json`
- `badges_json`
- `achievements_json`
- `raw_json`

Use cases:
- current deck
- lifetime wins/losses
- card-level milestone detection
- season performance summaries

### `member_card_collection_snapshots`

Optional extracted storage for card progression diffs.

Columns:
- `snapshot_id`
- `member_id`
- `fetched_at`
- `cards_json`

Use cases:
- detect "upgraded a card to level 16"
- detect new evo unlocks

This table may be redundant with `player_profile_snapshots.cards_json`, but it makes progression-only diffing simpler and cheaper.

### `member_deck_snapshots`

Current or recent deck states derived from profile and battle log.

Columns:
- `snapshot_id`
- `member_id`
- `fetched_at`
- `source` - `player_profile`, `battle_log`
- `mode_scope` - `overall`, `ladder`, `ranked`, `war`, `event`
- `deck_hash`
- `deck_json`
- `sample_size`

Use cases:
- current deck answers
- deck change detection
- "what deck have they been running lately?"

### `member_card_usage_snapshots`

Derived signature card view from recent battle logs.

Columns:
- `snapshot_id`
- `member_id`
- `fetched_at`
- `source`
- `mode_scope`
- `sample_battles`
- `cards_json`

`cards_json` should store a compact summary:

```json
[
  {"id": 26000011, "name": "Valkyrie", "usage_pct": 70},
  {"id": 26000056, "name": "Skeleton Barrel", "usage_pct": 60}
]
```

Use cases:
- signature cards
- playstyle summaries
- roster bios

### `member_battle_facts`

One row per observed battle for one member.

Columns:
- `battle_fact_id`
- `member_id`
- `battle_time`
- `battle_type`
- `game_mode_name`
- `game_mode_id`
- `deck_selection`
- `arena_id`
- `arena_name`
- `crowns_for`
- `crowns_against`
- `outcome` - `W`, `L`, `D`
- `trophy_change`
- `starting_trophies`
- `is_competitive`
- `is_ladder`
- `is_ranked`
- `is_war`
- `is_special_event`
- `deck_json`
- `support_cards_json`
- `opponent_name`
- `opponent_tag`
- `opponent_clan_tag`
- `raw_json`

Use cases:
- last 10 record
- streaks
- recent slumps
- war battle tracking
- deck analysis by mode

Notes:
- Deduplicate on `(member_id, battle_time, battle_type, opponent_tag, crowns_for, crowns_against)`.
- Mode classification should happen in code, not ad hoc in prompts.

### `member_recent_form`

Precomputed analytics built from `member_battle_facts`.

Columns:
- `form_id`
- `member_id`
- `computed_at`
- `scope` - examples: `overall_10`, `ladder_10`, `ranked_10`, `war_10`, `overall_25`
- `sample_size`
- `wins`
- `losses`
- `draws`
- `current_streak`
- `current_streak_type` - `win`, `loss`, `draw`
- `win_rate`
- `avg_crown_diff`
- `avg_trophy_change`
- `form_label`
- `summary`

Suggested `form_label` values:
- `hot`
- `strong`
- `mixed`
- `slumping`
- `cold`
- `inactive`
- `war_focused`
- `event_focused`

This is the right place for "lost 8 of last 10" style intelligence.

### `war_current_state`

Current war snapshot from `Clan War`.

Columns:
- `war_id`
- `observed_at`
- `war_state`
- `clan_tag`
- `clan_name`
- `fame`
- `repair_points`
- `period_points`
- `clan_score`
- `raw_json`

### `war_day_status`

Per-member daily war participation facts.

Columns:
- `status_id`
- `member_id`
- `battle_date`
- `observed_at`
- `fame`
- `repair_points`
- `boat_attacks`
- `decks_used_total`
- `decks_used_today`
- `raw_json`

Use cases:
- "who still has decks left today?"
- perfect 4/4 today
- season-long usage consistency

### `war_races`

Historical race summary from `Clan War Log`.

Columns:
- `war_race_id`
- `season_id`
- `section_index`
- `created_date`
- `our_rank`
- `trophy_change`
- `our_fame`
- `total_clans`
- `finish_time`
- `raw_json`

### `war_participation`

Historical member contribution per race.

Columns:
- `participation_id`
- `war_race_id`
- `member_id`
- `player_tag`
- `player_name`
- `fame`
- `repair_points`
- `boat_attacks`
- `decks_used`
- `decks_used_today`
- `raw_json`

Use cases:
- War Champ
- perfect participation
- season race contribution totals
- Elder recommendations

### `raw_api_payloads`

Generic raw ingest table.

Columns:
- `payload_id`
- `endpoint`
- `entity_key`
- `fetched_at`
- `payload_hash`
- `payload_json`

Examples:
- `endpoint = clan`
- `entity_key = #J2RGCRVG`
- `endpoint = player`
- `entity_key = #U8RYG9Y2U`
- `endpoint = player_battlelog`
- `entity_key = #U8RYG9Y2U`

Use cases:
- debugging normalization bugs
- schema change resilience
- audit trail

## Discord Identity And Memory

Elixir should know:

- every Discord user it has interacted with
- which clan member that Discord user maps to, if any
- what it has said to them before
- what has already been said in each channel
- important prior episodes worth carrying forward

This is separate from CR API facts.

### Why This Layer Exists

Without a Discord memory layer, Elixir:

- repeats itself
- loses user-specific context
- cannot reference Discord identities reliably
- cannot gracefully continue prior discussions
- cannot tailor channel output to avoid repetitive patterns

### Memory Categories

1. Identity memory
- Discord user identity
- member linkage
- aliases

2. Episodic memory
- what was discussed
- promises or recommendations made
- unresolved asks

3. Channel memory
- what Elixir has recently posted
- recent topics and framing
- anti-repetition support

4. Durable preference memory
- user likes brief answers
- leader focuses on promotions
- member often asks for deck help

### Suggested Retention

- raw `messages`: 30-90 days
- `memory_episodes`: keep indefinitely unless low-importance
- `memory_facts`: keep indefinitely or until expiry
- `channel_state`: rolling, always current

## Member Reference Formatting

When Elixir refers to a clan member who is also in Discord, it should be able to include the Discord identity.

Preferred output forms:

- plain name: `King Levy`
- name with handle: `King Levy (@jamie)`
- name with mention: `King Levy (<@1474760692992180429>)`

This should be driven by code and the `discord_links` table, not improvised by the model.

### Suggested Rules

- `#leader-lounge`: default to `name_with_handle`
- `#reception`: use direct mention when addressing the user
- `#elixir`: use `plain_name` or `name_with_handle`; avoid unnecessary hard mentions
- direct replies to a user: use `name_with_mention` when helpful

### Tool Contract Requirement

Any tool returning member identities should include Discord linkage fields when available:

- `member_name`
- `player_tag`
- `discord_user_id`
- `discord_username`
- `discord_display_name`
- `in_discord`

That lets the application layer render names consistently.

## What Not To Fully Normalize Yet

Keep these inside JSON unless a strong query need appears:

- full badge progress history
- full achievement progress history
- full card collection relational expansion
- location rankings history

The only card collection facts worth first-class handling now are milestone diffs:

- reached level 14 / 15 / 16
- unlocked evolution
- favorite card changed

## Indices

Minimum useful indices:

- `members(player_tag)`
- `members(status)`
- `member_current_state(role, clan_rank)`
- `member_daily_metrics(member_id, metric_date)`
- `player_profile_snapshots(member_id, fetched_at)`
- `member_battle_facts(member_id, battle_time)`
- `member_battle_facts(member_id, is_competitive, battle_time)`
- `member_battle_facts(member_id, is_war, battle_time)`
- `member_recent_form(member_id, scope, computed_at)`
- `clan_memberships(member_id, left_at)`
- `war_day_status(member_id, battle_date)`
- `war_races(season_id, section_index)`
- `war_participation(member_id, war_race_id)`
- `raw_api_payloads(endpoint, entity_key, fetched_at)`

## Ingestion Flow

### Hourly

1. Fetch `Clan`
2. Upsert `members`
3. Upsert `member_current_state`
4. Insert or update current membership rows
5. Write `member_daily_metrics` once per day
6. Detect join/leave, role, trophy, level changes

### Scheduled Player Refresh

For active clan members:

1. Fetch `Player`
2. Store `raw_api_payloads`
3. Insert `player_profile_snapshots`
4. Derive and insert `member_deck_snapshots`
5. Diff `cards_json` to detect collection milestones

Cadence:
- leaders and active members more often
- inactive members less often

### Scheduled Battle Refresh

For active clan members:

1. Fetch `Player Battles`
2. Store `raw_api_payloads`
3. Insert new `member_battle_facts`
4. Recompute `member_card_usage_snapshots`
5. Recompute `member_recent_form`

### War Refresh

1. Fetch `Clan War`
2. Upsert `war_current_state`
3. Insert per-member `war_day_status`
4. Fetch `Clan War Log`
5. Insert `war_races`
6. Insert `war_participation`

## Signals Enabled By V2

### Current-State Signals

- member joined
- member left
- role changed
- trophies crossed threshold
- best trophies increased
- player leveled up
- current deck changed
- favorite card changed

### Progression Signals

- card reached level 14
- card reached level 15
- card reached level 16
- card evolution unlocked

### Performance Signals

- won 7 of last 10
- lost 8 of last 10
- on 4-game win streak
- on rough ladder run
- crushing war battles this week

### War Signals

- used all 4 decks today
- still has decks left
- perfect weekly usage
- War Champ lead changed

## Tool Design Implications

The tool layer should query this schema directly.

### Core Identity Tools

- `resolve_member(name_or_tag)`
- `list_members(status='active', sort='clan_rank')`
- `get_member_profile(member_tag)`
- `get_discord_link(member_tag)`
- `resolve_discord_user(discord_user_id)`

### Member Fact Tools

- `get_member_current_state(member_tag)`
- `get_member_current_deck(member_tag, mode_scope='overall')`
- `get_member_signature_cards(member_tag, mode_scope='overall')`
- `get_member_collection_milestones(member_tag, since_days=30)`
- `get_member_recent_battles(member_tag, count=10, scope='competitive')`
- `get_member_recent_form(member_tag, scope='overall_10')`
- `get_member_next_chests(member_tag)`

### Clan Tools

- `list_members_with_levels_and_join_dates()`
- `get_donation_leaders()`
- `get_recent_level_ups()`
- `get_recent_card_upgrades()`

### War Tools

- `get_war_deck_status_today()`
- `get_member_war_status(member_tag)`
- `get_member_war_attendance(member_tag, season_id=null)`
- `get_war_champ_standings(season_id=null)`
- `get_perfect_war_participants(season_id=null)`

### Decision Tools

- `get_promotion_summary(member_tag)`
- `list_promotion_candidates()`

### Memory Tools

- `get_recent_thread_context(scope_type, scope_key, limit=10)`
- `get_recent_channel_context(channel_id, limit=20)`
- `get_user_memory(discord_user_id)`
- `get_member_memory(member_tag)`
- `save_memory_fact(...)`
- `save_memory_episode(...)`

## V2 Outcome

The V2 reset is implemented.

1. `elixir.db` is treated as disposable runtime state.
2. The baseline schema lives in `db/__init__.py` as `_migration_0()`.
3. Storage is now exposed through the `db` package, with domain code in `storage/`.
4. Discord identity and memory are part of the baseline schema.
5. Generic history lookups were replaced with narrower roster, member, war, and analytics queries.
6. Heartbeat, site generation, and channel workflows now read from normalized V2 tables.
7. Test coverage exists around tag normalization, Discord linkage, roster queries, war analytics, and channel routing.
   - join / leave membership tracking
   - battle dedup
   - form calculation
   - card milestone detection
   - war deck usage status
   - member reference formatting

## Open Questions

These should be answered during implementation:

- How often should player profiles be refreshed for all members?
- How aggressively should battle logs be polled to avoid missing events?
- Which battle types count toward "recent form" by default?
- Do we want separate form scopes for `ladder`, `ranked`, `war`, and `special_event`?
- Should Discord users be allowed to query only their own detailed form, or any clan member?
- Do we want a materialized "member_summary" view for very fast tool responses?

## Recommendation

Start implementation with this slice:

1. `members`
2. `member_metadata`
3. `clan_memberships`
4. `member_current_state`
5. `player_profile_snapshots`
6. `member_battle_facts`
7. `member_recent_form`
8. `war_day_status`
9. `war_races`
10. `war_participation`

That is the minimum schema that unlocks leader intelligence, member intelligence, war intelligence, and progression signals.
