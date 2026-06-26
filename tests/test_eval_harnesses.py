from __future__ import annotations

import sqlite3


def _seed_eval_fixture_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE members ("
            "player_tag TEXT PRIMARY KEY, current_name TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE war_period_clan_status ("
            "clan_tag TEXT, clan_name TEXT)"
        )
        conn.execute(
            "CREATE TABLE member_battle_facts ("
            "opponent_tag TEXT, opponent_name TEXT, opponent_clan_tag TEXT)"
        )
        conn.execute(
            "INSERT INTO members VALUES ('#AAA111', 'Eval Alice', 'active')"
        )
        conn.execute(
            "INSERT INTO war_period_clan_status VALUES ('#EXT999', 'Outside Clan')"
        )
        conn.execute(
            "INSERT INTO member_battle_facts VALUES ('#OPP222', 'Opponent Bob', '#EXT999')"
        )
        conn.commit()
    finally:
        conn.close()


def _create_leader_action_eval_schema(conn):
    conn.execute(
        """
        CREATE TABLE leader_action_recommendations (
            action_id INTEGER PRIMARY KEY,
            action_key TEXT,
            action_type TEXT,
            objective TEXT,
            status TEXT,
            target_channel_key TEXT,
            target_channel_id TEXT,
            target_player_tag TEXT,
            target_player_name TEXT,
            source_signal_key TEXT,
            source_signal_type TEXT,
            source_message_id TEXT,
            prompt_text TEXT,
            rationale TEXT,
            baseline_json TEXT,
            outcome_json TEXT,
            proposed_at TEXT,
            expires_at TEXT,
            decided_at TEXT,
            decided_by_discord_user_id TEXT,
            decision_emoji TEXT,
            created_at TEXT,
            updated_at TEXT,
            decision_note TEXT,
            decision_note_at TEXT,
            decision_note_message_id TEXT,
            decision_note_by_discord_user_id TEXT,
            copy_message_id TEXT,
            copy_message_ids_json TEXT,
            copy_original_text TEXT,
            copy_current_text TEXT,
            copy_edited_at TEXT,
            copy_edited_by_discord_user_id TEXT,
            copy_edit_diff_json TEXT,
            defer_days INTEGER,
            deferred_until TEXT,
            is_test INTEGER NOT NULL DEFAULT 0,
            ui_version TEXT,
            case_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            discord_message_id TEXT PRIMARY KEY,
            channel_id TEXT,
            author_type TEXT,
            workflow TEXT,
            event_type TEXT,
            content TEXT,
            summary TEXT,
            created_at TEXT,
            raw_json TEXT,
            intent_id INTEGER
        )
        """
    )


def _create_player_highlight_eval_schema(conn):
    conn.execute(
        """
        CREATE TABLE communication_intents (
            intent_id INTEGER PRIMARY KEY,
            intent_key TEXT,
            workflow TEXT,
            intent_type TEXT,
            status TEXT,
            target_channel_key TEXT,
            target_channel_id TEXT,
            source_signal_key TEXT,
            source_signal_type TEXT,
            covers_signal_keys_json TEXT,
            event_keys_json TEXT,
            project_id INTEGER,
            case_id INTEGER,
            summary TEXT,
            content_preview TEXT,
            skipped_reason TEXT,
            error_detail TEXT,
            payload_json TEXT,
            created_at TEXT,
            updated_at TEXT,
            delivered_at TEXT,
            failed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            discord_message_id TEXT PRIMARY KEY,
            channel_id TEXT,
            author_type TEXT,
            workflow TEXT,
            event_type TEXT,
            content TEXT,
            summary TEXT,
            created_at TEXT,
            raw_json TEXT,
            intent_id INTEGER
        )
        """
    )


