"""Focused tests for the V2 database baseline."""

import json
from datetime import datetime, timedelta, timezone
import sqlite3

import db


def test_v2_schema_initializes_core_tables():
    conn = db.get_connection(":memory:")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(db._MIGRATIONS)

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        expected = {
            "members",
            "member_metadata",
            "discord_users",
            "discord_links",
            "messages",
            "memory_facts",
            "member_current_state",
            "member_state_snapshots",
            "member_battle_facts",
            "member_recent_form",
            "war_races",
            "war_participation",
            "raw_api_payloads",
        }
        assert expected.issubset(tables)
    finally:
        conn.close()


def test_get_connection_rebuilds_legacy_database_with_stale_version(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute("CREATE TABLE leader_conversations (id INTEGER PRIMARY KEY)")
        legacy.execute("PRAGMA user_version = 2")
        legacy.commit()
    finally:
        legacy.close()

    conn = db.get_connection(str(db_path))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(db._MIGRATIONS)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "discord_users" in tables
        assert "leader_conversations" not in tables
    finally:
        conn.close()

    backups = list(tmp_path.glob("legacy.db.legacy-v2-backup-*"))
    assert len(backups) == 1


def test_snapshot_members_populates_current_state_membership_and_history():
    conn = db.get_connection(":memory:")
    try:
        members = [
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "role": "coLeader",
                "lastSeen": "20260307T031701.000Z",
                "expLevel": 66,
                "trophies": 11429,
                "bestTrophies": 11433,
                "clanRank": 3,
                "previousClanRank": 3,
                "donations": 154,
                "donationsReceived": 80,
                "arena": {"id": 54000131, "name": "Musketeer Street", "rawName": "Arena_L13"},
            }
        ]

        stored = db.snapshot_members(members, conn=conn)
        assert stored == 1

        roster = db.get_active_roster_map(conn=conn)
        assert roster == {"#ABC123": "King Levy"}

        history = db.get_member_history("#ABC123", conn=conn)
        assert len(history) == 1
        assert history[0]["trophies"] == 11429
        assert history[0]["role"] == "coLeader"

        extras = db.get_member_metadata_map(conn=conn)
        assert extras["ABC123"]["joined_date"] is None
    finally:
        conn.close()


def test_discord_link_and_member_reference_formatting():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.link_discord_user_to_member(
            "1474760692992180429",
            "#ABC123",
            username="jamie",
            display_name="King Levy",
            source="verified_nickname_match",
            conn=conn,
        )

        identity = db.get_member_identity("#ABC123", conn=conn)
        assert identity["in_discord"] == 1
        assert identity["discord_username"] == "jamie"

        assert db.format_member_reference("#ABC123", conn=conn) == "King Levy"
        assert (
            db.format_member_reference("#ABC123", style="name_with_handle", conn=conn)
            == "King Levy (<@1474760692992180429>)"
        )
        assert (
            db.format_member_reference("#ABC123", style="name_with_mention", conn=conn)
            == "King Levy (<@1474760692992180429>)"
        )
    finally:
        conn.close()


def test_member_metadata_csv_export_includes_read_only_and_editable_fields(tmp_path):
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "coLeader"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-01-15", conn=conn)
        db.set_member_birthday("#ABC123", "King Levy", 2, 14, conn=conn)
        db.set_member_profile_url("#ABC123", "King Levy", "https://example.com", conn=conn)
        db.set_member_note("#ABC123", "King Levy", "Founder", conn=conn)
        db.link_discord_user_to_member(
            "1474760692992180429",
            "#ABC123",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )

        csv_path = tmp_path / "member-metadata.csv"
        rows_written = db.export_member_metadata_csv(str(csv_path), conn=conn)

        assert rows_written == 1
        contents = csv_path.read_text()
        assert "player_tag,current_name,status,role,discord_username,discord_display_name" in contents
        assert "#ABC123" in contents
        assert "2024-01-15" in contents
        assert "Founder" in contents
    finally:
        conn.close()


