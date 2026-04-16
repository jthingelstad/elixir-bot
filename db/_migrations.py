"""db._migrations — Schema migrations for the Elixir SQLite database.

This module is an internal implementation detail. All symbols are re-exported
by the ``db`` package so that existing imports continue to work.
"""

from __future__ import annotations

import json
import sqlite3

from db import _existing_tables, _table_columns, _trusted_current_joined_at, _utcnow


def _migration_0(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS members (
            member_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_tag TEXT NOT NULL UNIQUE,
            current_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_metadata (
            member_id INTEGER PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
            joined_at TEXT,
            birth_month INTEGER,
            birth_day INTEGER,
            cr_account_age_days INTEGER,
            cr_account_age_years INTEGER,
            cr_account_age_updated_at TEXT,
            cr_games_per_day REAL,
            cr_games_per_day_window_days INTEGER,
            cr_games_per_day_updated_at TEXT,
            profile_url TEXT DEFAULT '',
            poap_address TEXT DEFAULT '',
            note TEXT DEFAULT '',
            generated_bio TEXT DEFAULT '',
            generated_highlight TEXT DEFAULT '',
            generated_profile_updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS member_aliases (
            alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(member_id, alias)
        );

        CREATE TABLE IF NOT EXISTS discord_users (
            discord_user_id TEXT PRIMARY KEY,
            username TEXT,
            global_name TEXT,
            display_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS discord_links (
            discord_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL REFERENCES discord_users(discord_user_id) ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            discord_username TEXT,
            discord_display_name TEXT,
            linked_at TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            is_primary INTEGER NOT NULL DEFAULT 1,
            UNIQUE(discord_user_id, member_id)
        );

        CREATE TABLE IF NOT EXISTS discord_channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            channel_kind TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_threads (
            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            UNIQUE(scope_type, scope_key)
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id TEXT UNIQUE,
            thread_id INTEGER NOT NULL REFERENCES conversation_threads(thread_id) ON DELETE CASCADE,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            author_type TEXT NOT NULL,
            workflow TEXT,
            event_type TEXT,
            content TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            source_message_id INTEGER REFERENCES messages(message_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            episode_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            source_message_ids_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_state (
            channel_id TEXT PRIMARY KEY REFERENCES discord_channels(channel_id) ON DELETE CASCADE,
            last_elixir_post_at TEXT,
            last_topics_json TEXT,
            recent_style_notes_json TEXT,
            last_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS clan_memberships (
            membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            left_at TEXT,
            join_source TEXT NOT NULL,
            leave_source TEXT
        );

        CREATE TABLE IF NOT EXISTS member_current_state (
            member_id INTEGER PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            role TEXT,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            previous_clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            arena_id INTEGER,
            arena_name TEXT,
            arena_raw_name TEXT,
            last_seen_api TEXT,
            source TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_state_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            name TEXT,
            role TEXT,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            previous_clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            arena_id INTEGER,
            arena_name TEXT,
            arena_raw_name TEXT,
            last_seen_api TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_daily_metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            metric_date TEXT NOT NULL,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            last_seen_api TEXT,
            UNIQUE(member_id, metric_date)
        );

        CREATE TABLE IF NOT EXISTS player_profile_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            wins INTEGER,
            losses INTEGER,
            battle_count INTEGER,
            total_donations INTEGER,
            donations INTEGER,
            donations_received INTEGER,
            war_day_wins INTEGER,
            challenge_max_wins INTEGER,
            challenge_cards_won INTEGER,
            tournament_battle_count INTEGER,
            tournament_cards_won INTEGER,
            three_crown_wins INTEGER,
            current_favourite_card_id INTEGER,
            current_favourite_card_name TEXT,
            league_statistics_json TEXT,
            current_deck_json TEXT,
            cards_json TEXT,
            badges_json TEXT,
            achievements_json TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_card_collection_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            cards_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_deck_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            mode_scope TEXT NOT NULL,
            deck_hash TEXT,
            deck_json TEXT NOT NULL,
            sample_size INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS member_card_usage_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            mode_scope TEXT NOT NULL,
            sample_battles INTEGER NOT NULL DEFAULT 0,
            cards_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_battle_facts (
            battle_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            battle_time TEXT NOT NULL,
            battle_type TEXT,
            game_mode_name TEXT,
            game_mode_id INTEGER,
            deck_selection TEXT,
            arena_id INTEGER,
            arena_name TEXT,
            crowns_for INTEGER,
            crowns_against INTEGER,
            outcome TEXT,
            trophy_change INTEGER,
            starting_trophies INTEGER,
            is_competitive INTEGER NOT NULL DEFAULT 0,
            is_ladder INTEGER NOT NULL DEFAULT 0,
            is_ranked INTEGER NOT NULL DEFAULT 0,
            is_war INTEGER NOT NULL DEFAULT 0,
            is_special_event INTEGER NOT NULL DEFAULT 0,
            deck_json TEXT,
            support_cards_json TEXT,
            opponent_name TEXT,
            opponent_tag TEXT,
            opponent_clan_tag TEXT,
            raw_json TEXT,
            UNIQUE(member_id, battle_time, battle_type, opponent_tag, crowns_for, crowns_against)
        );

        CREATE TABLE IF NOT EXISTS member_recent_form (
            form_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            computed_at TEXT NOT NULL,
            scope TEXT NOT NULL,
            sample_size INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            draws INTEGER NOT NULL,
            current_streak INTEGER NOT NULL DEFAULT 0,
            current_streak_type TEXT,
            win_rate REAL NOT NULL DEFAULT 0,
            avg_crown_diff REAL,
            avg_trophy_change REAL,
            form_label TEXT,
            summary TEXT,
            UNIQUE(member_id, scope)
        );

        CREATE TABLE IF NOT EXISTS war_current_state (
            war_id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            war_state TEXT,
            clan_tag TEXT,
            clan_name TEXT,
            fame INTEGER,
            repair_points INTEGER,
            period_points INTEGER,
            clan_score INTEGER,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS war_day_status (
            status_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            battle_date TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            fame INTEGER,
            repair_points INTEGER,
            boat_attacks INTEGER,
            decks_used_total INTEGER,
            decks_used_today INTEGER,
            raw_json TEXT,
            UNIQUE(member_id, battle_date)
        );

        CREATE TABLE IF NOT EXISTS war_races (
            war_race_id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER NOT NULL,
            section_index INTEGER NOT NULL,
            created_date TEXT,
            our_rank INTEGER,
            trophy_change INTEGER,
            our_fame INTEGER,
            total_clans INTEGER,
            finish_time TEXT,
            raw_json TEXT,
            UNIQUE(season_id, section_index)
        );

        CREATE TABLE IF NOT EXISTS war_participation (
            participation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            war_race_id INTEGER NOT NULL REFERENCES war_races(war_race_id) ON DELETE CASCADE,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT,
            fame INTEGER,
            repair_points INTEGER,
            boat_attacks INTEGER,
            decks_used INTEGER,
            decks_used_today INTEGER,
            raw_json TEXT,
            UNIQUE(war_race_id, player_tag)
        );

        CREATE TABLE IF NOT EXISTS raw_api_payloads (
            payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(endpoint, entity_key, payload_hash)
        );

        CREATE TABLE IF NOT EXISTS signal_log (
            signal_date TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            UNIQUE(signal_date, signal_type)
        );

        CREATE TABLE IF NOT EXISTS cake_day_announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            announcement_date TEXT NOT NULL,
            announcement_type TEXT NOT NULL,
            target_tag TEXT,
            recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            UNIQUE(announcement_date, announcement_type, target_tag)
        );

        CREATE INDEX IF NOT EXISTS idx_members_status ON members(status);
        CREATE INDEX IF NOT EXISTS idx_members_tag ON members(player_tag);
        CREATE INDEX IF NOT EXISTS idx_memberships_member ON clan_memberships(member_id, left_at, joined_at);
        CREATE INDEX IF NOT EXISTS idx_current_rank ON member_current_state(clan_rank, role);
        CREATE INDEX IF NOT EXISTS idx_state_snapshots_member_time ON member_state_snapshots(member_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_metrics_member_date ON member_daily_metrics(member_id, metric_date DESC);
        CREATE INDEX IF NOT EXISTS idx_profile_snapshots_member_time ON player_profile_snapshots(member_id, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_battle_facts_member_time ON member_battle_facts(member_id, battle_time DESC);
        CREATE INDEX IF NOT EXISTS idx_recent_form_member_scope ON member_recent_form(member_id, scope);
        CREATE INDEX IF NOT EXISTS idx_war_races_season ON war_races(season_id, section_index);
        CREATE INDEX IF NOT EXISTS idx_war_participation_member ON war_participation(member_id, war_race_id);
        CREATE INDEX IF NOT EXISTS idx_war_day_status_member_date ON war_day_status(member_id, battle_date DESC);
        CREATE INDEX IF NOT EXISTS idx_raw_payloads_endpoint_entity ON raw_api_payloads(endpoint, entity_key, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_threads_scope ON conversation_threads(scope_type, scope_key);
        CREATE INDEX IF NOT EXISTS idx_messages_thread_time ON messages(thread_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_facts(subject_type, subject_key, fact_type);
        """
    )


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompt_failures (
            failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            workflow TEXT,
            failure_type TEXT NOT NULL,
            failure_stage TEXT NOT NULL,
            channel_id TEXT,
            channel_name TEXT,
            discord_user_id TEXT,
            discord_message_id TEXT,
            question TEXT NOT NULL,
            detail TEXT,
            result_preview TEXT,
            llm_last_error TEXT,
            llm_last_model TEXT,
            llm_last_call_at TEXT,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_prompt_failures_recorded_at ON prompt_failures(recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_prompt_failures_workflow ON prompt_failures(workflow, recorded_at DESC);
        """
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "member_metadata")
    if "generated_bio" not in columns:
        conn.execute("ALTER TABLE member_metadata ADD COLUMN generated_bio TEXT DEFAULT ''")
    if "generated_highlight" not in columns:
        conn.execute("ALTER TABLE member_metadata ADD COLUMN generated_highlight TEXT DEFAULT ''")
    if "generated_profile_updated_at" not in columns:
        conn.execute("ALTER TABLE member_metadata ADD COLUMN generated_profile_updated_at TEXT")


def _migration_3(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "member_metadata")
    joined_column = "joined_at" if "joined_at" in columns else "joined_at_override"
    conn.execute(
        "INSERT INTO member_metadata (member_id) "
        "SELECT m.member_id FROM members m "
        "WHERE NOT EXISTS (SELECT 1 FROM member_metadata md WHERE md.member_id = m.member_id)"
    )
    rows = conn.execute("SELECT member_id FROM members").fetchall()
    for row in rows:
        trusted_joined_at = _trusted_current_joined_at(conn, row["member_id"])
        if not trusted_joined_at:
            continue
        current = conn.execute(
            f"SELECT {joined_column} AS joined_at FROM member_metadata WHERE member_id = ?",
            (row["member_id"],),
        ).fetchone()
        if current and current["joined_at"]:
            continue
        conn.execute(
            f"UPDATE member_metadata SET {joined_column} = ? WHERE member_id = ?",
            (trusted_joined_at, row["member_id"]),
        )


def _migration_4(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "member_metadata")
    if "joined_at" not in columns and "joined_at_override" in columns:
        conn.execute("ALTER TABLE member_metadata RENAME COLUMN joined_at_override TO joined_at")
        columns = _table_columns(conn, "member_metadata")
    if "joined_at" not in columns:
        conn.execute("ALTER TABLE member_metadata ADD COLUMN joined_at TEXT")


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS war_period_clan_status (
            status_id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER,
            section_index INTEGER,
            period_index INTEGER NOT NULL,
            period_offset INTEGER,
            clan_tag TEXT NOT NULL,
            clan_name TEXT,
            points_earned INTEGER,
            progress_start_of_day INTEGER,
            progress_end_of_day INTEGER,
            end_of_day_rank INTEGER,
            progress_earned INTEGER,
            num_defenses_remaining INTEGER,
            progress_earned_from_defenses INTEGER,
            observed_at TEXT NOT NULL,
            raw_json TEXT,
            UNIQUE(season_id, section_index, period_index, clan_tag)
        );

        CREATE INDEX IF NOT EXISTS idx_war_period_clan_status_lookup
            ON war_period_clan_status(clan_tag, season_id, section_index, period_index DESC);
        """
    )


def _migration_6(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS system_signals (
            system_signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            signal_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            announced_at TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_system_signals_pending
            ON system_signals(announced_at, created_at DESC);
        """
    )

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    if db_path in {"", ":memory:"}:
        return

    payload = {
        "title": "Achievement Unlocked: Boat Defense Intel",
        "capability": "boat_defense_intelligence",
        "importance": "high",
        "flavor": "clash_royale_achievement",
        "message": (
            "Elixir has unlocked a new clan-war intelligence upgrade. "
            "It can now read clan-level boat defense performance from River Race "
            "period logs, including defenses remaining and progress earned from defenses."
        ),
        "limitations": [
            "This is clan-level intel, not member-level defense placement tracking.",
            "The Clash Royale API still does not reveal which specific member placed defenses.",
        ],
        "announcement_style": (
            "Use Clash Royale flavored wording, like an achievement unlock or new tower "
            "ability, but keep the claims factual."
        ),
    }
    conn.execute(
        "INSERT OR IGNORE INTO system_signals (signal_key, signal_type, created_at, payload_json) VALUES (?, ?, ?, ?)",
        (
            "capability_boat_defense_intelligence_v1",
            "capability_unlock",
            _utcnow(),
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        ),
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    profile_columns = _table_columns(conn, "player_profile_snapshots")
    for name, sql_type in (
        ("exp_points", "INTEGER"),
        ("total_exp_points", "INTEGER"),
        ("star_points", "INTEGER"),
        ("clan_cards_collected", "INTEGER"),
        ("current_deck_support_cards_json", "TEXT"),
        ("support_cards_json", "TEXT"),
        ("current_path_of_legend_season_result_json", "TEXT"),
        ("last_path_of_legend_season_result_json", "TEXT"),
        ("best_path_of_legend_season_result_json", "TEXT"),
        ("legacy_trophy_road_high_score", "INTEGER"),
        ("progress_json", "TEXT"),
    ):
        if name not in profile_columns:
            conn.execute(f"ALTER TABLE player_profile_snapshots ADD COLUMN {name} {sql_type}")

    collection_columns = _table_columns(conn, "member_card_collection_snapshots")
    if "support_cards_json" not in collection_columns:
        conn.execute("ALTER TABLE member_card_collection_snapshots ADD COLUMN support_cards_json TEXT")

    deck_columns = _table_columns(conn, "member_deck_snapshots")
    if "support_cards_json" not in deck_columns:
        conn.execute("ALTER TABLE member_deck_snapshots ADD COLUMN support_cards_json TEXT")

    battle_columns = _table_columns(conn, "member_battle_facts")
    for name, sql_type in (
        ("event_tag", "TEXT"),
        ("league_number", "INTEGER"),
        ("is_hosted_match", "INTEGER"),
        ("modifiers_json", "TEXT"),
        ("team_rounds_json", "TEXT"),
        ("opponent_rounds_json", "TEXT"),
        ("boat_battle_side", "TEXT"),
        ("boat_battle_won", "INTEGER"),
        ("new_towers_destroyed", "INTEGER"),
        ("prev_towers_destroyed", "INTEGER"),
        ("remaining_towers", "INTEGER"),
    ):
        if name not in battle_columns:
            conn.execute(f"ALTER TABLE member_battle_facts ADD COLUMN {name} {sql_type}")


def _migration_8(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clan_memories (
            memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            source_type TEXT NOT NULL,
            is_inference INTEGER NOT NULL,
            confidence REAL NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            title TEXT,
            body TEXT NOT NULL,
            summary TEXT,
            member_id INTEGER,
            member_tag TEXT,
            role TEXT,
            channel_id TEXT,
            war_season_id TEXT,
            war_week_id TEXT,
            event_type TEXT,
            event_id TEXT,
            retention_class TEXT NOT NULL DEFAULT 'standard',
            expires_at TEXT,
            metadata_json TEXT,
            embedding_model TEXT,
            embedding_created_at TEXT,
            FOREIGN KEY(member_id) REFERENCES members(member_id) ON DELETE SET NULL,
            CHECK(source_type IN ('leader_note', 'elixir_inference', 'system')),
            CHECK(scope IN ('public', 'leadership', 'system_internal')),
            CHECK(status IN ('active', 'archived', 'deleted')),
            CHECK(is_inference IN (0, 1)),
            CHECK(confidence >= 0.0 AND confidence <= 1.0),
            CHECK(source_type != 'elixir_inference' OR (is_inference = 1 AND confidence < 1.0))
        );

        CREATE TABLE IF NOT EXISTS clan_memory_tags (
            tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS clan_memory_tag_links (
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES clan_memory_tags(tag_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS clan_memory_member_links (
            memory_member_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            member_tag TEXT,
            relation_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS clan_memory_event_links (
            memory_event_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(memory_id, event_type, event_id)
        );

        CREATE TABLE IF NOT EXISTS clan_memory_evidence_refs (
            evidence_ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            evidence_type TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            evidence_label TEXT,
            evidence_url TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS clan_memory_versions (
            memory_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            title TEXT,
            body TEXT,
            summary TEXT,
            status TEXT,
            scope TEXT,
            metadata_json TEXT,
            confidence REAL,
            UNIQUE(memory_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS clan_memory_audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            changed_at TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            action TEXT NOT NULL,
            payload_json TEXT
        );

        CREATE TABLE IF NOT EXISTS clan_memory_embeddings (
            memory_id INTEGER PRIMARY KEY REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
            embedding_model TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS clan_memory_index_status (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO clan_memory_index_status (key, value) VALUES ('sqlite_vec_enabled', '0');

        CREATE VIRTUAL TABLE IF NOT EXISTS clan_memories_fts USING fts5(
            title,
            summary,
            body,
            content='clan_memories',
            content_rowid='memory_id'
        );

        CREATE TRIGGER IF NOT EXISTS clan_memories_ai AFTER INSERT ON clan_memories BEGIN
            INSERT INTO clan_memories_fts(rowid, title, summary, body)
            VALUES (new.memory_id, new.title, new.summary, new.body);
        END;

        CREATE TRIGGER IF NOT EXISTS clan_memories_ad AFTER DELETE ON clan_memories BEGIN
            INSERT INTO clan_memories_fts(clan_memories_fts, rowid, title, summary, body)
            VALUES('delete', old.memory_id, old.title, old.summary, old.body);
        END;

        CREATE TRIGGER IF NOT EXISTS clan_memories_au AFTER UPDATE ON clan_memories BEGIN
            INSERT INTO clan_memories_fts(clan_memories_fts, rowid, title, summary, body)
            VALUES('delete', old.memory_id, old.title, old.summary, old.body);
            INSERT INTO clan_memories_fts(rowid, title, summary, body)
            VALUES (new.memory_id, new.title, new.summary, new.body);
        END;

        CREATE INDEX IF NOT EXISTS idx_clan_memories_scope_status_created
            ON clan_memories(scope, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_memories_member
            ON clan_memories(member_id, member_tag, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_memories_war
            ON clan_memories(war_season_id, war_week_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_memories_event
            ON clan_memories(event_type, event_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_memories_source
            ON clan_memories(source_type, is_inference, confidence, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_memory_evidence_lookup
            ON clan_memory_evidence_refs(memory_id, evidence_type, evidence_ref);
        CREATE INDEX IF NOT EXISTS idx_clan_memory_member_links_lookup
            ON clan_memory_member_links(member_id, member_tag, relation_type);
        CREATE INDEX IF NOT EXISTS idx_clan_memory_event_links_lookup
            ON clan_memory_event_links(event_type, event_id);
        """
    )

    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS clan_memory_vec USING vec0(memory_id INTEGER PRIMARY KEY, embedding float[1536])"
        )
        conn.execute(
            "UPDATE clan_memory_index_status SET value = '1' WHERE key = 'sqlite_vec_enabled'"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "UPDATE clan_memory_index_status SET value = '0' WHERE key = 'sqlite_vec_enabled'"
        )


def _migration_9(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clan_daily_metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_date TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            clan_name TEXT,
            member_count INTEGER NOT NULL DEFAULT 0,
            open_slots INTEGER NOT NULL DEFAULT 0,
            clan_score INTEGER,
            clan_war_trophies INTEGER,
            required_trophies INTEGER,
            donations_per_week_requirement INTEGER,
            weekly_donations_total INTEGER,
            total_member_trophies INTEGER,
            avg_member_trophies REAL,
            top_member_trophies INTEGER,
            joins_today INTEGER NOT NULL DEFAULT 0,
            leaves_today INTEGER NOT NULL DEFAULT 0,
            net_member_change INTEGER NOT NULL DEFAULT 0,
            observed_at TEXT NOT NULL,
            raw_json TEXT,
            UNIQUE(clan_tag, metric_date)
        );

        CREATE INDEX IF NOT EXISTS idx_clan_daily_metrics_date
            ON clan_daily_metrics(metric_date DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_daily_metrics_clan_date
            ON clan_daily_metrics(clan_tag, metric_date DESC);
        """
    )


def _migration_10(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS member_daily_battle_rollups (
            rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            battle_date TEXT NOT NULL,
            mode_group TEXT NOT NULL,
            game_mode_id INTEGER,
            game_mode_name TEXT,
            battles INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            crowns_for INTEGER NOT NULL DEFAULT 0,
            crowns_against INTEGER NOT NULL DEFAULT 0,
            trophy_change_total INTEGER NOT NULL DEFAULT 0,
            first_battle_at TEXT,
            last_battle_at TEXT,
            captured_battles INTEGER NOT NULL DEFAULT 0,
            expected_battle_delta INTEGER,
            completeness_ratio REAL,
            is_complete INTEGER NOT NULL DEFAULT 0,
            last_aggregated_at TEXT NOT NULL,
            UNIQUE(member_id, battle_date, mode_group, game_mode_id)
        );

        CREATE INDEX IF NOT EXISTS idx_member_daily_battle_rollups_member_date
            ON member_daily_battle_rollups(member_id, battle_date DESC, mode_group);
        CREATE INDEX IF NOT EXISTS idx_member_daily_battle_rollups_date
            ON member_daily_battle_rollups(battle_date DESC, mode_group);
        """
    )


def _migration_11(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clan_daily_battle_rollups (
            rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_date TEXT NOT NULL,
            clan_tag TEXT NOT NULL,
            clan_name TEXT,
            mode_group TEXT NOT NULL,
            game_mode_id INTEGER,
            game_mode_name TEXT,
            members_active INTEGER NOT NULL DEFAULT 0,
            battles INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            crowns_for INTEGER NOT NULL DEFAULT 0,
            crowns_against INTEGER NOT NULL DEFAULT 0,
            trophy_change_total INTEGER NOT NULL DEFAULT 0,
            captured_battles INTEGER,
            expected_battle_delta INTEGER,
            completeness_ratio REAL,
            is_complete INTEGER NOT NULL DEFAULT 0,
            last_aggregated_at TEXT NOT NULL,
            UNIQUE(clan_tag, battle_date, mode_group, game_mode_id)
        );

        CREATE INDEX IF NOT EXISTS idx_clan_daily_battle_rollups_date
            ON clan_daily_battle_rollups(battle_date DESC, mode_group);
        CREATE INDEX IF NOT EXISTS idx_clan_daily_battle_rollups_clan_date
            ON clan_daily_battle_rollups(clan_tag, battle_date DESC, mode_group);
        """
    )


def _migration_12(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "member_metadata")
    for name, sql_type in (
        ("cr_account_age_days", "INTEGER"),
        ("cr_account_age_years", "INTEGER"),
        ("cr_account_age_updated_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE member_metadata ADD COLUMN {name} {sql_type}")


def _migration_13(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "member_metadata")
    for name, sql_type in (
        ("cr_games_per_day", "REAL"),
        ("cr_games_per_day_window_days", "INTEGER"),
        ("cr_games_per_day_updated_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE member_metadata ADD COLUMN {name} {sql_type}")


def _migration_14(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "war_day_status")
    for name, sql_type in (
        ("season_id", "INTEGER"),
        ("section_index", "INTEGER"),
        ("period_index", "INTEGER"),
        ("phase", "TEXT"),
        ("phase_day_number", "INTEGER"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE war_day_status ADD COLUMN {name} {sql_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_war_day_status_period ON war_day_status(season_id, section_index, period_index, phase)"
    )


def _migration_15(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS war_participant_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            war_day_key TEXT NOT NULL,
            season_id INTEGER,
            section_index INTEGER,
            period_index INTEGER,
            phase TEXT,
            phase_day_number INTEGER,
            clan_tag TEXT,
            clan_name TEXT,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT,
            fame INTEGER,
            repair_points INTEGER,
            boat_attacks INTEGER,
            decks_used_total INTEGER,
            decks_used_today INTEGER,
            raw_json TEXT,
            UNIQUE(war_day_key, observed_at, player_tag)
        );

        CREATE INDEX IF NOT EXISTS idx_war_participant_snapshots_day_time
            ON war_participant_snapshots(war_day_key, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_war_participant_snapshots_member_time
            ON war_participant_snapshots(member_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_war_participant_snapshots_period
            ON war_participant_snapshots(season_id, section_index, period_index, observed_at DESC);
        """
    )


def _migration_16(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_signal_key TEXT NOT NULL,
            source_signal_type TEXT NOT NULL,
            target_channel_key TEXT NOT NULL,
            target_channel_id TEXT NOT NULL,
            intent TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            delivery_status TEXT NOT NULL DEFAULT 'planned',
            payload_json TEXT,
            error_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_attempt_at TEXT,
            delivered_at TEXT,
            UNIQUE(source_signal_key, target_channel_key, intent)
        );

        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_source
            ON signal_outcomes(source_signal_key, delivery_status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_target
            ON signal_outcomes(target_channel_key, delivery_status, updated_at DESC);
        """
    )


def _migration_17(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signal_detector_cursors (
            detector_key TEXT NOT NULL,
            scope_key TEXT NOT NULL DEFAULT '',
            cursor_text TEXT,
            cursor_int INTEGER,
            updated_at TEXT NOT NULL,
            metadata_json TEXT,
            PRIMARY KEY(detector_key, scope_key)
        );

        CREATE INDEX IF NOT EXISTS idx_signal_detector_cursors_updated
            ON signal_detector_cursors(updated_at DESC);
        """
    )


def _migration_18(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompt_feedback (
            prompt_feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_message_id INTEGER REFERENCES messages(message_id) ON DELETE SET NULL,
            assistant_discord_message_id TEXT NOT NULL,
            workflow TEXT,
            channel_id TEXT,
            channel_name TEXT,
            discord_user_id TEXT NOT NULL,
            original_asker_discord_user_id TEXT,
            feedback_value TEXT NOT NULL,
            question TEXT,
            response_preview TEXT,
            recorded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            removed_at TEXT,
            retry_invited_at TEXT,
            retry_invite_message_id TEXT,
            UNIQUE(assistant_discord_message_id, discord_user_id),
            CHECK(feedback_value IN ('up', 'down'))
        );

        CREATE INDEX IF NOT EXISTS idx_prompt_feedback_updated
            ON prompt_feedback(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_prompt_feedback_workflow_active
            ON prompt_feedback(workflow, removed_at, updated_at DESC);
        """
    )


def _migration_19(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "player_profile_snapshots")
    if "raw_json" in columns:
        conn.execute("ALTER TABLE player_profile_snapshots DROP COLUMN raw_json")


def _migration_20(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tournaments (
            tournament_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_tag TEXT NOT NULL UNIQUE,
            name TEXT,
            description TEXT,
            type TEXT,
            status TEXT NOT NULL,
            creator_tag TEXT,
            creator_name TEXT,
            game_mode_id INTEGER,
            game_mode_name TEXT,
            deck_selection TEXT,
            level_cap INTEGER,
            max_capacity INTEGER,
            duration_seconds INTEGER,
            preparation_duration_seconds INTEGER,
            created_time TEXT,
            started_time TEXT,
            ended_time TEXT,
            watching_started_at TEXT,
            watching_ended_at TEXT,
            poll_count INTEGER NOT NULL DEFAULT 0,
            last_poll_at TEXT,
            battles_captured INTEGER NOT NULL DEFAULT 0,
            recap_posted_at TEXT,
            raw_final_json TEXT
        );

        CREATE TABLE IF NOT EXISTS tournament_participants (
            participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL REFERENCES tournaments(tournament_id) ON DELETE CASCADE,
            player_tag TEXT NOT NULL,
            player_name TEXT,
            member_id INTEGER REFERENCES members(member_id),
            clan_tag TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            final_score INTEGER,
            final_rank INTEGER,
            UNIQUE(tournament_id, player_tag)
        );
        CREATE INDEX IF NOT EXISTS idx_tournament_participants_tournament
            ON tournament_participants(tournament_id);

        CREATE TABLE IF NOT EXISTS tournament_battles (
            tournament_battle_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER NOT NULL REFERENCES tournaments(tournament_id) ON DELETE CASCADE,
            battle_time TEXT NOT NULL,
            player1_tag TEXT NOT NULL,
            player1_name TEXT,
            player1_member_id INTEGER REFERENCES members(member_id),
            player1_crowns INTEGER,
            player1_deck_json TEXT,
            player2_tag TEXT NOT NULL,
            player2_name TEXT,
            player2_member_id INTEGER REFERENCES members(member_id),
            player2_crowns INTEGER,
            player2_deck_json TEXT,
            winner_tag TEXT,
            deck_selection TEXT,
            game_mode_id INTEGER,
            arena_name TEXT,
            raw_json TEXT,
            UNIQUE(tournament_id, battle_time, player1_tag, player2_tag)
        );
        CREATE INDEX IF NOT EXISTS idx_tournament_battles_tournament
            ON tournament_battles(tournament_id);
        """
    )

    battle_columns = _table_columns(conn, "member_battle_facts")
    if "tournament_tag" not in battle_columns:
        conn.execute("ALTER TABLE member_battle_facts ADD COLUMN tournament_tag TEXT")


def _migration_21(conn: sqlite3.Connection) -> None:
    """Rename openai_* columns to llm_* in prompt_failures for provider-neutral naming."""
    columns = _table_columns(conn, "prompt_failures")
    if "openai_last_error" in columns:
        conn.execute("ALTER TABLE prompt_failures RENAME COLUMN openai_last_error TO llm_last_error")
    if "openai_last_model" in columns:
        conn.execute("ALTER TABLE prompt_failures RENAME COLUMN openai_last_model TO llm_last_model")
    if "openai_last_call_at" in columns:
        conn.execute("ALTER TABLE prompt_failures RENAME COLUMN openai_last_call_at TO llm_last_call_at")


def _migration_22(conn: sqlite3.Connection) -> None:
    """Create llm_calls table for persistent token usage tracking."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            call_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            workflow TEXT NOT NULL,
            model TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            duration_ms REAL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cache_creation_tokens INTEGER,
            cache_read_tokens INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_llm_calls_recorded_at ON llm_calls(recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_workflow ON llm_calls(workflow, recorded_at DESC);
        CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model, recorded_at DESC);
        """
    )


def _migration_23(conn: sqlite3.Connection) -> None:
    """Create card_catalog and quiz tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS card_catalog (
            card_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            elixir_cost INTEGER,
            rarity TEXT,
            max_level INTEGER,
            max_evolution_level INTEGER,
            card_type TEXT NOT NULL,
            icon_url TEXT,
            hero_icon_url TEXT,
            evolution_icon_url TEXT,
            synced_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_card_catalog_name ON card_catalog(name);
        CREATE INDEX IF NOT EXISTS idx_card_catalog_rarity ON card_catalog(rarity);
        CREATE INDEX IF NOT EXISTS idx_card_catalog_type ON card_catalog(card_type);

        CREATE TABLE IF NOT EXISTS quiz_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            member_id INTEGER,
            session_type TEXT NOT NULL,
            question_count INTEGER NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            channel_id TEXT,
            message_id TEXT,
            question_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_quiz_sessions_user ON quiz_sessions(discord_user_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS quiz_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES quiz_sessions(session_id),
            question_index INTEGER NOT NULL,
            question_type TEXT NOT NULL,
            question_text TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            user_answer TEXT,
            is_correct INTEGER,
            answered_at TEXT,
            card_ids_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_quiz_responses_session ON quiz_responses(session_id);

        CREATE TABLE IF NOT EXISTS quiz_daily_streaks (
            discord_user_id TEXT PRIMARY KEY,
            current_streak INTEGER NOT NULL DEFAULT 0,
            longest_streak INTEGER NOT NULL DEFAULT 0,
            last_correct_date TEXT,
            total_daily_correct INTEGER NOT NULL DEFAULT 0,
            total_daily_answered INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _migration_24(conn: sqlite3.Connection) -> None:
    """Add discord_user_id to quiz_responses for per-user daily dedup."""
    columns = _table_columns(conn, "quiz_responses")
    if "discord_user_id" not in columns:
        conn.execute("ALTER TABLE quiz_responses ADD COLUMN discord_user_id TEXT")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_responses_daily_user
            ON quiz_responses(session_id, discord_user_id)
            WHERE discord_user_id IS NOT NULL;
        """
    )


def _migration_25(conn: sqlite3.Connection) -> None:
    """Capture opponent decks on member_battle_facts.

    Opponent cards are present in raw_json (battle["opponent"][0]["cards"]) but were
    not extracted to typed columns. This migration adds opponent_deck_json and
    opponent_support_cards_json, then backfills both from raw_json so the new
    deck-review tooling can aggregate "what's beating you" without parsing every
    raw payload at query time.
    """
    columns = _table_columns(conn, "member_battle_facts")
    if "opponent_deck_json" not in columns:
        conn.execute("ALTER TABLE member_battle_facts ADD COLUMN opponent_deck_json TEXT")
    if "opponent_support_cards_json" not in columns:
        conn.execute("ALTER TABLE member_battle_facts ADD COLUMN opponent_support_cards_json TEXT")

    rows = conn.execute(
        "SELECT battle_fact_id, raw_json FROM member_battle_facts "
        "WHERE opponent_deck_json IS NULL AND raw_json IS NOT NULL"
    ).fetchall()

    for row in rows:
        try:
            battle = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            continue
        opponents = battle.get("opponent") or []
        if not opponents:
            continue
        opp = opponents[0] if isinstance(opponents[0], dict) else {}
        cards = opp.get("cards") or []
        support_cards = opp.get("supportCards") or []
        deck_payload = json.dumps(cards) if cards else None
        support_payload = json.dumps(support_cards) if support_cards else None
        conn.execute(
            "UPDATE member_battle_facts SET opponent_deck_json = ?, opponent_support_cards_json = ? WHERE battle_fact_id = ?",
            (deck_payload, support_payload, row["battle_fact_id"]),
        )


def _migration_26(conn: sqlite3.Connection) -> None:
    """Create awareness_ticks for per-tick awareness-loop observability.

    The v4.5 awareness loop emits one agent turn per tick that sees all signals
    at once and decides what (if anything) to post. Previously only
    ``signal_outcomes`` rows were written, and only for *covered* signals, so
    admin reports under-counted ticks where the agent saw signals and chose
    silence. This table records the per-tick decision so observability is
    complete — including the skipped_reason the agent returned.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS awareness_ticks (
            tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticked_at TEXT NOT NULL,
            workflow TEXT,
            signals_in INTEGER NOT NULL DEFAULT 0,
            posts_delivered INTEGER NOT NULL DEFAULT 0,
            posts_rejected INTEGER NOT NULL DEFAULT 0,
            covered_keys INTEGER NOT NULL DEFAULT 0,
            considered_skipped INTEGER NOT NULL DEFAULT 0,
            hard_fallback INTEGER NOT NULL DEFAULT 0,
            hard_fallback_failed INTEGER NOT NULL DEFAULT 0,
            all_ok INTEGER NOT NULL DEFAULT 1,
            skipped_reason TEXT,
            signal_outcomes_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_awareness_ticks_ticked_at
            ON awareness_ticks(ticked_at DESC);
        """
    )


def _migration_27(conn: sqlite3.Connection) -> None:
    """Add write-call counters to awareness_ticks so the awareness loop's new
    write surface (flag_member_watch, record_leadership_followup, save_clan_memory)
    is observable per tick alongside the existing signal-coverage metrics.

    ``issued`` counts attempted writes, ``succeeded`` counts writes that returned
    success, and ``denied`` counts writes the per-turn budget rejected.
    """
    conn.executescript(
        """
        ALTER TABLE awareness_ticks ADD COLUMN write_calls_issued INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE awareness_ticks ADD COLUMN write_calls_succeeded INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE awareness_ticks ADD COLUMN write_calls_denied INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migration_28(conn: sqlite3.Connection) -> None:
    """Create the revisits table so the awareness loop can schedule reminders
    for itself to look at a signal again at a later tick.

    Pattern mirrors system_signals (``announced_at`` state) plus
    cake_day_announcements (composite UNIQUE for dedup). A revisit is "due"
    when ``due_at <= now`` and ``revisited_at IS NULL``. build_situation lifts
    due rows into each tick's Situation under ``due_revisits``; the delivery
    layer marks them revisited once the agent covers the underlying signal.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS revisits (
            revisit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL,
            created_by_workflow TEXT NOT NULL,
            due_at TEXT NOT NULL,
            rationale TEXT,
            revisited_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(signal_key, due_at)
        );

        CREATE INDEX IF NOT EXISTS idx_revisits_due
            ON revisits(due_at ASC, revisited_at);
        """
    )


_MIGRATIONS = [_migration_0, _migration_1, _migration_2, _migration_3, _migration_4, _migration_5, _migration_6, _migration_7, _migration_8, _migration_9, _migration_10, _migration_11, _migration_12, _migration_13, _migration_14, _migration_15, _migration_16, _migration_17, _migration_18, _migration_19, _migration_20, _migration_21, _migration_22, _migration_23, _migration_24, _migration_25, _migration_26, _migration_27, _migration_28]


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, fn in enumerate(_MIGRATIONS):
        if version < current:
            continue
        fn(conn)
        conn.execute(f"PRAGMA user_version = {version + 1}")
        conn.commit()