def _insert_leader_action(conn, **overrides):
    row = {
        "action_id": 1,
        "action_key": "leader-action:1",
        "action_type": "welcome_relay",
        "objective": "member_joined",
        "status": "done",
        "target_channel_key": "arena-relay",
        "target_channel_id": "1513758211206025227",
        "target_player_tag": "#AAA111",
        "target_player_name": "Eval Alice",
        "source_signal_key": "member_joined:#AAA111",
        "source_signal_type": "member_joined",
        "source_message_id": "2001",
        "prompt_text": "Welcome Eval Alice. - E",
        "rationale": "Exact leader action fixture.",
        "baseline_json": '{"fixture": true}',
        "outcome_json": '{"pending_evaluation": true}',
        "proposed_at": "2026-06-24T12:00:00Z",
        "expires_at": None,
        "decided_at": "2026-06-24T13:00:00Z",
        "decided_by_discord_user_id": "u1",
        "decision_emoji": "done",
        "created_at": "2026-06-24T12:00:00Z",
        "updated_at": "2026-06-24T13:00:00Z",
        "decision_note": None,
        "decision_note_at": None,
        "decision_note_message_id": None,
        "decision_note_by_discord_user_id": None,
        "copy_message_id": "2002",
        "copy_message_ids_json": '["2002"]',
        "copy_original_text": "Welcome Eval Alice. - E",
        "copy_current_text": "Welcome Eval Alice. - E",
        "copy_edited_at": None,
        "copy_edited_by_discord_user_id": None,
        "copy_edit_diff_json": None,
        "defer_days": None,
        "deferred_until": None,
        "is_test": 0,
        "ui_version": "leader-action-ui-v1",
        "case_id": None,
    }
    row.update(overrides)
    columns = list(row)
    conn.execute(
        f"INSERT INTO leader_action_recommendations ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def _insert_player_highlight_intent(conn, **overrides):
    row = {
        "intent_id": 11,
        "intent_key": "v5:intent:detection:best_trophies_peak:#AAA111:6000",
        "workflow": "v5-reactive",
        "intent_type": "celebrate:best_trophies_peak",
        "status": "delivered",
        "target_channel_key": "player-highlights",
        "target_channel_id": "1482352147029950474",
        "source_signal_key": "best_trophies_peak:#AAA111:6000",
        "source_signal_type": "best_trophies_peak",
        "covers_signal_keys_json": '["best_trophies_peak:#AAA111:6000"]',
        "event_keys_json": "[]",
        "project_id": None,
        "case_id": None,
        "summary": '{"detection_type": "best_trophies_peak", "peak": 6000}',
        "content_preview": "**Eval Alice** just hit 6000 trophies.",
        "skipped_reason": None,
        "error_detail": None,
        "payload_json": (
            '{"message_ids": ["4001"], "posted_messages": ['
            '{"discord_message_id": "4001", '
            '"content": "**Eval Alice** just hit 6000 trophies.", '
            '"discord_created_at": "2026-06-24T12:00:02Z"}]}'
        ),
        "created_at": "2026-06-24T12:00:00Z",
        "updated_at": "2026-06-24T12:00:02Z",
        "delivered_at": "2026-06-24T12:00:02Z",
        "failed_at": None,
    }
    row.update(overrides)
    columns = list(row)
    conn.execute(
        f"INSERT INTO communication_intents ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def test_eval_all_requests_samples_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_all_requests

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setattr(eval_all_requests.db, "DB_PATH", str(db_path), raising=False)

    fixtures = eval_all_requests.sample_real_tags()
    clan, _war = eval_all_requests._fake_clan_ctx()

    assert fixtures["our_member_tags"] == [("Eval Alice", "#AAA111")]
    assert fixtures["external_clan_tags"] == [("Outside Clan", "#EXT999")]
    assert fixtures["external_player_tags"] == [("Opponent Bob", "#OPP222")]
    assert clan["members"] == [{"tag": "#AAA111", "name": "Eval Alice"}]


def test_eval_card_conversations_builds_context_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_card_conversations

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setattr(eval_card_conversations.db, "DB_PATH", str(db_path), raising=False)

    clan, _war = eval_card_conversations._build_clan_war_context()

    assert clan["members"] == [{"tag": "#AAA111", "name": "Eval Alice"}]


def test_eval_leader_actions_scores_exact_artifacts(tmp_path):
    from scripts import eval_leader_actions

    db_path = tmp_path / "leader-actions.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_leader_action_eval_schema(conn)
        _insert_leader_action(conn)
        conn.execute(
            """
            INSERT INTO messages VALUES (
                '2001', '1513758211206025227', 'assistant', 'arena-relay',
                'welcome_relay', 'Leader action card body', 'Leader action R1',
                '2026-06-24T12:00:01', '{"raw": true}', 99
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages VALUES (
                '2002', '1513758211206025227', 'assistant', 'arena-relay',
                'welcome_relay', 'Welcome Eval Alice. - E', 'Copy body',
                '2026-06-24T12:00:02', '{"copy": true}', 99
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_leader_actions.evaluate(
        db_path,
        since=eval_leader_actions._parse_time("2026-06-24T00:00:00Z"),
        end=eval_leader_actions._parse_time("2026-06-25T00:00:00Z"),
    )

    assert result["passed"] is True
    assert result["metrics"]["decision_rate"]["value"] == 1.0
    assert result["metrics"]["trace_rate"]["value"] == 1.0
    assert result["metrics"]["relay_copy_text_rate"]["value"] == 1.0
    assert result["artifacts"][0]["source_message"]["workflow"] == "arena-relay"
    assert result["artifacts"][0]["copy_messages"][0]["content"] == "Welcome Eval Alice. - E"


def test_eval_leader_actions_flags_stale_open_cards(tmp_path):
    from scripts import eval_leader_actions

    db_path = tmp_path / "leader-actions-stale.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_leader_action_eval_schema(conn)
        _insert_leader_action(
            conn,
            action_id=2,
            action_key="leader-action:2",
            action_type="in_game_relay",
            status="proposed",
            proposed_at="2026-06-20T12:00:00Z",
            decided_at=None,
            source_message_id="3001",
            copy_message_id="3002",
            copy_message_ids_json='["3002"]',
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_leader_actions.evaluate(
        db_path,
        since=eval_leader_actions._parse_time("2026-06-20T00:00:00Z"),
        end=eval_leader_actions._parse_time("2026-06-25T00:00:00Z"),
    )

    assert result["passed"] is False
    assert result["metrics"]["stale_open_count"]["value"] == 1
    assert result["stale_open_action_ids"] == [2]


def test_eval_player_highlights_scores_exact_artifacts(tmp_path):
    from scripts import eval_player_highlights

    db_path = tmp_path / "player-highlights.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_player_highlight_eval_schema(conn)
        _insert_player_highlight_intent(conn)
        conn.execute(
            """
            INSERT INTO messages VALUES (
                '4001', '1482352147029950474', 'assistant', 'v5-reactive',
                'celebrate:best_trophies_peak', '**Eval Alice** just hit 6000 trophies.',
                'Player highlight body', '2026-06-24T12:00:02Z', '{"raw": true}', 11
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_player_highlights.evaluate(
        db_path,
        since=eval_player_highlights._parse_time("2026-06-24T00:00:00Z"),
        end=eval_player_highlights._parse_time("2026-06-25T00:00:00Z"),
    )

    assert result["passed"] is True
    assert result["metrics"]["delivery_rate"]["value"] == 1.0
    assert result["metrics"]["trace_rate"]["value"] == 1.0
    assert result["metrics"]["message_id_rate"]["value"] == 1.0
    assert result["metrics"]["exact_copy_rate"]["value"] == 1.0
    assert result["metrics"]["non_meta_copy_rate"]["value"] == 1.0
    assert result["artifacts"][0]["exact_copies"][0]["source"] == "messages"
    assert result["artifacts"][0]["messages"][0]["discord_message_id"] == "4001"


def test_eval_player_highlights_flags_meta_copy(tmp_path):
    from scripts import eval_player_highlights

    db_path = tmp_path / "player-highlights-meta.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_player_highlight_eval_schema(conn)
        _insert_player_highlight_intent(
            conn,
            payload_json=(
                '{"message_ids": ["4002"], "posted_messages": ['
                '{"discord_message_id": "4002", '
                '"content": "Signal data inconsistent with player profile; skipping post", '
                '"discord_created_at": "2026-06-24T12:00:02Z"}]}'
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_player_highlights.evaluate(
        db_path,
        since=eval_player_highlights._parse_time("2026-06-24T00:00:00Z"),
        end=eval_player_highlights._parse_time("2026-06-25T00:00:00Z"),
    )

    assert result["passed"] is False
    assert result["metrics"]["non_meta_copy_rate"]["value"] == 0.0
    assert result["meta_copy_intent_ids"] == [11]