def test_member_metadata_csv_import_updates_and_dry_run(tmp_path):
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        csv_path = tmp_path / "member-metadata.csv"
        csv_path.write_text(
            "player_tag,current_name,status,role,discord_username,discord_display_name,effective_joined_date,joined_date_override,birth_month,birth_day,profile_url,note\n"
            "#ABC123,King Levy,active,member,,,,2024-01-15,3,7,https://example.com,Founder\n"
        )

        dry_run = db.import_member_metadata_csv(str(csv_path), dry_run=True, conn=conn)
        assert dry_run["updated"] == 1
        assert dry_run["errors"] == []
        assert db.get_member_profile("#ABC123", conn=conn)["joined_date"] is None

        result = db.import_member_metadata_csv(str(csv_path), conn=conn)
        assert result["updated"] == 1
        profile = db.get_member_profile("#ABC123", conn=conn)
        assert profile["joined_date"] == "2024-01-15"
        assert profile["birth_month"] == 3
        assert profile["birth_day"] == 7
        assert profile["profile_url"] == "https://example.com"
        assert profile["note"] == "Founder"
    finally:
        conn.close()


def test_join_anniversary_uses_effective_join_date_override():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-03-07", conn=conn)

        anniversaries = db.get_join_anniversaries_today("2026-03-07", conn=conn)
        assert anniversaries == [
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "joined_date": "2024-03-07",
                "years": 2,
            }
        ]
    finally:
        conn.close()


def test_list_thread_messages_reads_thread_history_from_message_store():
    conn = db.get_connection(":memory:")
    try:
        db.save_message("leader:user123", "user", "Who should we promote?", workflow="leader", conn=conn)
        db.save_message("leader:user123", "assistant", "Vijay looks ready.", workflow="leader", conn=conn)

        history = db.list_thread_messages("leader:user123", conn=conn)
        assert [turn["role"] for turn in history] == ["user", "assistant"]
        assert history[0]["content"] == "Who should we promote?"
        assert history[1]["content"] == "Vijay looks ready."

        thread = conn.execute(
            "SELECT scope_type, scope_key FROM conversation_threads"
        ).fetchone()
        assert dict(thread) == {"scope_type": "leader", "scope_key": "user123"}
    finally:
        conn.close()


def test_save_message_auto_links_discord_user_to_member_identity():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.link_discord_user_to_member(
            "1474760692992180429",
            "#ABC123",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )
        db.save_message(
            "leader:1474760692992180429",
            "user",
            "How am I doing lately?",
            discord_user_id="1474760692992180429",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )

        stored = conn.execute(
            "SELECT member_id FROM messages ORDER BY message_id DESC LIMIT 1"
        ).fetchone()
        member = conn.execute(
            "SELECT player_tag FROM members WHERE member_id = ?",
            (stored["member_id"],),
        ).fetchone()
        assert member["player_tag"] == "#ABC123"

        memory = db.build_memory_context(
            discord_user_id="1474760692992180429",
            member_tag="#ABC123",
            conn=conn,
        )
        assert memory["discord_user"]["episodes"]
        assert memory["member"]["episodes"]
        assert memory["discord_user"]["facts"][0]["fact_type"] == "last_user_summary"
    finally:
        conn.close()


def test_channel_messages_and_state_are_tracked_for_channel_user_threads():
    conn = db.get_connection(":memory:")
    try:
        db.save_message(
            "channel_user:100:123",
            "assistant",
            "Keep an eye on war usage today.",
            channel_id=100,
            channel_name="clan-ops",
            channel_kind="text",
            workflow="clanops",
            conn=conn,
        )

        history = db.list_channel_messages(100, conn=conn)
        state = db.get_channel_state(100, conn=conn)

        assert history == [
            {
                "role": "assistant",
                "content": "Keep an eye on war usage today.",
                "author_name": None,
                "recorded_at": history[0]["recorded_at"],
            }
        ]
        assert state["last_summary"] == "Keep an eye on war usage today."
        assert state["last_elixir_post_at"]
    finally:
        conn.close()


def test_resolve_member_matches_at_prefixed_discord_display_name():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )
        db.link_discord_user_to_member(
            "456",
            "#DEF456",
            username="vijay_alt",
            display_name="Vijay",
            conn=conn,
        )

        matches = db.resolve_member("@Vijay", conn=conn)

        assert matches[0]["player_tag"] == "#DEF456"
        assert matches[0]["match_source"] == "discord_display_exact"
    finally:
        conn.close()


