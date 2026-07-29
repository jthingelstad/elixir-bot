"""Private migration-0 DDL for :mod:`db.schema`.

This is the frozen clean-break v5.1 baseline: hand-written DDL for tables that
were new or changed at the cut, plus a frozen export for tables carried from
the pre-cut archive. ``db.schema`` owns the public fresh-build API and applies
the ordered post-cut migrations after this baseline.

Consumers must import the builder from ``db.schema`` rather than this private
module. Keeping migration 0 beside the forward ladder gives schema ownership
one home while preserving the exact upgrade path for existing databases.
"""

from __future__ import annotations

import os
import re
import sqlite3

# ------------------------------------------------------------------ new DDL
# Sources: schema.md §3 (identity), §4 (ingest), §5 (streams), §6 (rollups &
# projections), §7 (recognition/war/awards/control), runtime.md §4 (poll_state).

NEW_DDL = """
-- §3 Identity & tenure (L5) --------------------------------------------------
CREATE TABLE players (
    player_tag    TEXT PRIMARY KEY,
    current_name  TEXT,
    display_name  TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE player_metadata (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    joined_at TEXT, birth_month INTEGER, birth_day INTEGER,
    profile_url TEXT DEFAULT '', note TEXT DEFAULT '',
    generated_bio TEXT DEFAULT '', generated_highlight TEXT DEFAULT '',
    generated_profile_updated_at TEXT,
    cr_account_age_days INTEGER, cr_account_age_years INTEGER,
    cr_account_age_updated_at TEXT, cr_games_per_day REAL,
    cr_games_per_day_window_days INTEGER, cr_games_per_day_updated_at TEXT,
    cr_collection_level INTEGER, cr_collection_level_badge_tier INTEGER,
    cr_collection_level_badge_max_tier INTEGER, cr_collection_level_updated_at TEXT,
    cr_clan_war_wins INTEGER, cr_battle_wins INTEGER, cr_clan_donations INTEGER,
    cr_banner_count INTEGER, cr_emote_count INTEGER, cr_profile_badges_updated_at TEXT,
    preferred_nickname TEXT, nickname_source TEXT, nickname_updated_at TEXT,
    email TEXT DEFAULT '', email_verified_at TEXT, email_source TEXT DEFAULT '',
    cr_years_celebrated INTEGER
);

-- Transient email-verification challenges: a member sets an email, Elixir mails a
-- 6-digit code, the member enters it to verify. One active challenge per member
-- (PK), overwritten on re-request; cleared on success/expiry.
CREATE TABLE email_verifications (
    player_tag    TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    pending_email TEXT NOT NULL,
    code_hash     TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE player_aliases (
    alias_id    INTEGER PRIMARY KEY,
    player_tag  TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    alias TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
    UNIQUE(player_tag, alias)
);

-- Evergreen housekeeping nudges (Discord, FAQ, website). Elixir surfaces one
-- as an in-game-relay leader-action card during a quiet period, infrequently
-- and rotated. Inventory + per-item send-state live together.
CREATE TABLE evergreen_nudges (
    nudge_key      TEXT PRIMARY KEY,
    topic          TEXT NOT NULL,
    context        TEXT NOT NULL,          -- prompt Elixir composes clan-chat copy from
    forbidden_terms_json TEXT,             -- extra terms to keep OUT of the copy
    cooldown_days  INTEGER NOT NULL DEFAULT 30,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_sent_at   TEXT,
    send_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE clans (
    clan_tag   TEXT PRIMARY KEY,
    name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    is_home INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE discord_links (
    discord_link_id INTEGER PRIMARY KEY,
    discord_user_id TEXT NOT NULL REFERENCES discord_users(discord_user_id) ON DELETE CASCADE,
    player_tag      TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    linked_at TEXT NOT NULL, source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_primary INTEGER NOT NULL DEFAULT 1,
    UNIQUE(discord_user_id, player_tag)
);

CREATE TABLE clan_memberships (
    membership_id INTEGER PRIMARY KEY,
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    clan_tag   TEXT NOT NULL DEFAULT '#J2RGCRVG' REFERENCES clans(clan_tag),
    joined_at TEXT NOT NULL, left_at TEXT,
    join_source TEXT NOT NULL, leave_source TEXT
);
CREATE INDEX idx_memberships_open ON clan_memberships(player_tag) WHERE left_at IS NULL;

-- §4 Baselines (L2) -----------------------------------------------------------
CREATE TABLE state_baselines (
    entity_kind  TEXT NOT NULL,
    entity_tag   TEXT NOT NULL,
    aspect       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    prev_observed_at TEXT,
    PRIMARY KEY (entity_kind, entity_tag, aspect)
);

-- §5 Event streams (L3) -------------------------------------------------------
CREATE TABLE battle_events (
    dedup_key    TEXT PRIMARY KEY,
    player_tag   TEXT NOT NULL,
    battle_time  TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    battle_type TEXT, opponent_tag TEXT, crowns_for INTEGER, crowns_against INTEGER,
    game_mode_id INTEGER, game_mode_name TEXT, mode_group TEXT, outcome TEXT,
    is_war INTEGER, is_ladder INTEGER, is_ranked INTEGER, is_competitive INTEGER,
    is_special_event INTEGER, trophy_change INTEGER, starting_trophies INTEGER,
    deck_selection TEXT, deck_json TEXT,
    arena_id INTEGER, arena_name TEXT, teammate_tag TEXT, league_number INTEGER,
    is_hosted_match INTEGER, tournament_tag TEXT, event_tag TEXT,
    season_id INTEGER, section_index INTEGER, war_day_index INTEGER
);
CREATE INDEX idx_battle_events_player_time ON battle_events(player_tag, battle_time DESC);
CREATE INDEX idx_battle_events_war ON battle_events(season_id, section_index, player_tag) WHERE is_war = 1;
CREATE INDEX idx_battle_events_time ON battle_events(battle_time);

CREATE TABLE player_events (
    event_id    INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    player_tag  TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,
    evidence_json TEXT,
    payload_json  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_player_events_subject ON player_events(player_tag, observed_at DESC);
CREATE INDEX idx_player_events_type ON player_events(event_type, observed_at DESC);

CREATE TABLE clan_events (
    event_id    INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    clan_tag    TEXT NOT NULL,
    subject_tag TEXT,
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,
    evidence_json TEXT,
    payload_json  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_clan_events_subject ON clan_events(subject_tag, observed_at DESC);
CREATE INDEX idx_clan_events_type ON clan_events(event_type, observed_at DESC);

CREATE TABLE war_events (
    event_id    INTEGER PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    season_id   INTEGER NOT NULL,
    section_index INTEGER,
    observed_at TEXT NOT NULL,
    timing      TEXT NOT NULL DEFAULT 'estimated' CHECK (timing IN ('exact','estimated')),
    window_start TEXT,
    evidence_json TEXT,
    payload_json  TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_war_events_season ON war_events(season_id, section_index, observed_at);
CREATE INDEX idx_war_events_type ON war_events(event_type, observed_at DESC);

CREATE TABLE game_events (
    event_id     INTEGER PRIMARY KEY,
    dedup_key    TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    change_key   TEXT NOT NULL,
    subject_tag  TEXT,
    observed_at  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    backfilled   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_game_events_change ON game_events(change_key, observed_at);
CREATE INDEX idx_game_events_type ON game_events(event_type, observed_at DESC);

-- §6.1 Rollups (L4, durable) — archive shapes re-keyed to tags ---------------
CREATE TABLE player_daily_metrics (
    metric_id INTEGER PRIMARY KEY,
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    metric_date TEXT NOT NULL,
    exp_level INTEGER, trophies INTEGER, best_trophies INTEGER,
    clan_rank INTEGER, donations_week INTEGER, donations_received_week INTEGER,
    last_seen_api TEXT,
    UNIQUE(player_tag, metric_date)
);
CREATE INDEX idx_player_daily_metrics_tag_date ON player_daily_metrics(player_tag, metric_date DESC);

CREATE TABLE player_daily_battle_rollups (
    rollup_id INTEGER PRIMARY KEY,
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    battle_date TEXT NOT NULL,
    mode_group TEXT NOT NULL,
    game_mode_id INTEGER, game_mode_name TEXT,
    battles INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0, draws INTEGER NOT NULL DEFAULT 0,
    crowns_for INTEGER NOT NULL DEFAULT 0, crowns_against INTEGER NOT NULL DEFAULT 0,
    trophy_change_total INTEGER NOT NULL DEFAULT 0,
    first_battle_at TEXT, last_battle_at TEXT,
    captured_battles INTEGER NOT NULL DEFAULT 0,
    expected_battle_delta INTEGER, completeness_ratio REAL,
    is_complete INTEGER NOT NULL DEFAULT 0,
    last_aggregated_at TEXT NOT NULL,
    UNIQUE(player_tag, battle_date, mode_group, game_mode_id)
);
CREATE INDEX idx_player_daily_battle_rollups_tag_date
    ON player_daily_battle_rollups(player_tag, battle_date DESC, mode_group);
CREATE INDEX idx_player_daily_battle_rollups_date
    ON player_daily_battle_rollups(battle_date DESC, mode_group);

CREATE TABLE clan_daily_metrics (          -- carried minus raw_json (L1 owns raw)
    metric_id INTEGER PRIMARY KEY,
    metric_date TEXT NOT NULL,
    clan_tag TEXT NOT NULL,
    clan_name TEXT,
    member_count INTEGER NOT NULL DEFAULT 0,
    open_slots INTEGER NOT NULL DEFAULT 0,
    clan_score INTEGER, clan_war_trophies INTEGER, required_trophies INTEGER,
    donations_per_week_requirement INTEGER, weekly_donations_total INTEGER,
    total_member_trophies INTEGER, avg_member_trophies REAL, top_member_trophies INTEGER,
    joins_today INTEGER NOT NULL DEFAULT 0, leaves_today INTEGER NOT NULL DEFAULT 0,
    net_member_change INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    UNIQUE(clan_tag, metric_date)
);
CREATE INDEX idx_clan_daily_metrics_date ON clan_daily_metrics(metric_date DESC);
CREATE INDEX idx_clan_daily_metrics_clan_date ON clan_daily_metrics(clan_tag, metric_date DESC);

-- §6.2 Projections (L6, disposable) -------------------------------------------
CREATE TABLE player_current_state (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    role TEXT, exp_level INTEGER, trophies INTEGER, best_trophies INTEGER,
    clan_rank INTEGER, previous_clan_rank INTEGER,
    donations_week INTEGER, donations_received_week INTEGER,
    arena_id INTEGER, arena_name TEXT,
    ranked_league INTEGER, ranked_trophies INTEGER,
    current_deck_json TEXT,
    last_seen_api TEXT
);

CREATE TABLE player_card_collection (
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    card_id INTEGER NOT NULL,
    level INTEGER, count INTEGER, star_level INTEGER, evolution_level INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (player_tag, card_id)
);

CREATE TABLE player_recent_form (
    player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    computed_at TEXT NOT NULL, sample_size INTEGER NOT NULL,
    wins INTEGER NOT NULL, losses INTEGER NOT NULL, draws INTEGER NOT NULL,
    current_streak INTEGER NOT NULL DEFAULT 0, current_streak_type TEXT,
    win_rate REAL NOT NULL DEFAULT 0, avg_crown_diff REAL, avg_trophy_change REAL,
    form_label TEXT, summary TEXT,
    PRIMARY KEY (player_tag, scope)
);

-- §6.3 member_management (management.md owns the transition rules) ------------
CREATE TABLE member_management (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    computed_at TEXT NOT NULL,
    week_anchor TEXT NOT NULL,
    tenure_days INTEGER, role TEXT,
    donations_4wk_avg REAL, war_fame_3season_avg REAL,
    war_attendance_rate REAL,
    battle_days_last_28 INTEGER,
    sustained_donor TEXT, war_reliable TEXT, battle_active TEXT,
    promote_state TEXT NOT NULL DEFAULT 'none',
    demote_state  TEXT NOT NULL DEFAULT 'none',
    kick_state    TEXT NOT NULL DEFAULT 'none',
    promote_qualifying_weeks INTEGER NOT NULL DEFAULT 0,
    kick_state_since TEXT,
    state_json TEXT NOT NULL DEFAULT '{}'
);

-- §7.3 revisits (leader_action_recommendations exports verbatim from the
-- archive, then V21 removes its retired case_id compatibility column) ----------
CREATE TABLE revisits (
    revisit_id INTEGER PRIMARY KEY,
    revisit_key TEXT NOT NULL,         -- was signal_key (opaque dedup string)
    created_by_workflow TEXT NOT NULL,
    due_at TEXT NOT NULL,
    rationale TEXT,
    revisited_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(revisit_key, due_at)
);
CREATE INDEX idx_revisits_due ON revisits(due_at ASC, revisited_at);

-- §7.4 Bounded war stream ------------------------------------------------------
CREATE TABLE war_seasons (
    season_id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    colosseum_detected_at TEXT,
    ended_at TEXT,
    final_rank INTEGER, weeks INTEGER,
    war_champ_tag TEXT,
    free_pass_tag TEXT,
    recap_json TEXT
);

CREATE TABLE war_weeks (
    season_id INTEGER NOT NULL REFERENCES war_seasons(season_id),
    section_index INTEGER NOT NULL,
    period_type TEXT,
    created_date TEXT, finish_time TEXT,
    our_rank INTEGER, our_fame INTEGER, trophy_change INTEGER, our_clan_score INTEGER,
    -- Boat-defense fame for the week (API periodLogs.progressEarnedFromDefenses,
    -- summed); placement fame = our_fame - defense_fame.
    defense_fame INTEGER,
    thread_id INTEGER,
    PRIMARY KEY (season_id, section_index)
);

CREATE TABLE war_week_clans (
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    clan_tag TEXT NOT NULL REFERENCES clans(clan_tag),
    fame INTEGER, rank INTEGER, clan_score INTEGER, completed_at TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, clan_tag)
);

CREATE TABLE war_participation (
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    player_tag TEXT NOT NULL,
    fame INTEGER, repair_points INTEGER, boat_attacks INTEGER,
    decks_used INTEGER, decks_used_today INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, player_tag)
);

CREATE TABLE war_attendance_days (
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL,
    war_day_index INTEGER NOT NULL,
    player_tag TEXT NOT NULL,
    decks_used INTEGER NOT NULL DEFAULT 0, decks_available INTEGER NOT NULL DEFAULT 4,
    fame_delta INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (season_id, section_index, war_day_index, player_tag)
);

-- §7.5 Awards (durable; war_champ is a podium — ranks 1–3) ---------------------
CREATE TABLE awards (
    award_id INTEGER PRIMARY KEY,
    award_type TEXT NOT NULL,
    season_id INTEGER NOT NULL,
    section_index INTEGER NOT NULL DEFAULT -1,
    player_tag TEXT NOT NULL REFERENCES players(player_tag),
    rank INTEGER NOT NULL DEFAULT 1,
    metric_value REAL, metric_unit TEXT, metadata_json TEXT,
    awarded_at TEXT NOT NULL,
    UNIQUE(award_type, season_id, section_index, player_tag)
);
CREATE INDEX idx_awards_season ON awards(season_id, award_type);
CREATE INDEX idx_awards_player ON awards(player_tag, award_type);

-- §7.6 Engine control -----------------------------------------------------------
CREATE TABLE stream_cursors (
    consumer_key TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    cursor_text TEXT,
    cursor_int INTEGER,
    updated_at TEXT NOT NULL,
    metadata_json TEXT,
    PRIMARY KEY (consumer_key, scope_key)
);

CREATE TABLE poll_state (
    player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
    temperature TEXT NOT NULL DEFAULT 'cold' CHECK (temperature IN ('hot','warm','cold')),
    heat INTEGER NOT NULL DEFAULT 0,
    last_battlelog_poll TEXT, last_profile_poll TEXT,
    last_battle_seen TEXT, last_roster_delta TEXT,
    updated_at TEXT NOT NULL
);

-- tournament_participants: carried minus member_id (player_tag already present) --
CREATE TABLE tournament_participants (
    participant_id INTEGER PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(tournament_id) ON DELETE CASCADE,
    player_tag TEXT NOT NULL,
    player_name TEXT,
    clan_tag TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    final_score INTEGER,
    final_rank INTEGER,
    UNIQUE(tournament_id, player_tag)
);
CREATE INDEX idx_tournament_participants_tournament ON tournament_participants(tournament_id);

-- tick_history: Observatory-persisted engine tick counters (added 2026-07-04;
-- 30-day self-pruning — see runtime/webapp/ticks.py)
CREATE TABLE tick_history (
    tick_id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    counters_json TEXT NOT NULL
);

-- runtime_incidents: the fail-soft-but-visible ledger (confidence plan §1;
-- added 2026-07-05 — lazily created live by storage/incidents.py)
CREATE TABLE runtime_incidents (
    incident_id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('warn','error')),
    summary TEXT NOT NULL,
    detail TEXT,
    context_json TEXT,
    resolved_at TEXT
);
CREATE INDEX idx_incidents_open ON runtime_incidents(resolved_at, at DESC);
CREATE INDEX idx_incidents_component ON runtime_incidents(component, at DESC);

-- Ranked season lifecycle (ranked-and-profiles.md §2.1; D1–D7 ratified
-- 2026-07-04). Ids are canonical 'YYYY-MM' (the month the season starts in;
-- first-Monday to first-Monday). Discovered lifecycle, war-tracker style.
CREATE TABLE pol_seasons (
    pol_season_id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pol_season_results (
    pol_season_id TEXT NOT NULL REFERENCES pol_seasons(pol_season_id),
    player_tag TEXT NOT NULL,
    league INTEGER, rating INTEGER, global_rank INTEGER,
    battles INTEGER, wins INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (pol_season_id, player_tag)
);

-- v5.1 memory system (memory.md §2.2; D1–D5 ratified 2026-07-04) -------------
CREATE TABLE memories (
    memory_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN
        ('leader_note','inference','system','synthesis','conversation_digest')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    summary TEXT,
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
    confidence REAL NOT NULL DEFAULT 0.9,
    member_tag TEXT,
    channel_key TEXT,
    source_event_key TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    retired_at TEXT
);
CREATE INDEX idx_memories_member ON memories(member_tag, updated_at DESC);
CREATE INDEX idx_memories_kind ON memories(kind, updated_at DESC);
CREATE INDEX idx_memories_event ON memories(source_event_key);

CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
CREATE INDEX idx_memory_tags_tag ON memory_tags(tag);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    title, summary, body,
    content='memories',
    content_rowid='memory_id'
);
CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, summary, body)
    VALUES (new.memory_id, new.title, new.summary, new.body);
END;
CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, summary, body)
    VALUES('delete', old.memory_id, old.title, old.summary, old.body);
END;
CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, summary, body)
    VALUES('delete', old.memory_id, old.title, old.summary, old.body);
    INSERT INTO memories_fts(rowid, title, summary, body)
    VALUES (new.memory_id, new.title, new.summary, new.body);
END;
"""

