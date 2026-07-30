from __future__ import annotations

import sqlite3


def test_active_eval_harnesses_open_the_current_v51_schema(tmp_path, monkeypatch):
    from scripts import eval_all_requests, eval_card_conversations

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setenv("ELIXIR_DB_PATH", str(db_path))

    fixtures = eval_all_requests.sample_real_tags()
    assert set(fixtures) == {
        "our_member_tags",
        "external_clan_tags",
        "external_player_tags",
    }
    clan, war = eval_all_requests._fake_clan_ctx()
    assert clan["tag"] and clan["memberList"] == clan["members"]
    assert war == {}
    card_clan, card_war = eval_card_conversations._build_clan_war_context()
    assert card_clan["memberList"] == card_clan["members"]
    assert card_war == {}


def _seed_eval_fixture_db(path):
    import db

    conn = db.get_connection(str(path))
    try:
        conn.execute(
            "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-06-25T17:00:00Z', "
            "'2026-06-25T17:00:00Z', 1), "
            "('#EXT999', 'Outside Clan', '2026-06-25T17:00:00Z', "
            "'2026-06-25T17:00:00Z', 0)"
        )
        conn.execute(
            "INSERT INTO players "
            "(player_tag, current_name, first_seen_at, last_seen_at) VALUES "
            "('#AAA111', 'Eval Alice', '2026-06-25T17:00:00Z', "
            "'2026-06-25T17:00:00Z')"
        )
        conn.execute(
            "INSERT INTO clan_memberships "
            "(player_tag, clan_tag, joined_at, join_source) VALUES "
            "('#AAA111', '#J2RGCRVG', '2026-06-25T17:00:00Z', 'test')"
        )
        conn.execute(
            "INSERT INTO battle_events "
            "(dedup_key, player_tag, battle_time, observed_at, opponent_tag) "
            "VALUES ('eval-battle', '#AAA111', '20260625T170000.000Z', "
            "'2026-06-25T17:00:00Z', '#OPP222')"
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
            ui_version TEXT
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


def _create_ask_elixir_alignment_schema(conn):
    conn.execute(
        """
        CREATE TABLE messages (
            message_id INTEGER PRIMARY KEY,
            discord_message_id TEXT,
            thread_id INTEGER,
            channel_id TEXT,
            discord_user_id TEXT,
            member_id INTEGER,
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
        "target_channel_key": "actions",
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
    }
    row.update(overrides)
    columns = list(row)
    conn.execute(
        f"INSERT INTO leader_action_recommendations ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def _insert_alignment_message(conn, **overrides):
    row = {
        "message_id": 1,
        "discord_message_id": "5001",
        "thread_id": 99,
        "channel_id": "1482368505058955467",
        "discord_user_id": "u1",
        "member_id": 1,
        "author_type": "user",
        "workflow": "interactive",
        "event_type": None,
        "content": "Who are the donation leaders?",
        "summary": "Who are the donation leaders?",
        "created_at": "2026-06-25T17:24:00Z",
        "raw_json": None,
        "intent_id": None,
    }
    row.update(overrides)
    columns = list(row)
    conn.execute(
        f"INSERT INTO messages ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def test_eval_all_requests_samples_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_all_requests

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setenv("ELIXIR_DB_PATH", str(db_path))

    fixtures = eval_all_requests.sample_real_tags()
    clan, _war = eval_all_requests._fake_clan_ctx()

    assert fixtures["our_member_tags"] == [("Eval Alice", "#AAA111")]
    assert fixtures["external_clan_tags"] == [("Outside Clan", "#EXT999")]
    assert fixtures["external_player_tags"] == [("#OPP222", "#OPP222")]
    assert clan["members"] == [{"tag": "#AAA111", "name": "Eval Alice"}]


def test_eval_card_conversations_builds_context_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_card_conversations

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setenv("ELIXIR_DB_PATH", str(db_path))

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
                '2001', '1513758211206025227', 'assistant', 'actions',
                'welcome_relay', 'Leader action card body', 'Leader action R1',
                '2026-06-24T12:00:01', '{"raw": true}', 99
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages VALUES (
                '2002', '1513758211206025227', 'assistant', 'actions',
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
    assert result["artifacts"][0]["source_message"]["workflow"] == "actions"
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


def test_eval_ask_elixir_alignment_flags_blank_user_reply(tmp_path):
    from scripts import eval_ask_elixir_alignment

    db_path = tmp_path / "ask-elixir-alignment.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_ask_elixir_alignment_schema(conn)
        _insert_alignment_message(
            conn,
            content="",
            summary="",
            workflow="deck_review",
            created_at="2026-06-25T17:26:11Z",
        )
        _insert_alignment_message(
            conn,
            message_id=2,
            discord_message_id="5002",
            author_type="assistant",
            workflow="deck_review",
            event_type="deck_review_response",
            content="**War Deck Review** stale answer.",
            summary="War deck review.",
            created_at="2026-06-25T17:26:32Z",
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_ask_elixir_alignment.evaluate(
        db_path,
        since=eval_ask_elixir_alignment._parse_time("2026-06-25T00:00:00Z"),
        end=eval_ask_elixir_alignment._parse_time("2026-06-26T00:00:00Z"),
        log_paths=[],
    )

    assert result["passed"] is False
    assert result["metrics"]["blank_user_reply_count"]["value"] == 1
    assert result["findings"]["blank_user_replies"][0]["assistant_message_id"] == 2


def test_eval_ask_elixir_alignment_flags_donation_topic_mismatch(tmp_path):
    from scripts import eval_ask_elixir_alignment

    db_path = tmp_path / "ask-elixir-topic.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_ask_elixir_alignment_schema(conn)
        _insert_alignment_message(conn)
        _insert_alignment_message(
            conn,
            message_id=2,
            discord_message_id="5002",
            author_type="assistant",
            workflow="deck_review",
            event_type="deck_review_response",
            content="**War Deck Review** Your four decks need Arrows coverage.",
            summary="War deck review.",
            created_at="2026-06-25T17:24:30Z",
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_ask_elixir_alignment.evaluate(
        db_path,
        since=eval_ask_elixir_alignment._parse_time("2026-06-25T00:00:00Z"),
        end=eval_ask_elixir_alignment._parse_time("2026-06-26T00:00:00Z"),
        log_paths=[],
    )

    assert result["passed"] is False
    assert result["metrics"]["topic_mismatch_count"]["value"] == 1
    assert result["findings"]["topic_mismatches"][0]["domain"] == "donations"


def test_eval_ask_elixir_alignment_flags_not_for_bot_in_open_lane(tmp_path):
    from scripts import eval_ask_elixir_alignment

    db_path = tmp_path / "ask-elixir-not-for-bot.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_ask_elixir_alignment_schema(conn)
        conn.commit()
    finally:
        conn.close()

    log_path = tmp_path / "elixir-v5.log"
    log_path.write_text(
        "2026-06-25 12:24:27,643 [INFO] elixir: intent_router mode=dispatch "
        "channel_id=1482368505058955467 author_id=1422141611122491503 "
        "workflow=interactive mentioned=False route=not_for_bot confidence=0.95 "
        "sub_mode=None target_member=None latency_ms=1369.2 fallback_reason=None "
        "rationale='asking clan members' raw_question='Who is donating beast in our clan???'\n"
    )

    result = eval_ask_elixir_alignment.evaluate(
        db_path,
        since=eval_ask_elixir_alignment._parse_time("2026-06-25T00:00:00Z"),
        end=eval_ask_elixir_alignment._parse_time("2026-06-26T00:00:00Z"),
        log_paths=[str(log_path)],
    )

    assert result["passed"] is False
    assert result["metrics"]["not_for_bot_route_count"]["value"] == 1
    finding = result["findings"]["not_for_bot_routes"][0]
    assert finding["raw_question"] == "Who is donating beast in our clan???"


def test_eval_ask_elixir_alignment_flags_ignored_question_then_blank_route(tmp_path):
    from scripts import eval_ask_elixir_alignment

    db_path = tmp_path / "ask-elixir-log.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_ask_elixir_alignment_schema(conn)
        conn.commit()
    finally:
        conn.close()

    log_path = tmp_path / "elixir-v5.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-06-25 12:24:27,643 [INFO] elixir: intent_router mode=dispatch "
                "channel_id=1482368505058955467 author_id=1422141611122491503 "
                "workflow=interactive mentioned=False route=not_for_bot confidence=0.95 "
                "sub_mode=None target_member=None latency_ms=1369.2 fallback_reason=None "
                "rationale='asking clan members' raw_question='Who is donating beast in our clan???'",
                "2026-06-25 12:26:11,119 [INFO] elixir: message_route "
                "route=deck_review_war_review channel_id=1482368505058955467 "
                "author_id=1422141611122491503 mentioned=True lane=ask-elixir "
                "workflow=interactive deck_target=None mode='war' subject='review' "
                "raw_question='' original='<@1477043197443182832>'",
            ]
        )
    )

    result = eval_ask_elixir_alignment.evaluate(
        db_path,
        since=eval_ask_elixir_alignment._parse_time("2026-06-25T00:00:00Z"),
        end=eval_ask_elixir_alignment._parse_time("2026-06-26T00:00:00Z"),
        log_paths=[str(log_path)],
    )

    assert result["passed"] is False
    assert result["metrics"]["not_for_bot_route_count"]["value"] == 1
    assert result["metrics"]["blank_route_count"]["value"] == 1
    assert result["metrics"]["ignored_question_blank_route_count"]["value"] == 1
    finding = result["findings"]["ignored_question_blank_routes"][0]
    assert finding["ignored_question"] == "Who is donating beast in our clan???"
    assert finding["blank_route"] == "deck_review_war_review"


def test_eval_ask_elixir_alignment_accepts_open_lane_llm_chat_and_ignored_blank_mention(
    tmp_path,
):
    from scripts import eval_ask_elixir_alignment

    db_path = tmp_path / "ask-elixir-fixed.db"
    conn = sqlite3.connect(db_path)
    try:
        _create_ask_elixir_alignment_schema(conn)
        _insert_alignment_message(
            conn,
            content="Who is donating beast in our clan???",
            summary="Who is donating beast in our clan???",
        )
        _insert_alignment_message(
            conn,
            message_id=2,
            discord_message_id="5002",
            author_type="assistant",
            workflow="interactive",
            event_type="channel_response",
            content="The current donation leaders are Finn and King Levy.",
            summary="Donation leaders.",
            created_at="2026-06-25T17:24:30Z",
        )
        conn.commit()
    finally:
        conn.close()

    log_path = tmp_path / "elixir-v5.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-06-25 12:24:27,643 [INFO] elixir: intent_router mode=dispatch "
                "channel_id=1482368505058955467 author_id=1422141611122491503 "
                "workflow=interactive mentioned=False route=llm_chat confidence=0.95 "
                "sub_mode=None target_member=None latency_ms=1369.2 "
                "fallback_reason='open_channel_not_for_bot_override' "
                "rationale='Open bot lane question classified not_for_bot; falling back to chat.' "
                "raw_question='Who is donating beast in our clan???'",
                "2026-06-25 12:26:11,119 [INFO] elixir: "
                "empty_addressed_message_ignored channel_id=1482368505058955467 "
                "author_id=1422141611122491503 mentioned=True lane=ask-elixir "
                "workflow=interactive original='<@1477043197443182832>'",
            ]
        )
    )

    result = eval_ask_elixir_alignment.evaluate(
        db_path,
        since=eval_ask_elixir_alignment._parse_time("2026-06-25T00:00:00Z"),
        end=eval_ask_elixir_alignment._parse_time("2026-06-26T00:00:00Z"),
        log_paths=[str(log_path)],
    )

    assert result["passed"] is True
    assert result["metrics"]["not_for_bot_route_count"]["value"] == 0
    assert result["metrics"]["blank_route_count"]["value"] == 0
    assert result["metrics"]["ignored_question_blank_route_count"]["value"] == 0
    assert result["metrics"]["topic_mismatch_count"]["value"] == 0