def test_prompt_failures_are_recorded_and_listed_for_review():
    conn = db.get_connection(":memory:")
    try:
        failure_id = db.record_prompt_failure(
            "Are there any members who have dropped in trophies significantly this week?",
            "agent_none",
            "respond_in_channel",
            workflow="clanops",
            channel_id=200,
            channel_name="clan-ops",
            discord_user_id=123,
            discord_message_id=555,
            detail="model returned null after tool call",
            result_preview='{"event_type":"channel_response","content":null}',
            openai_last_error="Error code: 429 rate_limit_exceeded",
            openai_last_model="gpt-4.1-mini",
            openai_last_call_at="2026-03-07T19:12:00",
            raw_json={"event_type": "channel_response", "content": None},
            conn=conn,
        )

        failures = db.list_prompt_failures(conn=conn)

        assert len(failures) == 1
        assert failures[0]["failure_id"] == failure_id
        assert failures[0]["workflow"] == "clanops"
        assert failures[0]["failure_type"] == "agent_none"
        assert failures[0]["channel_name"] == "clan-ops"
        assert failures[0]["openai_last_model"] == "gpt-4.1-mini"
        assert json.loads(failures[0]["raw_json"]) == {"event_type": "channel_response", "content": None}
    finally:
        conn.close()


def test_get_system_status_summarizes_v2_data_layer():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 66,
                "trophies": 11429,
                "bestTrophies": 11500,
                "currentDeck": [{"name": "Knight"}] * 8,
                "cards": [{"name": "Knight", "level": 16}],
            },
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#ABC123",
            [{
                "battleTime": "20260307T120000.000Z",
                "type": "PvP",
                "gameMode": {"name": "Ladder", "id": 72000006},
                "team": [{"crowns": 3, "trophyChange": 30, "startingTrophies": 11400, "cards": [{"name": "Knight"}] * 8}],
                "opponent": [{"crowns": 1, "tag": "#XYZ999", "name": "Opponent", "clan": {"tag": "#CLAN"}}],
            }],
            conn=conn,
        )
        db.upsert_war_current_state(
            {"state": "warDay", "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "participants": []}},
            conn=conn,
        )
        db.save_message(
            "channel_user:100:123",
            "assistant",
            "Status is healthy.",
            channel_id=100,
            channel_name="clan-ops",
            channel_kind="text",
            workflow="clanops",
            conn=conn,
        )

        status = db.get_system_status(conn=conn)

        assert status["schema_version"] == len(db._MIGRATIONS)
        assert status["schema_display"] == f"V2 baseline (migration v{len(db._MIGRATIONS)})"
        assert status["counts"]["members_active"] == 1
        assert status["counts"]["battle_fact_count"] == 1
        assert status["counts"]["message_count"] == 1
        assert status["freshness"]["member_state_at"] is not None
        assert status["freshness"]["player_profile_at"] is not None
        assert status["freshness"]["battle_fact_at"] is not None
        assert status["freshness"]["war_state_at"] is not None
        assert isinstance(status["raw_payloads_by_endpoint"], list)
    finally:
        conn.close()


def test_profile_and_battlelog_snapshots_power_deck_cards_and_recent_form():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        profile = {
            "tag": "#ABC123",
            "name": "King Levy",
            "expLevel": 66,
            "trophies": 11429,
            "bestTrophies": 11433,
            "wins": 100,
            "losses": 90,
            "battleCount": 190,
            "totalDonations": 5000,
            "donations": 100,
            "donationsReceived": 50,
            "warDayWins": 3,
            "challengeMaxWins": 5,
            "challengeCardsWon": 25,
            "tournamentBattleCount": 10,
            "tournamentCardsWon": 100,
            "threeCrownWins": 20,
            "currentFavouriteCard": {"id": 26000011, "name": "Valkyrie"},
            "currentDeck": [
                {"name": "Valkyrie", "level": 14, "maxLevel": 14, "rarity": "rare", "iconUrls": {"medium": "icon://valk"}},
                {"name": "Goblin Barrel", "level": 10, "maxLevel": 11, "rarity": "epic", "iconUrls": {"medium": "icon://gb"}},
                {"name": "Princess", "level": 6, "maxLevel": 8, "rarity": "legendary", "iconUrls": {"medium": "icon://princess"}},
                {"name": "Knight", "level": 16, "maxLevel": 16, "rarity": "common", "iconUrls": {"medium": "icon://knight"}},
                {"name": "Rocket", "level": 14, "maxLevel": 14, "rarity": "rare", "iconUrls": {"medium": "icon://rocket"}},
                {"name": "Ice Spirit", "level": 16, "maxLevel": 16, "rarity": "common", "iconUrls": {"medium": "icon://ice"}},
                {"name": "Inferno Tower", "level": 10, "maxLevel": 11, "rarity": "epic", "iconUrls": {"medium": "icon://inferno"}},
                {"name": "Log", "level": 8, "maxLevel": 8, "rarity": "legendary", "iconUrls": {"medium": "icon://log"}},
            ],
            "cards": [],
            "badges": [],
            "achievements": [],
            "leagueStatistics": {"currentSeason": {"trophies": 11429}},
        }
        db.snapshot_player_profile(profile, conn=conn)

        battle_log = [
            {
                "type": "PvP",
                "battleTime": "20260307T100000.000Z",
                "gameMode": {"id": 72000006, "name": "Ladder"},
                "deckSelection": "collection",
                "arena": {"id": 1, "name": "Arena 1"},
                "team": [{
                    "tag": "#ABC123",
                    "name": "King Levy",
                    "crowns": 2,
                    "trophyChange": 30,
                    "startingTrophies": 11400,
                    "cards": profile["currentDeck"],
                    "supportCards": [],
                }],
                "opponent": [{
                    "tag": "#DEF456",
                    "name": "Opponent",
                    "crowns": 1,
                    "cards": [],
                }],
            },
            {
                "type": "PvP",
                "battleTime": "20260307T090000.000Z",
                "gameMode": {"id": 72000006, "name": "Ladder"},
                "deckSelection": "collection",
                "arena": {"id": 1, "name": "Arena 1"},
                "team": [{
                    "tag": "#ABC123",
                    "name": "King Levy",
                    "crowns": 0,
                    "trophyChange": -25,
                    "startingTrophies": 11370,
                    "cards": profile["currentDeck"],
                    "supportCards": [],
                }],
                "opponent": [{
                    "tag": "#XYZ789",
                    "name": "Opponent 2",
                    "crowns": 1,
                    "cards": [],
                }],
            },
        ]
        db.snapshot_player_battlelog("#ABC123", battle_log, conn=conn)

        deck = db.get_member_current_deck("#ABC123", conn=conn)
        assert deck["cards"][0]["name"] == "Valkyrie"
        assert deck["cards"][0]["level"] == 16
        assert deck["cards"][0]["api_level"] == 14
        assert deck["cards"][1]["level"] == 15
        assert deck["cards"][1]["api_level"] == 10

        cards = db.get_member_signature_cards("#ABC123", conn=conn)
        assert cards["sample_battles"] == 2
        assert cards["cards"][0]["name"] == "Valkyrie"

        form = db.get_member_recent_form("#ABC123", conn=conn)
        assert form["wins"] == 1
        assert form["losses"] == 1
        assert form["sample_size"] == 2
    finally:
        conn.close()


def test_snapshot_player_profile_detects_level_and_card_milestones():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 65,
                "currentDeck": [],
                "cards": [
                    {"name": "Fireball", "level": 10, "maxLevel": 11, "rarity": "epic"},
                    {"name": "Knight", "level": 13, "maxLevel": 16, "rarity": "common"},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 66,
                "currentDeck": [],
                "cards": [
                    {"name": "Fireball", "level": 11, "maxLevel": 11, "rarity": "epic"},
                    {"name": "Knight", "level": 14, "maxLevel": 16, "rarity": "common"},
                ],
            },
            conn=conn,
        )

        assert any(sig["type"] == "player_level_up" and sig["new_level"] == 66 for sig in signals)
        assert any(sig["type"] == "card_level_milestone" and sig["card_name"] == "Fireball" and sig["milestone"] == 16 for sig in signals)
        assert any(sig["type"] == "card_level_milestone" and sig["card_name"] == "Knight" and sig["milestone"] == 14 for sig in signals)
    finally:
        conn.close()