# Tables whose DDL exports verbatim from the archive (schema.md: "carried
# as-is, verified live DDL").
CARRIED_VERBATIM = [
    "raw_api_payloads",
    "discord_users",
    "leader_action_recommendations",
    "llm_calls",
    "prompt_failures",
    "prompt_feedback",
    "system_signals",
    "api_sentinel_observations",
    "arena_relay_screenshot_observations",
    "discord_channels",
    "channel_state",
    "game_mode_contexts",
    "card_catalog",
    "elixir_improvement_suggestions",
    "runtime_job_status",
    "tournaments",
    # Conversation tables carried from the archive; durable memories are
    # defined in NEW_DDL and share the operational database.
    # conversation_threads/messages carry a member_id column whose FK target
    # (members) no longer exists — the export strips the dead FK clause and the
    # column becomes a soft ref for the deferred memory/conversation pass.
    "conversation_threads",
    "messages",
    # memory_facts retired (memory.md M3): dead since 2026-06-16, rolled up
    "memory_episodes",
]

_DEAD_MEMBERS_FK = re.compile(
    r"\s+REFERENCES members\(member_id\)(\s+ON DELETE (SET NULL|CASCADE))?"
)


def export_carried_ddl(archive: sqlite3.Connection) -> list[str]:
    stmts: list[str] = []
    for table in CARRIED_VERBATIM:
        rows = archive.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL "
            "AND type IN ('table','index') ORDER BY type DESC",  # table before its indexes
            (table,),
        ).fetchall()
        if not rows:
            raise SystemExit(f"carried table missing from archive: {table}")
        # Strip FK clauses that point at the dropped members table (§7 re-key):
        # the member_id columns stay as soft refs for the deferred pass.
        stmts.extend(_DEAD_MEMBERS_FK.sub("", r[0]) for r in rows)
    return stmts


