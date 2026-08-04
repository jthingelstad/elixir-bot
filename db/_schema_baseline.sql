-- Private migration-0 carried-table DDL for db.schema, exported verbatim from the read-only archive
-- (elixir-v5-archive-2026H2.db) with dead members-FK clauses stripped.
-- The archive is immutable, so this export is frozen truth - it lets
-- CI and any archive-less checkout build the v5.1 schema.

CREATE TABLE raw_api_payloads (
            payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(endpoint, entity_key, payload_hash)
        );

CREATE INDEX idx_raw_payloads_endpoint_entity ON raw_api_payloads(endpoint, entity_key, fetched_at DESC);

CREATE TABLE discord_users (
            discord_user_id TEXT PRIMARY KEY,
            username TEXT,
            global_name TEXT,
            display_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE leader_action_recommendations (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_key TEXT NOT NULL UNIQUE,
            action_type TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            target_channel_key TEXT,
            target_channel_id TEXT,
            target_player_tag TEXT,
            target_player_name TEXT,
            source_signal_key TEXT,
            source_signal_type TEXT,
            source_message_id TEXT,
            prompt_text TEXT NOT NULL,
            rationale TEXT,
            baseline_json TEXT,
            outcome_json TEXT,
            proposed_at TEXT NOT NULL,
            expires_at TEXT,
            decided_at TEXT,
            decided_by_discord_user_id TEXT,
            decision_emoji TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        , decision_note TEXT, decision_note_at TEXT, decision_note_message_id TEXT, decision_note_by_discord_user_id TEXT, copy_message_id TEXT, copy_message_ids_json TEXT, copy_original_text TEXT, copy_current_text TEXT, copy_edited_at TEXT, copy_edited_by_discord_user_id TEXT, copy_edit_diff_json TEXT, defer_days INTEGER, deferred_until TEXT, is_test INTEGER NOT NULL DEFAULT 0, ui_version TEXT, case_id INTEGER);

CREATE INDEX idx_leader_actions_status ON leader_action_recommendations(status, proposed_at DESC);

CREATE INDEX idx_leader_actions_message ON leader_action_recommendations(source_message_id);

CREATE INDEX idx_leader_actions_target ON leader_action_recommendations(target_player_tag, action_type);

CREATE INDEX idx_leader_actions_source ON leader_action_recommendations(source_signal_key, action_type);

CREATE INDEX idx_leader_actions_copy_message ON leader_action_recommendations(copy_message_id);

CREATE INDEX idx_leader_actions_test_status ON leader_action_recommendations(is_test, status, proposed_at DESC);

CREATE INDEX idx_leader_actions_case ON leader_action_recommendations(case_id, status);

CREATE TABLE prompt_failures (
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

CREATE INDEX idx_prompt_failures_recorded_at ON prompt_failures(recorded_at DESC);

CREATE INDEX idx_prompt_failures_workflow ON prompt_failures(workflow, recorded_at DESC);

CREATE TABLE prompt_feedback (
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

CREATE INDEX idx_prompt_feedback_updated
            ON prompt_feedback(updated_at DESC);

CREATE INDEX idx_prompt_feedback_workflow_active
            ON prompt_feedback(workflow, removed_at, updated_at DESC);

CREATE TABLE system_signals (
            system_signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            signal_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            announced_at TEXT,
            payload_json TEXT NOT NULL
        );

CREATE INDEX idx_system_signals_pending
            ON system_signals(announced_at, created_at DESC);

CREATE TABLE api_sentinel_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sentinel_type TEXT NOT NULL,
            scope TEXT NOT NULL,
            name TEXT NOT NULL,
            endpoint TEXT,
            entity_key TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            sample_json TEXT,
            announced_signal_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(sentinel_type, scope, name)
        );

CREATE INDEX idx_api_sentinel_type_seen ON api_sentinel_observations(sentinel_type, first_seen_at DESC);

CREATE INDEX idx_api_sentinel_endpoint ON api_sentinel_observations(endpoint, last_seen_at DESC);

CREATE INDEX idx_api_sentinel_announced ON api_sentinel_observations(announced_signal_key);

CREATE TABLE arena_relay_screenshot_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_message_id TEXT NOT NULL UNIQUE,
            channel_id TEXT,
            channel_name TEXT,
            author_discord_user_id TEXT,
            author_display_name TEXT,
            observed_at TEXT NOT NULL,
            screenshot_type TEXT NOT NULL DEFAULT 'unknown',
            summary TEXT,
            content TEXT,
            players_json TEXT,
            actionable_facts_json TEXT,
            uncertainty TEXT,
            image_count INTEGER NOT NULL DEFAULT 0,
            image_metadata_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE INDEX idx_arena_screenshot_observed ON arena_relay_screenshot_observations(observed_at DESC);

CREATE INDEX idx_arena_screenshot_type ON arena_relay_screenshot_observations(screenshot_type, observed_at DESC);

CREATE TABLE discord_channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            channel_kind TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

CREATE TABLE channel_state (
            channel_id TEXT PRIMARY KEY REFERENCES discord_channels(channel_id) ON DELETE CASCADE,
            last_elixir_post_at TEXT,
            last_topics_json TEXT,
            recent_style_notes_json TEXT,
            last_summary TEXT
        );

CREATE TABLE game_mode_contexts (
            context_id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_type TEXT NOT NULL,
            source_key TEXT NOT NULL,
            display_name TEXT,
            game_mode_id INTEGER,
            game_mode_name TEXT,
            event_tag TEXT,
            leaderboard_id INTEGER,
            source_endpoint TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            raw_json TEXT,
            UNIQUE(context_type, source_key)
        );

CREATE INDEX idx_game_mode_contexts_type_seen ON game_mode_contexts(context_type, last_seen_at DESC);

CREATE INDEX idx_game_mode_contexts_event_tag ON game_mode_contexts(event_tag);

CREATE INDEX idx_game_mode_contexts_leaderboard ON game_mode_contexts(leaderboard_id);

CREATE TABLE card_catalog (
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

CREATE INDEX idx_card_catalog_name ON card_catalog(name);

CREATE INDEX idx_card_catalog_rarity ON card_catalog(rarity);

CREATE INDEX idx_card_catalog_type ON card_catalog(card_type);

CREATE TABLE elixir_improvement_suggestions (
            suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'shadow',
            severity INTEGER NOT NULL DEFAULT 3,
            confidence REAL NOT NULL DEFAULT 0.5,
            title TEXT NOT NULL,
            rationale TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            source_fingerprint TEXT,
            github_issue_number INTEGER,
            github_issue_url TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            promoted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE INDEX idx_improvement_suggestions_status ON elixir_improvement_suggestions(status, confidence DESC, updated_at DESC);

CREATE INDEX idx_improvement_suggestions_category ON elixir_improvement_suggestions(category, status, updated_at DESC);

CREATE INDEX idx_improvement_suggestions_github ON elixir_improvement_suggestions(github_issue_number);

CREATE TABLE runtime_job_status (
            job_name TEXT PRIMARY KEY,
            status_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

CREATE TABLE tournaments (
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

CREATE TABLE conversation_threads (
            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            UNIQUE(scope_type, scope_key)
        );

CREATE INDEX idx_threads_scope ON conversation_threads(scope_type, scope_key);

CREATE TABLE messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id TEXT UNIQUE,
            thread_id INTEGER NOT NULL REFERENCES conversation_threads(thread_id) ON DELETE CASCADE,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER,
            author_type TEXT NOT NULL,
            workflow TEXT,
            event_type TEXT,
            content TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL,
            raw_json TEXT
        , intent_id INTEGER);

CREATE INDEX idx_messages_thread_time ON messages(thread_id, created_at DESC);

CREATE INDEX idx_messages_intent ON messages(intent_id, created_at DESC);

CREATE TABLE memory_episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            episode_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            source_message_ids_json TEXT,
            created_at TEXT NOT NULL
        );