def test_role_change_and_war_battle_queries():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )
        conn.execute(
            "UPDATE member_current_state SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.execute(
            "UPDATE member_state_snapshots SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.commit()
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )

        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "riverRacePvP",
                    "battleTime": "20260302T100000.000Z",
                    "gameMode": {"id": 72000061, "name": "River Race PvP"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#ZZZ111", "name": "Opp 1", "crowns": 1, "cards": []}],
                },
                {
                    "type": "riverRacePvP",
                    "battleTime": "20260303T100000.000Z",
                    "gameMode": {"id": 72000061, "name": "River Race PvP"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 0, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#ZZZ222", "name": "Opp 2", "crowns": 1, "cards": []}],
                },
            ],
            conn=conn,
        )

        changes = db.get_recent_role_changes(days=30, conn=conn)
        assert changes[0]["old_role"] == "member"
        assert changes[0]["new_role"] == "elder"

        attendance = db.get_member_war_attendance("#ABC123", season_id=129, conn=conn)
        assert attendance["season"]["races_played"] == 1
        assert attendance["season"]["participation_rate"] == 1.0

        record = db.get_member_war_battle_record("#ABC123", season_id=129, conn=conn)
        assert record["wins"] == 1
        assert record["losses"] == 1
        assert record["win_rate"] == 0.5

        win_rates = db.get_war_battle_win_rates(season_id=129, conn=conn)
        assert win_rates["members"][0]["tag"] == "#ABC123"
        assert win_rates["members"][0]["win_rate"] == 0.5
    finally:
        conn.close()