CARRIED_DDL_FILE = os.path.join(os.path.dirname(__file__), "_schema_baseline.sql")


def carried_ddl(archive_path: str | None) -> list[str]:
    """Prefer the live archive when present; fall back to the frozen export
    (_schema_baseline.sql) so CI and archive-less checkouts can build. The archive
    is immutable, so the two sources can never legitimately diverge — when
    both are available we assert exactly that."""
    frozen = None
    if os.path.exists(CARRIED_DDL_FILE):
        with open(CARRIED_DDL_FILE) as f:
            body = "".join(line for line in f if not line.startswith("--"))
        frozen = [s.strip() for s in body.split(";\n") if s.strip()]
    if archive_path and os.path.exists(archive_path):
        archive = sqlite3.connect(f"file:{archive_path}?immutable=1", uri=True)
        try:
            live = export_carried_ddl(archive)
        finally:
            archive.close()
        if frozen is not None and [s.strip() for s in live] != frozen:
            raise SystemExit(
                "_schema_baseline.sql has drifted from the archive export — regenerate it"
            )
        return live
    if frozen is None:
        raise SystemExit(
            f"need either the archive ({archive_path}) or {CARRIED_DDL_FILE} to build the schema"
        )
    return frozen


def create_baseline_schema(conn: sqlite3.Connection, archive_path: str | None = None) -> None:
    """Create migration 0 on an empty connection.

    ``db.schema`` is responsible for advancing this frozen baseline through
    the ordered migrations and validating the final contract.
    """
    for statement in carried_ddl(archive_path):
        conn.execute(statement)
    conn.executescript(NEW_DDL)


__all__ = ["create_baseline_schema"]