def test_boat_battle_trend_and_missed_day_queries():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 128,
                        "sectionIndex": 1,
                        "createdDate": "20260215T120000.000Z",
                        "standings": [{"rank": 2, "trophyChange": -50, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10000, "finishTime": "20260215T180000.000Z", "participants": [{"tag": "#ABC123", "name": "King Levy", "fame": 2000, "repairPoints": 0, "boatAttacks": 1, "decksUsed": 4, "decksUsedToday": 0}]}}],
                    },
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [{"rank": 1, "trophyChange": 100, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 12000, "finishTime": "20260301T180000.000Z", "participants": [{"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 1, "decksUsed": 4, "decksUsedToday": 0}, {"tag": "#DEF456", "name": "Vijay", "fame": 2400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0}]}}],
                    },
                    {
                        "seasonId": 129,
                        "sectionIndex": 2,
                        "createdDate": "20260308T120000.000Z",
                        "standings": [{"rank": 1, "trophyChange": 100, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 14000, "finishTime": "20260308T180000.000Z", "participants": [{"tag": "#ABC123", "name": "King Levy", "fame": 3700, "repairPoints": 0, "boatAttacks": 1, "decksUsed": 4, "decksUsedToday": 0}, {"tag": "#DEF456", "name": "Vijay", "fame": 2500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0}]}}],
                    },
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        conn.execute(
            "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES (?, 'full', '#J2RGCRVG', 'POAP KINGS', 5000, 0, 0, 120, '{}')",
            ("2026-02-10T10:00:00",),
        )
        conn.execute(
            "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES (?, 'full', '#J2RGCRVG', 'POAP KINGS', 7000, 0, 0, 150, '{}')",
            ("2026-03-05T10:00:00",),
        )
        conn.execute(
            "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, raw_json) "
            "VALUES ((SELECT member_id FROM members WHERE player_tag = '#ABC123'), '2026-03-02', '2026-03-02T09:00:00', 400, 0, 0, 4, 1, '{}')"
        )
        conn.execute(
            "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, raw_json) "
            "VALUES ((SELECT member_id FROM members WHERE player_tag = '#ABC123'), '2026-03-03', '2026-03-03T09:00:00', 700, 0, 0, 8, 0, '{}')"
        )
        conn.commit()

        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "boatBattle",
                    "battleTime": "20260301T150000.000Z",
                    "gameMode": {"id": 72000062, "name": "Boat Battle"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": []}],
                },
                {
                    "type": "boatBattle",
                    "battleTime": "20260308T150000.000Z",
                    "gameMode": {"id": 72000062, "name": "Boat Battle"},
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 0, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 1, "cards": []}],
                },
            ],
            conn=conn,
        )

        boat = db.get_clan_boat_battle_record(wars=2, conn=conn)
        assert boat["wars_considered"] == 2
        assert boat["wins"] == 1
        assert boat["losses"] == 1

        trend = db.get_war_score_trend(days=30, conn=conn)
        assert trend["direction"] == "up"
        assert trend["score_change"] == 30

        compare = db.compare_fame_per_member_to_previous_season(season_id=129, conn=conn)
        assert compare["direction"] == "up"
        assert compare["current"]["fame_per_member"] == 13000.0

        missed = db.get_member_missed_war_days("#ABC123", season_id=129, conn=conn)
        assert missed["tracked_days"] == 2
        assert missed["days_missed"] == 1
        assert missed["missed_dates"] == ["2026-03-03"]
    finally:
        conn.close()


def test_war_status_queries_use_v2_tables():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 6622,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [
                        {
                            "tag": "#ABC123",
                            "name": "King Levy",
                            "fame": 400,
                            "repairPoints": 0,
                            "boatAttacks": 0,
                            "decksUsed": 2,
                            "decksUsedToday": 1,
                        }
                    ],
                },
            },
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 3,
                        "createdDate": "20260302T095140.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12850,
                                    "finishTime": "19691231T235959.000Z",
                                    "participants": [
                                        {
                                            "tag": "#ABC123",
                                            "name": "King Levy",
                                            "fame": 2400,
                                            "repairPoints": 0,
                                            "boatAttacks": 0,
                                            "decksUsed": 12,
                                            "decksUsedToday": 0,
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        war = db.get_current_war_status(conn=conn)
        assert war["war_state"] == "full"
        assert war["season_id"] == 129
        assert war["phase"] is None
        assert war["battle_phase_active"] is False
        assert war["practice_phase_active"] is False

        today = db.get_war_deck_status_today(conn=conn)
        assert today["total_participants"] == 1
        assert today["used_some"][0]["name"] == "King Levy"

        member_war = db.get_member_war_status("#ABC123", conn=conn)
        assert member_war["season"]["races_played"] == 1
        assert member_war["current_day"]["decks_left_today"] == 3
    finally:
        conn.close()


def test_current_war_status_infers_new_season_after_section_index_rollover():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 3,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12850,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {
                                            "tag": "#ABC123",
                                            "name": "King Levy",
                                            "fame": 2400,
                                            "repairPoints": 0,
                                            "boatAttacks": 0,
                                            "decksUsed": 12,
                                            "decksUsedToday": 0,
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 5,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 6622,
                    "repairPoints": 0,
                    "periodPoints": 2400,
                    "clanScore": 140,
                    "participants": [
                        {
                            "tag": "#ABC123",
                            "name": "King Levy",
                            "fame": 400,
                            "repairPoints": 0,
                            "boatAttacks": 0,
                            "decksUsed": 2,
                            "decksUsedToday": 1,
                        }
                    ],
                },
                "clans": [
                    {"tag": "#AAA111", "fame": 0, "repairPoints": 0, "periodPoints": 0},
                    {"tag": "#J2RGCRVG", "fame": 6622, "repairPoints": 0, "periodPoints": 2400},
                ],
            },
            conn=conn,
        )

        war = db.get_current_war_status(conn=conn)

        assert war["season_id"] == 130
        assert war["section_index"] == 0
        assert war["week"] == 1
        assert war["period_type"] == "warDay"
        assert war["period_index"] == 5
        assert war["phase"] == "battle"
        assert war["battle_phase_active"] is True
        assert war["practice_phase_active"] is False
        assert war["final_practice_day_active"] is False
        assert war["battle_day_number"] == 3
        assert war["battle_day_total"] == 4
        assert war["phase_display"] == "Battle Day 3"
        assert war["season_week_label"] == "Season 130 Week 1"
        assert war["final_battle_day_active"] is False
        assert war["race_rank"] == 1
        assert db.get_current_season_id(conn=conn) == 130
    finally:
        conn.close()


def test_current_war_status_marks_final_practice_day_from_api_period_index():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 1,
                "periodIndex": 2,
                "periodType": "trainingDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        war = db.get_current_war_status(conn=conn)

        assert war["week"] == 2
        assert war["phase"] == "practice"
        assert war["battle_phase_active"] is False
        assert war["practice_phase_active"] is True
        assert war["final_practice_day_active"] is True
        assert war["practice_day_number"] == 3
        assert war["practice_day_total"] == 3
        assert war["phase_display"] == "Practice Day 3"
        assert war["final_battle_day_active"] is False
    finally:
        conn.close()


def test_resolution_and_roster_summary_queries_use_v2_identity_data():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )
        db.link_discord_user_to_member(
            "123",
            "#ABC123",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )

        exact = db.resolve_member("King Levy", conn=conn)
        assert exact[0]["player_tag"] == "#ABC123"
        assert exact[0]["match_source"] == "current_name_exact"

        handle = db.resolve_member("@jamie", conn=conn)
        assert handle[0]["player_tag"] == "#ABC123"
        assert handle[0]["match_source"] == "discord_username_exact"

        summary = db.get_clan_roster_summary(conn=conn)
        assert summary["active_members"] == 2
        assert summary["open_slots"] == 48
        assert summary["avg_exp_level"] == 65.0
    finally:
        conn.close()


def test_tenure_recent_join_and_losing_streak_queries():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-01-15", conn=conn)
        recent_joined = (datetime.now(timezone.utc).date() - timedelta(days=7)).strftime("%Y-%m-%d")
        db.set_member_join_date("#DEF456", "Vijay", recent_joined, conn=conn)

        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#DEF456'"
        ).fetchone()["member_id"]
        conn.execute(
            "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
            "VALUES (?, ?, 'competitive_10', 10, 2, 8, 0, 5, 'L', 0.2, -1.5, -22.0, 'cold', '2-8 over the last 10 battles (cold).')",
            (member_id, "2026-03-07T12:00:00"),
        )
        conn.commit()

        tenure = db.list_longest_tenure_members(conn=conn)
        assert tenure[0]["tag"] == "#ABC123"

        recent = db.list_recent_joins(days=30, conn=conn)
        assert recent[0]["tag"] == "#DEF456"

        slumping = db.get_members_on_losing_streak(min_streak=3, conn=conn)
        assert len(slumping) == 1
        assert slumping[0]["tag"] == "#DEF456"
        assert slumping[0]["current_streak"] == 5
    finally:
        conn.close()


def test_war_rollup_queries_cover_nonparticipants_and_member_vs_average():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
                {"tag": "#GHI789", "name": "Finn", "role": "member", "expLevel": 62, "trophies": 8700, "clanRank": 3},
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 2400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        missing = db.get_members_without_war_participation(season_id=129, conn=conn)
        assert [m["tag"] for m in missing["members"]] == ["#GHI789"]

        summary = db.get_war_season_summary(season_id=129, conn=conn)
        assert summary["races"] == 1
        assert summary["top_contributors"][0]["tag"] == "#ABC123"
        assert summary["nonparticipants"][0]["tag"] == "#GHI789"

        comparison = db.compare_member_war_to_clan_average("#ABC123", season_id=129, conn=conn)
        assert comparison["member"]["total_fame"] == 3600
        assert comparison["clan_average"]["avg_total_fame"] == 3000.0
    finally:
        conn.close()


def test_historical_war_log_members_do_not_become_active_roster_members():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#FORMER1", "name": "Former Member", "fame": 1200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        summary = db.get_clan_roster_summary(conn=conn)
        assert summary["active_members"] == 1

        former = conn.execute(
            "SELECT status FROM members WHERE player_tag = '#FORMER1'"
        ).fetchone()
        assert former["status"] == "observed"
    finally:
        conn.close()


def test_current_war_participants_do_not_promote_non_roster_members_to_active():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 9000,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [
                        {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 1},
                        {"tag": "#FORMER2", "name": "Former Current War", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                    ],
                },
            },
            conn=conn,
        )

        summary = db.get_clan_roster_summary(conn=conn)
        assert summary["active_members"] == 1

        former = conn.execute(
            "SELECT status FROM members WHERE player_tag = '#FORMER2'"
        ).fetchone()
        assert former["status"] == "observed"
    finally:
        conn.close()


def test_war_analytics_ignore_historical_only_participants():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3000, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#FORMER1", "name": "Former Member", "fame": 4000, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    },
                    {
                        "seasonId": 129,
                        "sectionIndex": 2,
                        "createdDate": "20260302T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260302T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 2000, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    },
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        standings = db.get_war_champ_standings(season_id=129, conn=conn)
        assert [row["tag"] for row in standings] == ["#ABC123", "#DEF456"]

        trending = db.get_trending_war_contributors(season_id=129, conn=conn)
        assert all(row["tag"] != "#FORMER1" for row in trending["members"])

        comparison = db.compare_member_war_to_clan_average("#ABC123", season_id=129, conn=conn)
        assert comparison["clan_average"]["participants_with_data"] == 2
    finally:
        conn.close()


def test_risk_and_trending_war_queries_use_v2_rollups():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {
                    "tag": "#ABC123",
                    "name": "King Levy",
                    "role": "leader",
                    "expLevel": 66,
                    "trophies": 11429,
                    "clanRank": 1,
                    "donations": 150,
                    "lastSeen": "20260307T120000.000Z",
                },
                {
                    "tag": "#DEF456",
                    "name": "Vijay",
                    "role": "member",
                    "expLevel": 64,
                    "trophies": 9020,
                    "clanRank": 2,
                    "donations": 10,
                    "lastSeen": "20260226T120000.000Z",
                },
            ],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-01-15", conn=conn)
        db.set_member_join_date("#DEF456", "Vijay", "2025-10-01", conn=conn)

        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 2000, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    },
                    {
                        "seasonId": 129,
                        "sectionIndex": 2,
                        "createdDate": "20260302T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260302T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    },
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        risk = db.get_members_at_risk(
            inactivity_days=7,
            min_donations_week=20,
            require_war_participation=True,
            min_war_races=2,
            season_id=129,
            conn=conn,
        )
        assert risk["members"][0]["tag"] == "#DEF456"
        assert len(risk["members"][0]["reasons"]) >= 2

        trending = db.get_trending_war_contributors(
            season_id=129,
            recent_races=1,
            limit=2,
            conn=conn,
        )
        assert trending["members"][0]["tag"] == "#ABC123"
        assert trending["members"][0]["trend_delta"] > 0
    finally:
        conn.close()


def test_promotion_candidates_use_v2_review_logic():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {
                    "tag": "#ABC123",
                    "name": "King Levy",
                    "role": "leader",
                    "expLevel": 66,
                    "trophies": 11429,
                    "clanRank": 1,
                    "donations": 150,
                    "lastSeen": "20260307T120000.000Z",
                },
                {
                    "tag": "#DEF456",
                    "name": "Vijay",
                    "role": "member",
                    "expLevel": 64,
                    "trophies": 9020,
                    "bestTrophies": 9300,
                    "clanRank": 2,
                    "donations": 80,
                    "lastSeen": "20260306T120000.000Z",
                },
                {
                    "tag": "#GHI789",
                    "name": "Finn",
                    "role": "member",
                    "expLevel": 62,
                    "trophies": 8700,
                    "bestTrophies": 8900,
                    "clanRank": 3,
                    "donations": 15,
                    "lastSeen": "20260307T120000.000Z",
                },
            ],
            conn=conn,
        )
        db.set_member_join_date("#DEF456", "Vijay", "2025-10-01", conn=conn)
        db.set_member_join_date("#GHI789", "Finn", "2026-03-01", conn=conn)
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 100,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": "20260301T180000.000Z",
                                    "participants": [
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 2200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        review = db.get_promotion_candidates(conn=conn)
        assert review["recommended"][0]["tag"] == "#DEF456"
        assert review["borderline"] == []
        assert review["composition"]["elder_capacity_remaining"] >= 0
    finally:
        conn.close()


def test_player_intel_refresh_targets_prioritize_stale_active_members():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "clanRank": 2},
            ],
            conn=conn,
        )
        db.snapshot_player_profile({"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": []}, conn=conn)
        targets = db.get_player_intel_refresh_targets(limit=5, stale_after_hours=6, conn=conn)
        assert targets[0]["tag"] == "#ABC123"
        assert targets[0]["needs_battle_refresh"] is True
        assert targets[1]["tag"] == "#DEF456"
        assert targets[1]["needs_profile_refresh"] is True
    finally:
        conn.close()
