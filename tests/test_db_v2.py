"""Focused tests for the baseline database schema."""

import json
import os
from datetime import datetime, timedelta, timezone
import sqlite3
from unittest.mock import patch

import db
from memory_store import create_memory, list_memories
from runtime import leader_action_policy


def test_default_connection_uses_isolated_test_database():
    conn = db.get_connection()
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        main_path = row["file"]

        assert main_path
        assert main_path != os.path.join(db.PROJECT_ROOT, "elixir.db")
        assert os.path.basename(main_path) == "elixir-test.db"
    finally:
        conn.close()


def test_decision_case_tracks_inactivity_review_deferral_lifecycle():
    conn = db.get_connection(":memory:")
    try:
        signal = {
            "type": "inactive_members",
            "signal_key": "inactive:2026-06-19",
            "event_key": "game_event:inactive",
            "members": [
                {
                    "name": "xian",
                    "tag": "#UGQPVQ9U9",
                    "days_inactive": 10,
                    "battle_days_ago": 10,
                    "login_days_ago": 7,
                    "threshold_days": 7.6,
                    "role": "member",
                }
            ],
        }

        cases = db.upsert_decision_cases_from_signals(
            [signal],
            source_system="clan_awareness",
            conn=conn,
        )

        assert len(cases) == 1
        case = cases[0]
        assert case["case_key"] == "inactivity_review:member:#UGQPVQ9U9"
        assert case["case_type"] == "inactivity_review"
        assert case["status"] == db.CASE_OPEN
        assert case["target_player_tag"] == "#UGQPVQ9U9"
        assert case["source_event_key"] == "game_event:inactive"

        action = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="roster_health",
            prompt_text="Review xian for removal from the clan.",
            rationale="10 days inactive vs 7.6 day threshold",
            target_player_tag="#UGQPVQ9U9",
            target_player_name="xian",
            source_signal_key="inactive:2026-06-19",
            source_signal_type="inactive_members",
            case_id=case["case_id"],
            conn=conn,
        )
        assert action["case_id"] == case["case_id"]

        deferred = db.decide_leader_action(
            action["action_id"],
            status=db.ACTION_DEFERRED,
            discord_user_id="leader-1",
            emoji="⏳",
            defer_days=3,
            decided_at="2026-06-19T12:00:00",
            conn=conn,
        )
        assert deferred["deferred_until"] == "2026-06-22T12:00:00"

        case = db.get_decision_case_by_id(case["case_id"], conn=conn)
        assert case["status"] == db.CASE_DEFERRED
        assert case["due_at"] == "2026-06-22T12:00:00"
        assert db.list_due_decision_cases(now="2026-06-22T11:59:00", conn=conn) == []
        due = db.list_due_decision_cases(now="2026-06-22T12:00:00", conn=conn)
        assert [item["case_id"] for item in due] == [case["case_id"]]
    finally:
        conn.close()


def test_backfill_leader_actions_links_historical_deferrals_to_due_cases():
    conn = db.get_connection(":memory:")
    try:
        for name, tag, message_id, decided_at in [
            ("xian", "#UGQPVQ9U9", 1001, "2026-06-17T14:57:06"),
            ("pigsareus", "#20CGPVUL92", 1002, "2026-06-17T14:55:55"),
        ]:
            action = db.create_leader_action_recommendation(
                action_type="kick_recommendation",
                objective="roster_health",
                prompt_text=f"Review {name} for removal from the clan.",
                rationale="10 days inactive; over threshold.",
                target_player_tag=tag,
                target_player_name=name,
                source_signal_key=f"inactive:{tag}",
                source_signal_type="inactive_members",
                source_message_id=message_id,
                conn=conn,
            )
            db.decide_leader_action(
                action["action_id"],
                status=db.ACTION_DEFERRED,
                discord_user_id="leader-1",
                emoji="⏳",
                defer_days=3,
                decision_note="Deferred for 3 days.",
                decided_at=decided_at,
                conn=conn,
            )

        summary = db.backfill_decision_cases_from_leader_actions(
            now="2026-06-20T15:00:00",
            conn=conn,
        )

        assert summary["scanned"] == 2
        assert summary["created"] == 2
        assert summary["linked"] == 2
        assert summary["deferred"] == 2

        due = db.list_due_decision_cases(now="2026-06-20T15:00:00", conn=conn)
        due_tags = {item["target_player_tag"] for item in due}
        assert due_tags == {"#UGQPVQ9U9", "#20CGPVUL92"}
        xian = db.get_decision_case("inactivity_review:member:#UGQPVQ9U9", conn=conn)
        assert xian["status"] == db.CASE_DEFERRED
        assert xian["due_at"] == "2026-06-20T14:57:06"
        assert xian["state"]["leader_action"]["outcome"] == "deferred"

        action = db.get_leader_action_by_message(1001, conn=conn)
        assert action["case_id"] == xian["case_id"]

        again = db.backfill_decision_cases_from_leader_actions(
            now="2026-06-20T15:00:00",
            conn=conn,
        )
        assert again["created"] == 0
        assert again["linked"] == 0
        assert conn.execute("SELECT COUNT(*) FROM decision_cases").fetchone()[0] == 2
    finally:
        conn.close()


def test_backfill_leader_actions_maps_terminal_and_expired_case_outcomes():
    conn = db.get_connection(":memory:")
    try:
        promotion = db.create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="reward_and_retention",
            prompt_text="Promote Atlas to Elder.",
            target_player_tag="#PROMO1",
            target_player_name="Atlas",
            source_message_id=2001,
            conn=conn,
        )
        db.decide_leader_action(
            promotion["action_id"],
            status=db.ACTION_REJECTED,
            discord_user_id="leader-1",
            emoji="❌",
            decision_note="Wait one more week.",
            decided_at="2026-06-18T12:00:00",
            conn=conn,
        )
        kick = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="roster_health",
            prompt_text="Remove OfflineKing from the clan.",
            target_player_tag="#KICK1",
            target_player_name="OfflineKing",
            source_message_id=2002,
            conn=conn,
        )
        db.decide_leader_action(
            kick["action_id"],
            status=db.ACTION_DONE,
            discord_user_id="leader-1",
            emoji="✅",
            decided_at="2026-06-18T13:00:00",
            conn=conn,
        )
        db.create_leader_action_recommendation(
            action_type="demotion_recommendation",
            objective="role_review",
            prompt_text="Review Nova for demotion.",
            target_player_tag="#EXPIRE1",
            target_player_name="Nova",
            source_message_id=2003,
            expires_at="2026-06-17T12:00:00",
            conn=conn,
        )

        summary = db.backfill_decision_cases_from_leader_actions(
            now="2026-06-19T12:00:00",
            conn=conn,
        )

        assert summary["resolved"] == 1
        assert summary["dismissed"] == 2
        assert summary["expired"] == 1

        promotion_case = db.get_decision_case("promotion_review:member:#PROMO1", conn=conn)
        assert promotion_case["status"] == db.CASE_DISMISSED
        assert promotion_case["resolution"] == "Wait one more week."
        assert promotion_case["resolved_at"] == "2026-06-18T12:00:00"
        assert promotion_case["state"]["leader_action"]["outcome"] == "rejected"

        kick_case = db.get_decision_case("inactivity_review:member:#KICK1", conn=conn)
        assert kick_case["status"] == db.CASE_RESOLVED
        assert kick_case["state"]["leader_action"]["outcome"] == "accepted"

        expired_case = db.get_decision_case("demotion_review:member:#EXPIRE1", conn=conn)
        assert expired_case["status"] == db.CASE_DISMISSED
        assert expired_case["resolution"] == "Recommendation expired before a leader decision was recorded."
        assert expired_case["state"]["leader_action"]["outcome"] == "expired"
    finally:
        conn.close()


def test_get_war_season_snapshot_none_without_active_war():
    conn = db.get_connection(":memory:")
    try:
        # No war state ingested → the fresh snapshot is None (table-free path).
        assert db.get_war_season_snapshot(conn=conn) is None
    finally:
        conn.close()


def test_awareness_skip_intent_records_due_case_silence():
    conn = db.get_connection(":memory:")
    try:
        case = db.upsert_decision_case(
            case_type="inactivity_review",
            title="Review Alice",
            target_player_tag="#ABC",
            target_player_name="Alice",
            status="open",
            due_at="2026-06-19T12:00:00",
            conn=conn,
        )
        intent = db.create_awareness_skip_intent(
            [],
            workflow="clan_awareness",
            skipped_reason="No leader post needed yet.",
            situation={"decision_cases": {"due": [case], "open": []}},
            conn=conn,
        )
        assert intent["status"] == "skipped"
        assert intent["intent_type"] == "skip"
        assert intent["case_id"] == case["case_id"]
        assert intent["skipped_reason"] == "No leader post needed yet."
    finally:
        conn.close()


def test_awareness_coverage_gap_intent_records_failed_required_signal():
    conn = db.get_connection(":memory:")
    try:
        signal = {
            "type": "member_join",
            "signal_key": "join:#ABC",
            "event_key": "game_event:join",
            "tag": "#ABC",
            "name": "Alice",
        }
        intent = db.create_awareness_coverage_gap_intent(
            [signal],
            workflow="clan_awareness",
            reason="hard-post-floor signal was not covered",
            conn=conn,
        )

        assert intent["status"] == "failed"
        assert intent["intent_type"] == "coverage_gap"
        assert intent["source_signal_key"] == "join:#ABC"
        assert intent["source_signal_type"] == "member_join"
        assert intent["covers_signal_keys"] == ["join:#ABC"]
        assert intent["event_keys"] == ["game_event:join"]
        assert intent["error_detail"] == "hard-post-floor signal was not covered"
        assert intent["payload"]["signals_uncovered"] == 1
    finally:
        conn.close()


def test_war_nudge_cleanup_migration_removes_retired_action_data():
    conn = db.get_connection(":memory:")
    try:
        nudge = db.create_leader_action_recommendation(
            action_type="war_nudge_recommendation",
            objective="war_participation",
            prompt_text="Ask Vijay to use war decks.",
            target_player_tag="#VJ1",
            target_player_name="Vijay",
            source_signal_key="war_nudge:#VJ1:s134-w3-d2",
            source_signal_type="war_nudge_recommendation",
            source_message_id=101,
            conn=conn,
        )
        kept = db.create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="role_review",
            prompt_text="Promote King Levy.",
            target_player_tag="#KING",
            target_player_name="King Levy",
            source_signal_key="promotion:#KING",
            source_signal_type="promotion_recommendation",
            source_message_id=102,
            conn=conn,
        )
        db.save_message(
            "channel",
            "assistant",
            "Retired nudge card",
            channel_id=900,
            channel_name="arena-relay",
            workflow="arena-relay",
            event_type="war_nudge_recommendation",
            discord_message_id=101,
            raw_json={"leader_action": nudge},
            conn=conn,
        )
        db.save_message(
            "channel",
            "user",
            "Already handled.",
            channel_id=900,
            channel_name="arena-relay",
            workflow="arena-relay",
            event_type="leader_action_note",
            discord_message_id=201,
            raw_json={"leader_action_id": nudge["action_id"]},
            conn=conn,
        )
        db.save_message(
            "channel",
            "assistant",
            "Kept promotion card",
            channel_id=900,
            channel_name="arena-relay",
            workflow="arena-relay",
            event_type="promotion_recommendation",
            discord_message_id=102,
            raw_json={"leader_action": kept},
            conn=conn,
        )
        db.upsert_signal_outcome(
            "war_nudge:#VJ1:s134-w3-d2",
            "war_nudge_recommendation",
            "arena-relay",
            900,
            "war_nudge_recommendation",
            delivery_status="delivered",
            conn=conn,
        )
        db.upsert_signal_outcome(
            "promotion:#KING",
            "promotion_recommendation",
            "arena-relay",
            900,
            "promotion_recommendation",
            delivery_status="delivered",
            conn=conn,
        )
        db.upsert_leader_action_feedback_profile(
            action_type="war_nudge_recommendation",
            profile={
                "summary": "War nudges were declined as too fast for Elixir.",
                "avoid": ["Do not recommend war nudges."],
                "sample_count": 1,
            },
            conn=conn,
        )

        next(fn for fn in db._MIGRATIONS if fn.__name__ == "_migration_42")(conn)

        assert conn.execute(
            "SELECT COUNT(*) FROM leader_action_recommendations WHERE action_type = 'war_nudge_recommendation'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM leader_action_recommendations WHERE action_type = 'promotion_recommendation'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE event_type = 'war_nudge_recommendation'"
        ).fetchone()[0] == 0
        assert conn.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE event_type = 'leader_action_note'
              AND json_valid(raw_json)
              AND CAST(json_extract(raw_json, '$.leader_action_id') AS INTEGER) = ?
            """,
            (nudge["action_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE event_type = 'promotion_recommendation'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE source_signal_type = 'war_nudge_recommendation'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE source_signal_type = 'promotion_recommendation'"
        ).fetchone()[0] == 1
        assert db.list_leader_action_feedback_profiles(
            action_type="war_nudge_recommendation", conn=conn
        ) == []
    finally:
        conn.close()


def test_leader_action_recommendation_records_reaction_decision():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="reward_and_retention",
            prompt_text="Promote King Levy to Elder.",
            rationale="220 donations, 4 war races",
            target_player_tag="#ABC123",
            target_player_name="King Levy",
            source_message_id=987,
            baseline={"member": {"role": "member", "status": "active"}},
            conn=conn,
        )

        assert action["status"] == db.ACTION_PROPOSED
        assert db.has_recent_leader_action(
            action_type="promotion_recommendation",
            target_player_tag="#ABC123",
            objective="reward_and_retention",
            conn=conn,
        )
        assert not db.has_recent_leader_action(
            action_type="promotion_recommendation",
            target_player_tag="#NOPE",
            objective="reward_and_retention",
            conn=conn,
        )
        decided = db.decide_leader_action_by_message(
            987,
            status=db.ACTION_REJECTED,
            discord_user_id=123,
            emoji="❌",
            conn=conn,
        )

        assert decided["status"] == db.ACTION_REJECTED
        assert decided["decided_by_discord_user_id"] == "123"
        assert decided["baseline"]["member"]["role"] == "member"

        cleared = db.clear_leader_action_decision_by_message(
            987,
            discord_user_id=123,
            emoji="❌",
            conn=conn,
        )
        assert cleared["status"] == db.ACTION_PROPOSED
        assert cleared["decided_at"] is None

        db.update_leader_action_copy_message(action["action_id"], copy_message_id=321, conn=conn)
        copy_lookup = db.get_leader_action_by_message(321, conn=conn)
        assert copy_lookup["action_id"] == action["action_id"]

        noted = db.record_leader_action_note_by_message(
            321,
            note="boat defenses full already",
            discord_user_id=123,
            note_message_id=654,
            conn=conn,
        )
        assert noted["decision_note"] == "boat defenses full already"
        assert noted["decision_note_message_id"] == "654"
        assert noted["expires_at"] is not None
        assert noted["outcome"]["leader_note"]["category"] == "state_already_satisfied"
    finally:
        conn.close()


def test_leader_action_defer_is_terminal_with_suppression_window():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="roster_health",
            prompt_text="Review Vijay for removal.",
            rationale="inactive",
            source_message_id=777,
            conn=conn,
        )

        deferred = db.decide_leader_action_by_message(
            777,
            status=db.ACTION_DEFERRED,
            discord_user_id=123,
            emoji="⏳",
            defer_days=3,
            decision_note="Deferred for 3 days.",
            decided_at="2026-06-11T12:00:00",
            conn=conn,
        )

        assert deferred["action_id"] == action["action_id"]
        assert deferred["status"] == db.ACTION_DEFERRED
        assert deferred["defer_days"] == 3
        assert deferred["deferred_until"] == "2026-06-14T12:00:00"
        assert deferred["expires_at"] == deferred["deferred_until"]
        assert deferred["decision_note"] == "Deferred for 3 days."
    finally:
        conn.close()


def test_leader_action_copy_sequence_lookup_and_edit_diff():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="discord_invite_relay",
            objective="discord_recruiting",
            prompt_text="Invite the clan to Discord.",
            copy_original_text="Message one\nMessage two",
            copy_current_text="Message one\nMessage two",
            source_message_id=100,
            conn=conn,
        )
        db.update_leader_action_copy_messages(action["action_id"], copy_message_ids=[101, 102], conn=conn)

        lookup = db.get_leader_action_by_message(102, conn=conn)
        assert lookup["action_id"] == action["action_id"]
        assert lookup["copy_message_ids"] == ["101", "102"]

        edited = db.update_leader_action_copy_text(
            action["action_id"],
            copy_text="Message one edited\nMessage two",
            discord_user_id=456,
            conn=conn,
        )
        assert edited["copy_current_text"] == "Message one edited\nMessage two"
        assert edited["copy_edited_by_discord_user_id"] == "456"
        assert edited["copy_edit_diff"]["changed"] is True
    finally:
        conn.close()


def test_leader_action_feedback_profile_persists_as_elixir_synthesis_memory():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="welcome_relay",
            objective="new_member_welcome",
            prompt_text="Welcome Gem! 319 wins is strong.",
            target_player_tag="#GEM",
            target_player_name="Gem",
            source_message_id=1234,
            conn=conn,
        )
        db.decide_leader_action_by_message(
            1234,
            status=db.ACTION_DONE,
            discord_user_id=42,
            emoji="✅",
            conn=conn,
        )
        db.record_leader_action_note_by_message(
            1234,
            note="Changed to “Welcome to POAP KINGS Gem! 319 wins!”",
            discord_user_id=42,
            note_message_id=5678,
            conn=conn,
        )

        context = db.build_leader_action_feedback_synthesis_context(
            action_type="welcome_relay",
            conn=conn,
        )
        assert context["counts"]["total"] == 1
        assert context["recent_actions"][0]["action_id"] == action["action_id"]
        assert "Changed to" in context["recent_actions"][0]["decision_note"]

        memory = db.upsert_leader_action_feedback_profile(
            action_type="welcome_relay",
            profile={
                "action_type": "welcome_relay",
                "sample_count": 1,
                "summary": "Leaders prefer short native clan-chat welcomes.",
                "guidance": ["Use the pattern: Welcome to POAP KINGS NAME! FACT!"],
                "avoid": ["Phrases like 'is strong' when the leader removes them."],
                "evidence": [{"action_id": action["action_id"], "lesson": "Leader rewrote the copy into a short in-game welcome."}],
            },
            conn=conn,
        )

        assert memory["source_type"] == "elixir_synthesis"
        assert memory["event_type"] == db.LEADER_ACTION_FEEDBACK_EVENT_TYPE
        assert memory["event_id"] == "leader_action_feedback:welcome_relay"
        assert "Welcome to POAP KINGS NAME! FACT!" in memory["body"]

        profiles = db.list_leader_action_feedback_profiles(action_type="welcome_relay", conn=conn)
        assert len(profiles) == 1
        assert profiles[0]["memory_id"] == memory["memory_id"]

        memories = list_memories(
            viewer_scope="leadership",
            filters={"event_type": db.LEADER_ACTION_FEEDBACK_EVENT_TYPE},
            conn=conn,
        )
        assert memories[0]["source_type"] == "elixir_synthesis"
    finally:
        conn.close()


def test_leader_action_done_decision_uses_delayed_outcome_refresh():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="reward_and_retention",
            prompt_text="Promote King Levy to Elder.",
            target_player_tag="#ABC123",
            target_player_name="King Levy",
            source_message_id=987,
            baseline={"member": {"role": "member", "status": "active"}},
            conn=conn,
        )

        decided = db.decide_leader_action_by_message(
            987,
            status=db.ACTION_DONE,
            discord_user_id=123,
            emoji="✅",
            decided_at="2026-01-01T00:00:00",
            conn=conn,
        )

        assert decided["status"] == db.ACTION_DONE
        assert decided["outcome"]["pending_evaluation"] is True
        assert decided["outcome"]["due_at"] == "2026-01-02T00:00:00"

        refreshed = db.refresh_due_leader_action_outcomes(conn=conn)
        assert refreshed
        updated = db.get_leader_action_by_key(action["action_key"], conn=conn)
        assert not updated["outcome"].get("pending_evaluation")
        assert updated["outcome"]["changed"]["role"] is True
    finally:
        conn.close()


def test_leader_action_note_revisit_controls_recent_suppression_window():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="roster_health",
            prompt_text="Review King Levy for removal.",
            target_player_tag="#ABC123",
            target_player_name="King Levy",
            source_message_id=987,
            conn=conn,
        )

        noted = db.record_leader_action_note_by_message(
            987,
            note="Revisit in a week.",
            discord_user_id=123,
            conn=conn,
        )

        assert noted["expires_at"] is not None
        assert noted["outcome"]["leader_note"]["category"] == "revisit"
        assert db.has_recent_leader_action(
            action_type="kick_recommendation",
            target_player_tag="#ABC123",
            objective="roster_health",
            conn=conn,
        )

        conn.execute(
            "UPDATE leader_action_recommendations SET expires_at = '2026-01-01T00:00:00' WHERE action_id = ?",
            (action["action_id"],),
        )
        conn.commit()
        assert not db.has_recent_leader_action(
            action_type="kick_recommendation",
            target_player_tag="#ABC123",
            objective="roster_health",
            conn=conn,
        )
    finally:
        conn.close()


def test_recent_leader_action_lookup_finds_completed_kick_target():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="roster_health",
            prompt_text="Remove QuickChurn from the clan.",
            rationale="Inactive and not using war decks.",
            target_player_tag="#ABC123",
            target_player_name="QuickChurn",
            source_message_id=987,
            conn=conn,
        )
        db.decide_leader_action_by_message(
            987,
            status=db.ACTION_DONE,
            discord_user_id=123,
            emoji="✅",
            conn=conn,
        )

        found = db.get_recent_leader_action_for_target(
            action_type="kick_recommendation",
            target_player_tag="#ABC123",
            status=db.ACTION_DONE,
            conn=conn,
        )

        assert found["action_id"] == action["action_id"]
        assert found["rationale"] == "Inactive and not using war decks."
    finally:
        conn.close()


def test_leader_action_policy_runs_24h_and_caps_on_open_backlog():
    """No quiet hours, no daily quota — posting pauses only while leadership
    has a full backlog of undecided cards, and resumes as cards get decided."""
    conn = db.get_connection(":memory:")
    try:
        # Middle of the night Chicago: still allowed. Clash runs 24 hours.
        chicago_now = datetime.now(db.CHICAGO_TZ)
        late_night = chicago_now.replace(hour=0, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        allowed, reason = leader_action_policy.can_post_leader_action(conn=conn, now=late_night)
        assert allowed, reason

        # Fill the board with undecided cards → backlog gate closes.
        for index in range(leader_action_policy.LEADER_ACTION_OPEN_CARD_CAP):
            db.create_leader_action_recommendation(
                action_type="kick_recommendation",
                objective="roster_health",
                prompt_text=f"Review member {index}.",
                source_message_id=4000 + index,
                conn=conn,
            )
        allowed, reason = leader_action_policy.can_post_leader_action(conn=conn, now=late_night)
        assert not allowed
        assert reason.startswith("open_card_backlog:")

        # Critical war moments bypass the backlog.
        allowed, reason = leader_action_policy.can_post_leader_action(conn=conn, now=late_night, critical=True)
        assert allowed
        assert reason is None

        # The leader deciding a card re-opens the budget — regardless of hour.
        db.decide_leader_action_by_message(
            4000, status="done", discord_user_id=123, emoji="✅", conn=conn,
        )
        allowed, reason = leader_action_policy.can_post_leader_action(conn=conn, now=late_night)
        assert allowed, reason

        # Stale open cards age out of the backlog window instead of
        # deadlocking the board forever.
        conn.execute("UPDATE leader_action_recommendations SET proposed_at = '2026-01-01T00:00:00'")
        allowed, reason = leader_action_policy.can_post_leader_action(conn=conn, now=late_night)
        assert allowed, reason
    finally:
        conn.close()


def test_leader_action_decision_stats_counts_and_decline_rate():
    conn = db.get_connection(":memory:")
    try:
        for index, status in enumerate(["done", "rejected", "rejected", "deferred"]):
            db.create_leader_action_recommendation(
                action_type="promotion_recommendation",
                objective="role_review",
                prompt_text=f"Promote member {index}.",
                target_player_tag=f"#T{index}",
                source_message_id=1000 + index,
                conn=conn,
            )
            db.decide_leader_action_by_message(
                1000 + index,
                status=status,
                discord_user_id=123,
                emoji="✅" if status == "done" else "❌",
                defer_days=3 if status == "deferred" else None,
                conn=conn,
            )
        stats = db.leader_action_decision_stats(action_type="promotion_recommendation", conn=conn)
        assert stats["done"] == 1
        assert stats["rejected"] == 2
        assert stats["deferred"] == 1
        assert stats["decided"] == 3
        # Defers excluded from the denominator: 2 rejected of 3 decided.
        assert abs(stats["decline_rate"] - (2 / 3)) < 1e-9

        # Unknown type returns zeroed stats, not a KeyError.
        empty = db.leader_action_decision_stats(action_type="kick_recommendation", conn=conn)
        assert empty["decided"] == 0
        assert empty["decline_rate"] is None
    finally:
        conn.close()


def test_leader_action_board_snapshot_splits_open_and_decided():
    conn = db.get_connection(":memory:")
    try:
        for index, status in enumerate([None, "done", "rejected"]):
            db.create_leader_action_recommendation(
                action_type="promotion_recommendation",
                objective="role_review",
                prompt_text=f"Promote member {index}.",
                target_player_tag=f"#T{index}",
                source_message_id=3000 + index,
                conn=conn,
            )
            if status:
                db.decide_leader_action_by_message(
                    3000 + index,
                    status=status,
                    discord_user_id=123,
                    emoji="✅" if status == "done" else "❌",
                    conn=conn,
                )
        board = db.leader_action_board_snapshot(conn=conn)
        assert [item["target_player_tag"] for item in board["open"]] == ["#T0"]
        assert {item["status"] for item in board["recent_decisions"]} == {"done", "rejected"}
        # Compact shape only — no baselines/outcomes/copy bodies.
        assert "baseline" not in board["open"][0]
        assert "outcome" not in board["recent_decisions"][0]
    finally:
        conn.close()


def test_leader_action_policy_earned_frequency_throttles_declined_types():
    """A type the leader keeps declining self-throttles to one card per
    cooldown; critical bypasses; a well-accepted type is unaffected."""
    conn = db.get_connection(":memory:")
    try:
        chicago_now = datetime.now(db.CHICAGO_TZ)
        active = chicago_now.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

        # Five declined promotions (>= MIN_DECIDED, decline rate 1.0). Backdate
        # proposed_at out of today's daily-cap window but inside the 72h
        # earned-frequency cooldown, so this test isolates the new gate.
        for index in range(5):
            db.create_leader_action_recommendation(
                action_type="promotion_recommendation",
                objective="role_review",
                prompt_text=f"Promote member {index}.",
                target_player_tag=f"#T{index}",
                source_message_id=2000 + index,
                conn=conn,
            )
            db.decide_leader_action_by_message(
                2000 + index, status="rejected", discord_user_id=123, emoji="❌", conn=conn,
            )
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "UPDATE leader_action_recommendations SET proposed_at = ?", (two_days_ago,),
        )

        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="promotion_recommendation", conn=conn, now=active,
        )
        assert not allowed
        assert reason.startswith("earned_frequency:promotion_recommendation")

        # Critical moments bypass the throttle.
        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="promotion_recommendation", critical=True, conn=conn, now=active,
        )
        assert allowed

        # A different action type with no decline history is unaffected.
        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="kick_recommendation", conn=conn, now=active,
        )
        assert allowed, reason

        # Once the throttled type's last card ages past the cooldown, it is
        # allowed again (one card per cooldown, not a permanent ban).
        conn.execute("UPDATE leader_action_recommendations SET proposed_at = '2026-01-01T00:00:00', expires_at = NULL")
        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="promotion_recommendation", conn=conn, now=active,
        )
        assert allowed, reason
    finally:
        conn.close()


def test_leader_action_policy_throttles_in_game_relay_by_objective():
    conn = db.get_connection(":memory:")
    try:
        db.create_leader_action_recommendation(
            action_type="in_game_relay",
            objective="war_participation",
            prompt_text="Battle day is live. Use your decks.",
            source_message_id=3000,
            conn=conn,
        )

        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="in_game_relay",
            objective="war_participation",
            conn=conn,
        )
        assert not allowed
        assert reason == "objective_cooldown:in_game_relay:war_participation:18h"

        allowed, reason = leader_action_policy.can_post_leader_action(
            action_type="in_game_relay",
            objective="war_recognition",
            conn=conn,
        )
        assert allowed, reason

        allowed, reason = leader_action_policy.can_post_leader_action(
            critical=True,
            action_type="in_game_relay",
            objective="war_participation",
            conn=conn,
        )
        assert allowed, reason
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


def test_snapshot_members_uses_chicago_day_for_daily_metrics():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.roster._utcnow", return_value="2026-01-01T03:30:00"):
            db.snapshot_members(
                [{"tag": "#ABC123", "name": "King Levy", "role": "member", "trophies": 7000}],
                conn=conn,
            )

        row = conn.execute(
            "SELECT metric_date FROM member_daily_metrics"
        ).fetchone()
        assert row["metric_date"] == "2025-12-31"
    finally:
        conn.close()


def test_snapshot_clan_daily_metrics_tracks_churn_and_updates_same_day():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.roster._utcnow", return_value="2026-03-10T18:00:00"):
            db.snapshot_members(
                [
                    {"tag": "#AAA111", "name": "Alpha", "role": "leader", "trophies": 7000, "clanRank": 1, "donations": 40},
                    {"tag": "#BBB222", "name": "Bravo", "role": "member", "trophies": 6900, "clanRank": 2, "donations": 25},
                ],
                conn=conn,
            )

        with patch("storage.roster._utcnow", return_value="2026-03-11T18:00:00"):
            db.snapshot_members(
                [
                    {"tag": "#AAA111", "name": "Alpha", "role": "leader", "trophies": 7100, "clanRank": 1, "donations": 50},
                    {"tag": "#CCC333", "name": "Charlie", "role": "member", "trophies": 8000, "clanRank": 2, "donations": 75},
                ],
                conn=conn,
            )

        with patch("storage.metadata.chicago_today", return_value="2026-03-11"):
            db.clear_member_tenure("#BBB222", conn=conn)

        db.snapshot_clan_daily_metrics(
            {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "members": 2,
                "clanScore": 4567,
                "clanWarTrophies": 1234,
                "requiredTrophies": 9000,
                "donationsPerWeek": 100,
                "memberList": [
                    {"tag": "#AAA111", "name": "Alpha", "trophies": 7100, "donations": 50},
                    {"tag": "#CCC333", "name": "Charlie", "trophies": 8000, "donations": 75},
                ],
            },
            observed_at="2026-03-11T18:00:00",
            conn=conn,
        )
        db.snapshot_clan_daily_metrics(
            {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "members": 2,
                "clanScore": 4600,
                "clanWarTrophies": 1234,
                "requiredTrophies": 9000,
                "donationsPerWeek": 100,
                "memberList": [
                    {"tag": "#AAA111", "name": "Alpha", "trophies": 7100, "donations": 50},
                    {"tag": "#CCC333", "name": "Charlie", "trophies": 8000, "donations": 75},
                ],
            },
            observed_at="2026-03-11T19:00:00",
            conn=conn,
        )

        with patch("storage.roster.chicago_today", return_value="2026-03-11"):
            rows = db.list_clan_daily_metrics(days=10, conn=conn)
        assert len(rows) == 1
        assert rows[0]["metric_date"] == "2026-03-11"
        assert rows[0]["member_count"] == 2
        assert rows[0]["open_slots"] == 48
        assert rows[0]["clan_score"] == 4600
        assert rows[0]["weekly_donations_total"] == 125
        assert rows[0]["total_member_trophies"] == 15100
        assert rows[0]["avg_member_trophies"] == 7550.0
        assert rows[0]["top_member_trophies"] == 8000
        assert rows[0]["joins_today"] == 1
        assert rows[0]["leaves_today"] == 1
        assert rows[0]["net_member_change"] == 0
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
    finally:
        conn.close()


def test_manual_discord_identity_uses_handle_not_fake_mention():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_discord_identity("#ABC123", "@kinglevy", conn=conn)

        identity = db.get_member_identity("#ABC123", conn=conn)
        assert identity["discord_user_id"] == "manual:kinglevy"
        assert identity["discord_username"] == "kinglevy"
        assert db.format_member_reference("#ABC123", conn=conn) == "King Levy"
    finally:
        conn.close()


def test_manual_discord_identity_parses_real_discord_mention():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.upsert_discord_user(
            "1478515435606380687",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )
        db.set_member_discord_identity("#ABC123", "<@1478515435606380687>", conn=conn)

        identity = db.get_member_identity("#ABC123", conn=conn)
        assert identity["discord_user_id"] == "1478515435606380687"
        assert identity["discord_username"] == "jamie"
        assert db.format_member_reference("#ABC123", conn=conn) == "King Levy"
    finally:
        conn.close()


def test_upsert_discord_user_auto_links_unique_exact_member_name():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#20JJJ2CCRU", "name": "King Thing", "role": "leader"}],
            conn=conn,
        )

        db.upsert_discord_user(
            "704062105258557511",
            username="jthingelstad",
            global_name="Jamie Thingelstad",
            display_name="King Thing",
            conn=conn,
        )

        identity = db.get_member_identity("#20JJJ2CCRU", conn=conn)
        assert identity["in_discord"] == 1
        assert identity["discord_user_id"] == "704062105258557511"
        assert identity["discord_display_name"] == "King Thing"
    finally:
        conn.close()


def test_member_metadata_fields_flow_into_member_profile():
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

        profile = db.get_member_profile("#ABC123", conn=conn)
        assert profile["joined_date"] == "2024-01-15"
        assert profile["birth_month"] == 2
        assert profile["birth_day"] == 14
        assert profile["profile_url"] == "https://example.com"
        assert profile["note"] == "Founder"
        rows = db.list_member_metadata_rows(conn=conn)
        assert rows[0]["joined_date"] == "2024-01-15"
        assert rows[0]["poap_address"] == ""
    finally:
        conn.close()


def test_member_generated_profile_and_player_snapshot_flow_into_member_profile():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "coLeader", "trophies": 11313, "bestTrophies": 11400}],
            conn=conn,
        )
        db.set_member_generated_profile(
            "#ABC123",
            "King Levy",
            "King Levy is one of the war leaders and keeps the pressure high with sharp Goblin Barrel lines.",
            "war",
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "wins": 1234,
                "losses": 777,
                "battleCount": 2105,
                "totalDonations": 9876,
                "warDayWins": 88,
                "threeCrownWins": 222,
                "currentFavouriteCard": {"name": "Goblin Barrel"},
                "currentDeck": [],
                "cards": [],
            },
            conn=conn,
        )

        profile = db.get_member_profile("#ABC123", conn=conn)

        assert profile["bio"].startswith("King Levy is one of the war leaders")
        assert profile["profile_highlight"] == "war"
        assert profile["career_wins"] == 1234
        assert profile["career_losses"] == 777
        assert profile["career_battle_count"] == 2105
        assert profile["career_total_donations"] == 9876
        assert profile["war_day_wins"] == 88
        assert profile["three_crown_wins"] == 222
        assert profile["current_favourite_card_name"] == "Goblin Barrel"
        assert profile["generated_profile_updated_at"] is not None
    finally:
        conn.close()


def test_snapshot_player_profile_derives_clash_royale_account_age_metadata():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "coLeader"}],
            conn=conn,
        )

        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473, "target": 1825},
                    {"name": "CollectionLevel", "level": 8, "maxLevel": 8, "progress": 1639},
                    {"name": "ClanWarWins", "level": 5, "maxLevel": 10, "progress": 421},
                    {"name": "BattleWins", "level": 7, "maxLevel": 10, "progress": 6400},
                    {"name": "ClanDonations", "level": 9, "maxLevel": 10, "progress": 32145},
                    {"name": "BannerCollection", "level": 3, "maxLevel": 10, "progress": 88},
                    {"name": "EmoteCollection", "level": 4, "maxLevel": 10, "progress": 123},
                ],
            },
            conn=conn,
        )

        metadata = db.get_member_metadata("#ABC123", conn=conn)
        profile = db.get_member_profile("#ABC123", conn=conn)

        assert metadata["cr_account_age_days"] == 1473
        assert metadata["cr_account_age_years"] == 4
        assert metadata["cr_account_age_updated_at"] is not None
        assert metadata["cr_collection_level"] == 1639
        assert metadata["cr_collection_level_badge_tier"] == 8
        assert metadata["cr_collection_level_badge_max_tier"] == 8
        assert metadata["cr_collection_level_updated_at"] is not None
        assert metadata["cr_clan_war_wins"] == 421
        assert metadata["cr_battle_wins"] == 6400
        assert metadata["cr_clan_donations"] == 32145
        assert metadata["cr_banner_count"] == 88
        assert metadata["cr_emote_count"] == 123
        assert metadata["cr_profile_badges_updated_at"] is not None
        assert profile["cr_account_age_days"] == 1473
        assert profile["cr_account_age_years"] == 4
        assert profile["cr_collection_level"] == 1639
        assert profile["cr_collection_level_badge_tier"] == 8
        assert profile["cr_clan_war_wins"] == 421
        assert profile["cr_battle_wins"] == 6400
        assert profile["cr_clan_donations"] == 32145
        assert profile["cr_banner_count"] == 88
        assert profile["cr_emote_count"] == 123

        rows = db.list_member_metadata_rows(conn=conn)
        assert rows[0]["cr_account_age_days"] == 1473
        assert rows[0]["cr_account_age_years"] == 4
        assert rows[0]["cr_collection_level"] == 1639
        assert rows[0]["cr_clan_war_wins"] == 421
    finally:
        conn.close()


def test_snapshot_player_profile_emits_cr_account_anniversary_signal():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "coLeader"}],
            conn=conn,
        )

        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473, "target": 1825},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "YearsPlayed", "level": 5, "maxLevel": 11, "progress": 1825, "target": 2190},
                ],
            },
            conn=conn,
        )

        assert {
            "type": "cr_account_anniversary",
            "tag": "#ABC123",
            "name": "King Levy",
            "old_years": 4,
            "new_years": 5,
            "account_age_days": 1825,
        } in signals
        assert not any(
            signal["type"] == "badge_level_milestone" and signal.get("badge_name") == "YearsPlayed"
            for signal in signals
        )
    finally:
        conn.close()


def test_snapshot_player_battlelog_derives_recent_games_per_day_metadata():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        with patch("storage.player.chicago_today", return_value="2026-03-14"):
            db.snapshot_player_battlelog(
                "#ABC123",
                [
                    {
                        "type": "PvP",
                        "battleTime": "20260314T100000.000Z",
                        "gameMode": {"id": 72000006, "name": "Ladder"},
                        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 30, "startingTrophies": 7000, "cards": [], "supportCards": []}],
                        "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": [], "supportCards": []}],
                    },
                    {
                        "type": "PvP",
                        "battleTime": "20260314T090000.000Z",
                        "gameMode": {"id": 72000006, "name": "Ladder"},
                        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 28, "startingTrophies": 6972, "cards": [], "supportCards": []}],
                        "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 1, "cards": [], "supportCards": []}],
                    },
                    {
                        "type": "PvP",
                        "battleTime": "20260310T110000.000Z",
                        "gameMode": {"id": 72000006, "name": "Ladder"},
                        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "trophyChange": 31, "startingTrophies": 6941, "cards": [], "supportCards": []}],
                        "opponent": [{"tag": "#OPP3", "name": "Opp 3", "crowns": 0, "cards": [], "supportCards": []}],
                    },
                    {
                        "type": "pathOfLegend",
                        "battleTime": "20260305T120000.000Z",
                        "gameMode": {"id": 72000464, "name": "Ranked1v1_NewArena2"},
                        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 1, "trophyChange": 29, "startingTrophies": 6912, "cards": [], "supportCards": []}],
                        "opponent": [{"tag": "#OPP4", "name": "Opp 4", "crowns": 0, "cards": [], "supportCards": []}],
                    },
                ],
                conn=conn,
            )

        metadata = db.get_member_metadata("#ABC123", conn=conn)
        profile = db.get_member_profile("#ABC123", conn=conn)

        assert metadata["cr_games_per_day"] == 0.29
        assert metadata["cr_games_per_day_window_days"] == 14
        assert metadata["cr_games_per_day_updated_at"] is not None
        assert profile["cr_games_per_day"] == 0.29
        assert profile["cr_games_per_day_window_days"] == 14
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
                "months": 24,
                "quarters": 8,
                "years": 2,
                "is_yearly": True,
            }
        ]
    finally:
        conn.close()


def test_join_anniversary_emits_quarterly_membership_milestones():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2025-12-08", conn=conn)

        anniversaries = db.get_join_anniversaries_today("2026-03-08", conn=conn)
        assert anniversaries == [
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "joined_date": "2025-12-08",
                "months": 3,
                "quarters": 1,
                "years": 0,
                "is_yearly": False,
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


def test_list_thread_messages_skips_blank_history_before_limit():
    conn = db.get_connection(":memory:")
    try:
        db.save_message("leader:user123", "user", "First real question", workflow="leader", conn=conn)
        db.save_message("leader:user123", "assistant", "", workflow="leader", conn=conn)
        db.save_message("leader:user123", "user", "   ", workflow="leader", conn=conn)
        db.save_message("leader:user123", "assistant", "Second real answer", workflow="leader", conn=conn)

        history = db.list_thread_messages("leader:user123", limit=2, conn=conn)

        assert [turn["content"] for turn in history] == [
            "First real question",
            "Second real answer",
        ]
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

        public_memory = db.build_memory_context(
            discord_user_id="1474760692992180429",
            member_tag="#ABC123",
            conn=conn,
        )
        assert public_memory["discord_user"] is None
        assert public_memory["member"] is None

        leadership_memory = db.build_memory_context(
            discord_user_id="1474760692992180429",
            member_tag="#ABC123",
            viewer_scope="leadership",
            conn=conn,
        )
        assert leadership_memory["discord_user"]["episodes"]
        assert leadership_memory["member"]["episodes"]
        # last_user_summary is written by _post_conversation_memory after
        # distillation, not by save_message, so no facts exist yet
        assert leadership_memory["discord_user"]["facts"] == []
    finally:
        conn.close()


def test_memory_context_includes_scoped_durable_memories():
    conn = db.get_connection(":memory:")
    try:
        create_memory(
            title="Leadership context",
            body="King Levy prefers late-war recaps.",
            summary="Prefers late-war recaps",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            scope="leadership",
            member_tag="#ABC123",
            conn=conn,
        )

        public_context = db.build_memory_context(
            member_tag="#ABC123",
            viewer_scope="public",
            conn=conn,
        )
        assert "durable_memories" not in public_context

        leadership_context = db.build_memory_context(
            member_tag="#ABC123",
            viewer_scope="leadership",
            conn=conn,
        )
        assert leadership_context["durable_memories"][0]["title"] == "Leadership context"
    finally:
        conn.close()


def test_runtime_job_status_round_trips():
    conn = db.get_connection(":memory:")
    try:
        saved = db.save_runtime_job_status(
            "memory_synthesis",
            {"run_count": 2, "last_error": "agent truncation"},
            conn=conn,
        )
        statuses = db.list_runtime_job_status(conn=conn)

        assert saved["job_name"] == "memory_synthesis"
        assert statuses["memory_synthesis"]["run_count"] == 2
        assert statuses["memory_synthesis"]["last_error"] == "agent truncation"
        assert statuses["memory_synthesis"]["updated_at"]
    finally:
        conn.close()


def test_migration_repairs_truncated_v5_intent_summary(tmp_path):
    db_path = tmp_path / "repair-trace-summary.db"
    conn = db.get_connection(str(db_path))
    full_summary = {
        "detection_type": "best_trophies_peak",
        "peak": 6000,
        "recognition_policy": "player_highlight_score:v1",
        "recognition_evidence": [
            {
                "dedup_key": f"battle_trophy_push:#A:{index}",
                "detection_type": "battle_trophy_push",
                "score": 25 + index,
            }
            for index in range(20)
        ],
    }
    broken_summary = json.dumps(full_summary, ensure_ascii=False)[:500]
    try:
        db.upsert_communication_intent(
            intent_key="v5:intent:detection:best_trophies_peak:#A:6000",
            workflow="v5-reactive",
            intent_type="celebrate:best_trophies_peak",
            status="delivered",
            target_channel_key="player-highlights",
            target_channel_id=1482352147029950474,
            source_signal_key="best_trophies_peak:#A:6000",
            source_signal_type="best_trophies_peak",
            covers_signal_keys=["best_trophies_peak:#A:6000"],
            summary=broken_summary,
            payload={"summary": full_summary},
            conn=conn,
        )
        conn.execute(f"PRAGMA user_version = {len(db._MIGRATIONS) - 1}")
        conn.commit()
    finally:
        conn.close()

    conn = db.get_connection(str(db_path))
    try:
        row = conn.execute(
            "SELECT summary FROM communication_intents WHERE intent_key = ?",
            ("v5:intent:detection:best_trophies_peak:#A:6000",),
        ).fetchone()
        assert json.loads(row["summary"]) == full_summary
    finally:
        conn.close()


def test_arena_relay_screenshot_observation_round_trips():
    conn = db.get_connection(":memory:")
    try:
        saved = db.save_arena_relay_screenshot_observation(
            source_message_id=12345,
            channel_id=1513758211206025227,
            channel_name="arena-relay",
            author_discord_user_id=999,
            author_display_name="Leader",
            screenshot_type="boat defense",
            summary="Three visible open defense slots.",
            content="**Read:** three open slots visible.",
            players=["dez42", "WaltadR"],
            actionable_facts=["At least three boat defense slots are visible."],
            uncertainty="bottom of the list is cropped",
            image_count=1,
            image_metadata=[{"filename": "boat.png", "submitted_bytes": 1234}],
            result={"event_type": "arena_relay_screenshot_observation"},
            conn=conn,
        )
        rows = db.list_arena_relay_screenshot_observations(conn=conn)

        assert saved["screenshot_type"] == "boat_defense"
        assert rows[0]["players_json"] == ["dez42", "WaltadR"]
        assert rows[0]["actionable_facts_json"] == ["At least three boat defense slots are visible."]
        assert rows[0]["image_metadata_json"][0]["filename"] == "boat.png"
        assert rows[0]["result_json"]["event_type"] == "arena_relay_screenshot_observation"
    finally:
        conn.close()


def test_clan_voyage_capture_merges_pages_and_resolves_members():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#AAA111", "name": "Aaqib Javed", "role": "member", "trophies": 9000, "clanRank": 1},
                {"tag": "#KING001", "name": "King Thing", "role": "leader", "trophies": 11000, "clanRank": 2},
                {"tag": "#TH15GUY", "name": "TH15_Guy", "role": "elder", "trophies": 8500, "clanRank": 18},
            ],
            conn=conn,
        )
        choices = db.build_clan_voyage_roster_choices(conn=conn)
        th15 = next(choice for choice in choices if choice["player_tag"] == "#TH15GUY")
        assert th15["name"] == "TH15_Guy"
        assert th15["ocr_key"] == "thisguy"
        validation = db.validate_clan_voyage_entry_match("THIS_GUY", "#TH15GUY", conn=conn)
        assert validation["accepted"] is True
        assert validation["reason"] == "ocr_exact"

        first = db.upsert_clan_voyage_capture(
            source_message_id=1001,
            channel_id=1513758211206025227,
            channel_name="leader-actions",
            observed_at="2026-06-14T11:23:00",
            clan_name="POAP KINGS",
            completed=True,
            ends_in_text="21h 36min",
            image_count=6,
            entries=[
                {"rank": 1, "player_name": "Aaqib Javed", "role": "Member", "points": 388, "source_image_index": 1, "confidence": 0.93},
                {"rank": 11, "player_name": "King Thing", "role": "Leader", "points": 148, "source_image_index": 3, "confidence": 0.9},
            ],
            conn=conn,
        )
        second = db.upsert_clan_voyage_capture(
            source_message_id=1002,
            channel_id=1513758211206025227,
            channel_name="leader-actions",
            observed_at="2026-06-14T11:30:00",
            clan_name="POAP KINGS",
            completed=True,
            ends_in_text="21h 29min",
            image_count=4,
            entries=[
                {"rank": 2, "player_name": "The Joesma", "role": "Member", "points": 243, "source_image_index": 1, "confidence": 0.88},
                {"rank": 37, "player_name": "EddiePlayz", "role": "Member", "points": 0, "source_image_index": 4, "confidence": 0.86},
            ],
            conn=conn,
        )

        assert first["voyage_id"] == second["voyage_id"]
        latest = db.get_latest_clan_voyage(include_entries=True, conn=conn)
        assert latest["completed"] is True
        assert latest["season_key"] == "2026-06"
        assert latest["event_end_at"] == "2026-06-15T08:59:00"
        assert latest["source_message_ids_json"] == ["1001", "1002"]
        assert latest["image_count"] == 10
        assert [entry["rank"] for entry in latest["entries"]] == [1, 2, 11, 37]
        assert latest["entries"][0]["player_tag"] == "#AAA111"
        assert latest["entries"][1]["player_tag"] is None
        assert latest["entries"][3]["points"] == 0

        summary = db.get_member_clan_voyage_summary("#KING001", conn=conn)
        assert summary["best_rank"] == 11
        assert summary["latest_points"] == 148
        assert "Clan Voyage" in db.build_clan_voyage_context(conn=conn)
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


def test_list_channel_messages_skips_blank_history_before_limit():
    conn = db.get_connection(":memory:")
    try:
        db.save_message(
            "channel_user:100:123",
            "assistant",
            "First useful post",
            channel_id=100,
            workflow="clanops",
            conn=conn,
        )
        db.save_message(
            "channel_user:100:123",
            "user",
            "",
            channel_id=100,
            workflow="interactive",
            conn=conn,
        )
        db.save_message(
            "channel_user:100:123",
            "assistant",
            "   ",
            channel_id=100,
            workflow="clanops",
            conn=conn,
        )
        db.save_message(
            "channel_user:100:123",
            "assistant",
            "Second useful post",
            channel_id=100,
            workflow="clanops",
            conn=conn,
        )

        history = db.list_channel_messages(100, limit=2, conn=conn)

        assert [turn["content"] for turn in history] == [
            "First useful post",
            "Second useful post",
        ]
    finally:
        conn.close()


def test_resolve_member_folds_diacritics_for_non_ascii_names():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "José", "role": "member"},
                {"tag": "#DEF456", "name": "Pokémon", "role": "member"},
                {"tag": "#GHI789", "name": "Malmö", "role": "member"},
            ],
            conn=conn,
        )

        # ASCII query matches accented stored name
        jose = db.resolve_member("jose", conn=conn)
        assert jose[0]["player_tag"] == "#ABC123"
        assert jose[0]["match_source"] == "current_name_exact"

        # Accented query matches accented stored name
        jose_accented = db.resolve_member("José", conn=conn)
        assert jose_accented[0]["player_tag"] == "#ABC123"

        # Substring with accents folds correctly
        pokemon = db.resolve_member("pokem", conn=conn)
        assert pokemon[0]["player_tag"] == "#DEF456"
        assert pokemon[0]["match_source"] == "current_name_prefix"

        # Accented query folds on the query side too
        malmo = db.resolve_member("malmo", conn=conn)
        assert malmo[0]["player_tag"] == "#GHI789"
    finally:
        conn.close()


def test_resolve_member_matches_at_prefixed_discord_display_name():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "VijayCR", "role": "member"},
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
            llm_last_error="Error code: 429 rate_limit_exceeded",
            llm_last_model="claude-sonnet-4-6",
            llm_last_call_at="2026-03-07T19:12:00",
            raw_json={"event_type": "channel_response", "content": None},
            conn=conn,
        )

        failures = db.list_prompt_failures(conn=conn)

        assert len(failures) == 1
        assert failures[0]["failure_id"] == failure_id
        assert failures[0]["workflow"] == "clanops"
        assert failures[0]["failure_type"] == "agent_none"
        assert failures[0]["channel_name"] == "clan-ops"
        assert failures[0]["llm_last_model"] == "claude-sonnet-4-6"
        assert json.loads(failures[0]["raw_json"]) == {"event_type": "channel_response", "content": None}
    finally:
        conn.close()


def test_prompt_feedback_is_recorded_cleared_and_listed_for_review():
    conn = db.get_connection(":memory:")
    try:
        db.save_message(
            "channel_user:1482368505058955467:123",
            "user",
            "What deck should I learn next?",
            channel_id=1482368505058955467,
            channel_name="ask-elixir",
            channel_kind="text",
            discord_user_id=123,
            username="jamie",
            display_name="Jamie",
            workflow="interactive",
            discord_message_id=555,
            conn=conn,
        )
        db.save_message(
            "channel_user:1482368505058955467:123",
            "assistant",
            "Try a faster cycle deck first so you can learn matchups quickly.",
            channel_id=1482368505058955467,
            channel_name="ask-elixir",
            channel_kind="text",
            discord_user_id=123,
            username="jamie",
            display_name="Jamie",
            workflow="interactive",
            event_type="channel_response",
            discord_message_id=777,
            conn=conn,
        )

        up = db.upsert_prompt_feedback(
            assistant_discord_message_id=777,
            discord_user_id=123,
            original_asker_discord_user_id=123,
            workflow="interactive",
            channel_id=1482368505058955467,
            channel_name="#ask-elixir",
            feedback_value="up",
            conn=conn,
        )
        assert up["feedback_value"] == "up"
        assert up["became_active_down"] is False

        down = db.upsert_prompt_feedback(
            assistant_discord_message_id=777,
            discord_user_id=123,
            original_asker_discord_user_id=123,
            workflow="interactive",
            channel_id=1482368505058955467,
            channel_name="#ask-elixir",
            feedback_value="down",
            conn=conn,
        )
        assert down["feedback_value"] == "down"
        assert down["became_active_down"] is True

        review_items = db.list_prompt_review_items(conn=conn)
        assert len(review_items) == 1
        assert review_items[0]["kind"] == "feedback"
        assert review_items[0]["feedback_value"] == "down"
        assert review_items[0]["question"] == "What deck should I learn next?"
        assert "faster cycle deck" in review_items[0]["result_preview"]

        positives = db.list_prompt_review_items(include_positive=True, conn=conn)
        assert positives[0]["feedback_value"] == "down"

        cleared = db.clear_prompt_feedback(
            assistant_discord_message_id=777,
            discord_user_id=123,
            feedback_value="down",
            conn=conn,
        )
        assert cleared == 1
        assert db.list_prompt_review_items(conn=conn) == []
    finally:
        conn.close()


def test_prompt_review_items_hide_positive_feedback_by_default():
    conn = db.get_connection(":memory:")
    try:
        db.save_message(
            "channel_user:1482368505058955467:123",
            "user",
            "Was that answer right?",
            channel_id=1482368505058955467,
            channel_name="ask-elixir",
            channel_kind="text",
            discord_user_id=123,
            username="jamie",
            display_name="Jamie",
            workflow="interactive",
            discord_message_id=901,
            conn=conn,
        )
        db.save_message(
            "channel_user:1482368505058955467:123",
            "assistant",
            "Yes, and here is why.",
            channel_id=1482368505058955467,
            channel_name="ask-elixir",
            channel_kind="text",
            discord_user_id=123,
            username="jamie",
            display_name="Jamie",
            workflow="interactive",
            event_type="channel_response",
            discord_message_id=902,
            conn=conn,
        )
        db.upsert_prompt_feedback(
            assistant_discord_message_id=902,
            discord_user_id=123,
            workflow="interactive",
            channel_id=1482368505058955467,
            channel_name="#ask-elixir",
            feedback_value="up",
            conn=conn,
        )

        assert db.list_prompt_review_items(conn=conn) == []
        review_items = db.list_prompt_review_items(include_positive=True, conn=conn)
        assert len(review_items) == 1
        assert review_items[0]["feedback_value"] == "up"
        assert review_items[0]["failure_type"] == "user_feedback_up"
    finally:
        conn.close()


def test_system_signal_queue_round_trips_pending_and_announced_state():
    conn = db.get_connection(":memory:")
    try:
        db.queue_system_signal(
            "capability_boat_defense_intelligence_v1",
            "capability_unlock",
            {
                "title": "Achievement Unlocked: Boat Defense Intel",
                "message": "Elixir can now read clan-level boat defense performance.",
            },
            conn=conn,
        )

        pending = db.list_pending_system_signals(conn=conn)

        assert len(pending) == 1
        assert pending[0]["signal_key"] == "capability_boat_defense_intelligence_v1"
        assert pending[0]["type"] == "capability_unlock"
        assert pending[0]["title"] == "Achievement Unlocked: Boat Defense Intel"
        assert pending[0]["signal_log_type"] == "system_signal::capability_boat_defense_intelligence_v1"

        db.mark_system_signal_announced("capability_boat_defense_intelligence_v1", conn=conn)

        assert db.list_pending_system_signals(conn=conn) == []
    finally:
        conn.close()


def test_api_sentinel_baselines_existing_payloads_without_signals():
    conn = db.get_connection(":memory:")
    try:
        payload = {
            "tag": "#ABC",
            "name": "King Thing",
            "badges": [{"name": "CollectionLevel", "progress": 1597}],
            "progress": {"seasonal-trophy-road-202606": {"wins": 2}},
        }
        db._store_raw_payload(conn, "player", "ABC", payload)
        conn.commit()

        result = db.bootstrap_api_sentinel_baseline(conn=conn)

        assert result["bootstrapped"] is True
        assert result["payloads"] == 1
        observations = db.list_api_sentinel_observations(limit=100, conn=conn)
        observed = {(item["sentinel_type"], item["scope"], item["name"]) for item in observations}
        assert ("badge_name", "player.badges", "CollectionLevel") in observed
        assert ("progress_key", "player.progress", "seasonal-trophy-road-202606") in observed
        assert ("schema_path", "player", "badges[].name") in observed
        assert db.list_pending_system_signals(conn=conn) == []
    finally:
        conn.close()


def test_api_sentinel_queues_schema_signal_for_new_observations():
    conn = db.get_connection(":memory:")
    try:
        db.record_api_payload_sentinel_observations(
            "player",
            "ABC",
            {"tag": "#ABC", "badges": [{"name": "CollectionLevel"}]},
            announce=False,
            conn=conn,
        )

        db.record_api_payload_sentinel_observations(
            "player",
            "ABC",
            {
                "tag": "#ABC",
                "newTopLevelField": True,
                "badges": [{"name": "ClanVoyageMaybe"}],
                "progress": {"MergeTactics_2026_Season_9": {"score": 12}},
            },
            conn=conn,
        )

        pending = db.list_pending_system_signals(conn=conn)
        assert len(pending) == 1
        assert pending[0]["type"] == "api_schema_sentinel"
        assert pending[0]["audience"] == "leadership"
        assert "ClanVoyageMaybe" in pending[0]["discord_content"]
        assert "MergeTactics_2026_Season_9" in pending[0]["discord_content"]
        assert "newTopLevelField" in pending[0]["discord_content"]
    finally:
        conn.close()


def test_api_sentinel_queues_event_signal_for_new_events():
    conn = db.get_connection(":memory:")
    try:
        db.record_api_payload_sentinel_observations(
            "events",
            "global",
            [
                {
                    "eventTag": "#2PRC9GU0",
                    "title": "Princess Gambit",
                    "description": None,
                }
            ],
            conn=conn,
        )

        pending = db.list_pending_system_signals(conn=conn)
        assert len(pending) == 1
        assert pending[0]["type"] == "api_event_sentinel"
        assert pending[0]["audience"] == "leadership"
        assert "Princess Gambit" in pending[0]["discord_content"]
        assert "#2PRC9GU0" in pending[0]["discord_content"]
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
        db.record_llm_call(
            "interactive",
            "claude-haiku-4-5-20251001",
            ok=True,
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
            conn=conn,
        )
        db.record_awareness_tick(
            workflow="clan_awareness",
            signals_in=2,
            posts_delivered=1,
            all_ok=True,
            conn=conn,
        )

        status = db.get_system_status(conn=conn)

        assert status["schema_version"] == len(db._MIGRATIONS)
        assert status["schema_display"] == f"baseline schema (migration v{len(db._MIGRATIONS)})"
        assert status["counts"]["members_active"] == 1
        assert status["counts"]["battle_fact_count"] == 1
        assert status["counts"]["message_count"] == 1
        assert status["freshness"]["member_state_at"] is not None
        assert status["freshness"]["player_profile_at"] is not None
        assert status["freshness"]["battle_fact_at"] is not None
        assert status["freshness"]["war_state_at"] is not None
        assert isinstance(status["raw_payloads_by_endpoint"], list)
        assert "contextual_memory" in status
        assert status["contextual_memory"]["total"] == 0
        assert status["contextual_memory"]["leader_notes"] == 0
        assert status["llm_cost_7d"]["calls"] == 1
        assert status["llm_cost_7d"]["cost_usd"] == 0.0015
        assert status["awareness_7d"]["ticks"] == 1
        assert status["awareness_7d"]["signals_in"] == 2
        assert status["awareness_7d"]["posts_delivered"] == 1
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
            "expPoints": 12345,
            "totalExpPoints": 54321,
            "starPoints": 777,
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
            "clanCardsCollected": 3210,
            "currentFavouriteCard": {"id": 26000011, "name": "Valkyrie"},
            "currentDeck": [
                {"name": "Valkyrie", "level": 14, "maxLevel": 14, "rarity": "rare", "iconUrls": {"medium": "icon://valk"}},
                {"name": "Goblin Barrel", "level": 10, "maxLevel": 11, "rarity": "epic", "iconUrls": {"medium": "icon://gb"}, "evolutionLevel": 2, "maxEvolutionLevel": 2},
                {"name": "Princess", "level": 6, "maxLevel": 8, "rarity": "legendary", "iconUrls": {"medium": "icon://princess"}},
                {"name": "Knight", "level": 16, "maxLevel": 16, "rarity": "common", "iconUrls": {"medium": "icon://knight"}, "evolutionLevel": 3, "maxEvolutionLevel": 3},
                {"name": "Rocket", "level": 14, "maxLevel": 14, "rarity": "rare", "iconUrls": {"medium": "icon://rocket"}},
                {"name": "Ice Spirit", "level": 16, "maxLevel": 16, "rarity": "common", "iconUrls": {"medium": "icon://ice"}},
                {"name": "Inferno Tower", "level": 10, "maxLevel": 11, "rarity": "epic", "iconUrls": {"medium": "icon://inferno"}},
                {"name": "Log", "level": 8, "maxLevel": 8, "rarity": "legendary", "iconUrls": {"medium": "icon://log"}},
            ],
            "currentDeckSupportCards": [
                {"name": "Dagger Duchess", "level": 4, "maxLevel": 4, "rarity": "legendary", "iconUrls": {"medium": "icon://duchess"}},
            ],
            "cards": [],
            "supportCards": [
                {"name": "Dagger Duchess", "level": 4, "maxLevel": 4, "rarity": "legendary", "iconUrls": {"medium": "icon://duchess"}},
            ],
            "badges": [],
            "achievements": [],
            "leagueStatistics": {"currentSeason": {"trophies": 11429}},
            "currentPathOfLegendSeasonResult": {"leagueNumber": 9, "trophies": 2000, "rank": 1234},
            "lastPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1800, "rank": 2345},
            "bestPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 2200, "rank": 345},
            "legacyTrophyRoadHighScore": 9000,
            "progress": {"AutoChess_2026_Mar": {"trophies": 3460, "bestTrophies": 3593}},
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
        assert deck["cards"][0]["maxLevel"] == 16
        assert deck["cards"][0]["api_max_level"] == 14
        assert deck["cards"][1]["level"] == 15
        assert deck["cards"][1]["api_level"] == 10
        assert deck["cards"][1]["maxLevel"] == 16
        assert deck["cards"][1]["api_max_level"] == 11
        assert deck["cards"][1]["supports_hero"] is True
        assert deck["cards"][1]["mode_label"] == "Hero"
        assert deck["cards"][1]["mode_status_label"] == "Hero unlocked"
        assert deck["cards"][3]["supports_evo"] is True
        assert deck["cards"][3]["supports_hero"] is True
        assert deck["cards"][3]["mode_label"] == "Evo + Hero"
        assert deck["support_cards"][0]["name"] == "Dagger Duchess"
        assert deck["support_cards"][0]["level"] == 16
        assert deck["support_cards"][0]["api_level"] == 4
        assert deck["support_cards"][0]["maxLevel"] == 16
        assert deck["support_cards"][0]["api_max_level"] == 4

        profile_row = conn.execute(
            "SELECT exp_points, total_exp_points, star_points, clan_cards_collected, "
            "current_deck_support_cards_json, current_path_of_legend_season_result_json, "
            "legacy_trophy_road_high_score, progress_json "
            "FROM player_profile_snapshots ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        assert profile_row["exp_points"] == 12345
        assert profile_row["total_exp_points"] == 54321
        assert profile_row["star_points"] == 777
        assert profile_row["clan_cards_collected"] == 3210
        assert json.loads(profile_row["current_deck_support_cards_json"])[0]["name"] == "Dagger Duchess"
        assert json.loads(profile_row["current_deck_support_cards_json"])[0]["level"] == 16
        assert json.loads(profile_row["current_deck_support_cards_json"])[0]["api_level"] == 4
        # The full card collection is stored once in member_card_collection_snapshots,
        # not duplicated into player_profile_snapshots.
        collection_row = conn.execute(
            "SELECT support_cards_json FROM member_card_collection_snapshots ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        assert json.loads(collection_row["support_cards_json"])[0]["name"] == "Dagger Duchess"
        assert json.loads(collection_row["support_cards_json"])[0]["maxLevel"] == 16
        assert json.loads(collection_row["support_cards_json"])[0]["api_max_level"] == 4
        assert json.loads(profile_row["current_path_of_legend_season_result_json"])["leagueNumber"] == 9
        assert profile_row["legacy_trophy_road_high_score"] == 9000
        assert json.loads(profile_row["progress_json"])["AutoChess_2026_Mar"]["trophies"] == 3460

        cards = db.get_member_signature_cards("#ABC123", conn=conn)
        assert cards["sample_battles"] == 2
        assert cards["cards"][0]["name"] == "Valkyrie"

        form = db.get_member_recent_form("#ABC123", conn=conn)
        assert form["wins"] == 1
        assert form["losses"] == 1
        assert form["sample_size"] == 2
        # Tag exposure: LLM needs the player_tag to chain into cr_api.
        assert form["player_tag"] == "#ABC123"
    finally:
        conn.close()


def test_get_member_card_collection_returns_collection_summary_and_levels():
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
                "cards": [
                    {"name": "Knight", "level": 16, "maxLevel": 16, "rarity": "common", "evolutionLevel": 3, "maxEvolutionLevel": 3},
                    {"name": "Hog Rider", "level": 14, "maxLevel": 14, "rarity": "rare"},
                    {"name": "Fireball", "level": 10, "maxLevel": 11, "rarity": "epic", "maxEvolutionLevel": 1},
                ],
                "supportCards": [
                    {"name": "Dagger Duchess", "level": 4, "maxLevel": 4, "rarity": "legendary"},
                ],
            },
            conn=conn,
        )

        collection = db.get_member_card_collection("#ABC123", conn=conn)
        profile = db.get_member_profile("#ABC123", conn=conn)

        assert {card["name"] for card in collection["cards"][:2]} == {"Knight", "Hog Rider"}
        assert collection["cards"][0]["level"] == 16
        assert collection["cards"][0]["maxLevel"] == 16
        assert collection["cards"][0]["supports_evo"] is True
        assert collection["cards"][0]["supports_hero"] is True
        assert collection["cards"][0]["mode_label"] == "Evo + Hero"
        assert collection["cards"][0]["mode_status_label"] == "Evo + Hero unlocked"
        assert collection["support_cards"][0]["name"] == "Dagger Duchess"
        assert collection["support_cards"][0]["maxLevel"] == 16
        assert collection["support_cards"][0]["levels_to_max"] == 0
        assert collection["cards"][2]["supports_evo"] is True
        assert collection["cards"][2]["mode_label"] is None
        assert collection["summary"]["cards_tracked"] == 3
        assert collection["summary"]["support_cards_tracked"] == 1
        assert collection["summary"]["highest_level"] == 16
        assert collection["summary"]["maxed_cards_count"] == 3
        assert profile["card_collection_summary"]["highest_level"] == 16
        assert "Knight" in {
            card["name"] for card in profile["card_collection_summary"]["strongest_cards"][:3]
        }
        assert profile["card_collection_summary"]["strongest_cards"][0]["mode_label"] == "Evo + Hero"
    finally:
        conn.close()


def test_card_mode_fields_interpret_observed_evo_and_hero_mapping():
    from storage import cards

    cases = [
        ({}, (False, False, False, False, None, None)),
        ({"maxEvolutionLevel": 1}, (True, False, False, False, None, None)),
        ({"maxEvolutionLevel": 1, "evolutionLevel": 1}, (True, False, True, False, "Evo", "Evo unlocked")),
        ({"maxEvolutionLevel": 2, "evolutionLevel": 2}, (False, True, False, True, "Hero", "Hero unlocked")),
        ({"maxEvolutionLevel": 3, "evolutionLevel": 1}, (True, True, True, False, "Evo", "Evo unlocked")),
        ({"maxEvolutionLevel": 3, "evolutionLevel": 2}, (True, True, False, True, "Hero", "Hero unlocked")),
        ({"maxEvolutionLevel": 3, "evolutionLevel": 3}, (True, True, True, True, "Evo + Hero", "Evo + Hero unlocked")),
    ]

    for card, expected in cases:
        interpreted = cards._card_mode_fields(card)
        assert (
            interpreted["supports_evo"],
            interpreted["supports_hero"],
            interpreted["evo_unlocked"],
            interpreted["hero_unlocked"],
            interpreted["mode_label"],
            interpreted["mode_status_label"],
        ) == expected


def test_get_member_recent_battles_surfaces_cap_when_requested_over_limit():
    """When the caller asks for more than the cap, the result must surface
    requested_limit + capped_at so the LLM can tell the user instead of
    silently answering against fewer battles than asked."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Tester", "role": "member"}],
            conn=conn,
        )
        # Under-cap request: no cap fields surfaced.
        result = db.get_member_recent_battles("#ABC123", limit=5, conn=conn)
        assert "capped_at" not in result
        assert "requested_limit" not in result

        # Over-cap request (50 > 100? no — try 250 > 100):
        result = db.get_member_recent_battles("#ABC123", limit=250, conn=conn)
        assert result["requested_limit"] == 250
        assert result["capped_at"] == 100
        assert "Tell the user" in result["cap_note"]
    finally:
        conn.close()


def test_cards_required_to_upgrade_table_returns_known_values():
    from cr_knowledge import cards_required_to_upgrade, is_ready_to_upgrade

    # Spot-check known wiki values across rarities.
    assert cards_required_to_upgrade("common", 1) == 2
    assert cards_required_to_upgrade("rare", 9) == 800
    assert cards_required_to_upgrade("epic", 5) == 100
    assert cards_required_to_upgrade("legendary", 1) == 2
    assert cards_required_to_upgrade("champion", 1) == 5

    # Beyond the table → None (already maxed or out of range).
    assert cards_required_to_upgrade("legendary", 99) is None
    assert cards_required_to_upgrade("unknown_rarity", 1) is None
    assert cards_required_to_upgrade("common", 0) is None

    # is_ready_to_upgrade composes count + rarity + level.
    assert is_ready_to_upgrade("rare", 9, 800) is True
    assert is_ready_to_upgrade("rare", 9, 799) is False
    assert is_ready_to_upgrade("legendary", 99, 1000) is False  # maxed → False


def _seed_member_with_collection(conn, *, exp_level=12):
    db.snapshot_members(
        [{"tag": "#ABC123", "name": "Tester", "role": "member", "expLevel": exp_level}],
        conn=conn,
    )
    db.snapshot_player_profile(
        {
            "tag": "#ABC123",
            "name": "Tester",
            "expLevel": exp_level,
            "cards": [
                # Common, level 9, has 1000 (needed for L9->10 is 800) → ready_to_upgrade.
                {"name": "Knight", "level": 9, "maxLevel": 16, "rarity": "common", "count": 1000},
                # Rare, level 13, 200 of 1500 needed → near_ready=False (under 50%).
                {"name": "Hog Rider", "level": 13, "maxLevel": 16, "rarity": "rare", "count": 200},
                # Rare, level 9, 500 of 800 → near_ready (>=50% but not ready).
                {"name": "Musketeer", "level": 9, "maxLevel": 16, "rarity": "rare", "count": 500},
                # Epic, level 14, 1 level from max → near_max.
                {"name": "P.E.K.K.A", "level": 14, "maxLevel": 15, "rarity": "epic", "count": 50},
                # Legendary, max level → maxed.
                {"name": "Log", "level": 16, "maxLevel": 16, "rarity": "legendary", "count": 100},
                # Common at level 8, king tower 12 → king_tower_gap = 4.
                {"name": "Archers", "level": 8, "maxLevel": 16, "rarity": "common", "count": 100},
                # Card with evolution unlocked.
                {"name": "Valkyrie", "level": 14, "maxLevel": 16, "rarity": "rare", "count": 100, "evolutionLevel": 1, "maxEvolutionLevel": 1},
            ],
        },
        conn=conn,
    )


def test_get_member_card_profile_returns_compact_digest_with_upgrade_signals():
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn, exp_level=12)
        profile = db.get_member_card_profile("#ABC123", conn=conn)

        # exp_level=12 is below the King Tower cap (16), so king_tower
        # equals experience_level. Both are surfaced separately.
        assert profile["king_tower_level"] == 12
        assert profile["experience_level"] == 12
        assert profile["king_tower_max"] == 16
        # 7 cards total, 1 maxed (Log).
        assert profile["totals"]["owned"] == 7
        assert profile["totals"]["max_level"] == 1
        # By-rarity breakdown.
        assert profile["by_rarity"]["common"]["owned"] == 2
        assert profile["by_rarity"]["common"]["ready"] == 1  # Knight is ready
        # Knight should top the ready list with 1000 stockpiled vs 800 required.
        assert profile["ready_to_upgrade_top"][0]["name"] == "Knight"
        assert profile["ready_to_upgrade_top"][0]["count"] == 1000
        # Closest-to-max should surface P.E.K.K.A (1 level from max, not maxed).
        names = [c["name"] for c in profile["closest_to_max_top"]]
        assert "P.E.K.K.A" in names
        # King-tower gap should surface Archers (level 8 vs king tower 12, gap 4).
        assert profile["biggest_king_tower_gaps_top"][0]["name"] == "Archers"
        assert profile["biggest_king_tower_gaps_top"][0]["king_tower_gap"] == 4
        # Mode counts pick up Valkyrie's unlocked evo.
        assert profile["modes"]["evo_unlocked"] == 1
    finally:
        conn.close()


def test_get_member_card_profile_clamps_king_tower_at_cap():
    """When experience_level exceeds the King Tower cap (16), king_tower_level
    must clamp at the cap. Reading expLevel raw produced nonsense gaps like
    "27 levels" on shimmeringhost (cards L7 vs expLevel 34) when actual gap
    was 9 (L7 vs King Tower 16, capped)."""
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn, exp_level=34)  # past cap
        profile = db.get_member_card_profile("#ABC123", conn=conn)
        assert profile["experience_level"] == 34
        assert profile["king_tower_level"] == 16  # clamped
        # Archers at level 8 vs King Tower 16 = gap of 8, NOT 26.
        archers = next(
            c for c in profile["biggest_king_tower_gaps_top"]
            if c["name"] == "Archers"
        )
        assert archers["king_tower_gap"] == 8
    finally:
        conn.close()


def test_get_member_card_profile_under_3kb_serialized():
    """Digest must stay small enough that it never triggers truncation."""
    conn = db.get_connection(":memory:")
    try:
        # Seed with a realistic 100-card collection.
        cards = [
            {"name": f"Card{i:03d}", "level": (i % 14) + 1, "maxLevel": 16,
             "rarity": ["common", "rare", "epic", "legendary"][i % 4], "count": (i * 13) % 2000}
            for i in range(120)
        ]
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Stress", "role": "member", "expLevel": 14}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "Stress", "expLevel": 14, "cards": cards},
            conn=conn,
        )
        profile = db.get_member_card_profile("#ABC123", conn=conn)
        size = len(json.dumps(profile))
        assert size < 3072, f"digest is {size} bytes; must stay under 3KB"
    finally:
        conn.close()


def test_lookup_member_cards_requires_filter():
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn)
        # No filter → structured filter_required error with hints.
        result = db.lookup_member_cards("#ABC123", filter=None, conn=conn)
        assert result["error"] == "filter_required"
        assert any("ready_to_upgrade" in hint for hint in result["available_filters"])
        assert "ask the user" in result["suggest_clarify"].lower()
        # Empty-filter dict also rejected.
        result = db.lookup_member_cards("#ABC123", filter={}, conn=conn)
        assert result["error"] == "filter_required"
    finally:
        conn.close()


def test_lookup_member_cards_ready_to_upgrade_returns_only_ready():
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn)
        result = db.lookup_member_cards(
            "#ABC123", filter={"ready_to_upgrade": True}, conn=conn,
        )
        names = {c["name"] for c in result["cards"]}
        # Knight (common L9, count 1000 ≥ 800 required) is ready.
        assert "Knight" in names
        # Hog Rider (rare L13, only 200 of 1500) is NOT ready.
        assert "Hog Rider" not in names
        # Log is maxed → never ready.
        assert "Log" not in names
        # Each returned card carries upgrade context.
        for card in result["cards"]:
            assert card["ready_to_upgrade"] is True
            assert card["count"] >= card["cards_required_for_next_level"]
    finally:
        conn.close()


def test_lookup_member_cards_filters_by_rarity_and_near_max():
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn)
        # Rarity filter.
        result = db.lookup_member_cards("#ABC123", filter={"rarity": "common"}, conn=conn)
        names = {c["name"] for c in result["cards"]}
        assert names == {"Knight", "Archers"}
        # near_max returns cards 1-2 levels from max: P.E.K.K.A (1 from max)
        # and Valkyrie (2 from max). Log is maxed → excluded.
        result = db.lookup_member_cards("#ABC123", filter={"near_max": True}, conn=conn)
        assert {c["name"] for c in result["cards"]} == {"P.E.K.K.A", "Valkyrie"}
    finally:
        conn.close()


def test_lookup_member_cards_war_mode_carries_caveat():
    conn = db.get_connection(":memory:")
    try:
        _seed_member_with_collection(conn)
        result = db.lookup_member_cards("#ABC123", filter={"mode": "war"}, conn=conn)
        # No war battles seeded → empty matching, but caveat must be present.
        assert "war_deck_caveat" in result
        assert "not authoritative" in result["war_deck_caveat"]
    finally:
        conn.close()


def test_get_member_card_collection_can_filter_by_rarity_for_full_collection_questions():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Thing", "role": "leader"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Thing",
                "cards": [
                    {"name": "Royal Ghost", "level": 14, "maxLevel": 16, "rarity": "legendary"},
                    {"name": "Inferno Dragon", "level": 14, "maxLevel": 16, "rarity": "legendary"},
                    {"name": "Princess", "level": 13, "maxLevel": 16, "rarity": "legendary"},
                    {"name": "Log", "level": 12, "maxLevel": 16, "rarity": "legendary"},
                    {"name": "Knight", "level": 16, "maxLevel": 16, "rarity": "common"},
                ],
                "supportCards": [
                    {"name": "Tower Princess", "level": 15, "maxLevel": 16, "rarity": "legendary"},
                ],
            },
            conn=conn,
        )

        collection = db.get_member_card_collection("#ABC123", rarity="legendaries", conn=conn)

        assert collection["rarity_filter"] == "legendary"
        assert collection["matching_total_cards"] == 5
        assert {card["name"] for card in collection["cards"]} == {
            "Royal Ghost",
            "Inferno Dragon",
            "Princess",
            "Log",
        }
        assert {card["name"] for card in collection["support_cards"]} == {"Tower Princess"}
        assert set(collection["cards_by_rarity"]["legendary"]) == {
            "Royal Ghost",
            "Inferno Dragon",
            "Princess",
            "Log",
            "Tower Princess (support)",
        }
        assert collection["collection_summary"]["rarity_counts"]["legendary"] == 5
    finally:
        conn.close()


def test_member_daily_battle_rollups_group_by_chicago_day_and_mode_group():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#ABC123'"
        ).fetchone()["member_id"]
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, battle_count) VALUES (?, ?, ?)",
            (member_id, "2026-01-10T05:00:00", 100),
        )
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, battle_count) VALUES (?, ?, ?)",
            (member_id, "2026-01-11T05:00:00", 103),
        )
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, battle_count) VALUES (?, ?, ?)",
            (member_id, "2026-01-12T05:00:00", 104),
        )
        conn.commit()

        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "PvP",
                    "battleTime": "20260111T013000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 30, "startingTrophies": 5000, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "pathOfLegend",
                    "battleTime": "20260111T023000.000Z",
                    "gameMode": {"id": 72000464, "name": "Ranked1v1_NewArena2"},
                    "leagueNumber": 7,
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 0, "trophyChange": -20, "startingTrophies": 5030, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "boatBattle",
                    "battleTime": "20260111T030000.000Z",
                    "gameMode": {"id": 72000061, "name": "Boat Battle"},
                    "boatBattleWon": False,
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 0, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP3", "name": "Opp 3", "crowns": 0, "cards": [], "supportCards": []}],
                },
                {
                    "type": "PvP",
                    "battleTime": "20260111T070000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "trophyChange": 25, "startingTrophies": 5010, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP4", "name": "Opp 4", "crowns": 1, "cards": [], "supportCards": []}],
                },
            ],
            conn=conn,
        )

        # Pin "today" near the fixture dates so the days window never expires
        # as the wall clock advances.
        with patch("storage.player.chicago_today", return_value="2026-01-12"):
            rollups = db.list_member_daily_battle_rollups("#ABC123", days=120, conn=conn)

        assert [(row["battle_date"], row["mode_group"]) for row in rollups] == [
            ("2026-01-10", "ladder"),
            ("2026-01-10", "ranked"),
            ("2026-01-10", "war"),
            ("2026-01-11", "ladder"),
        ]
        assert rollups[0]["battles"] == 1
        assert rollups[0]["wins"] == 1
        assert rollups[0]["trophy_change_total"] == 30
        assert rollups[0]["captured_battles"] == 3
        assert rollups[0]["expected_battle_delta"] == 3
        assert rollups[0]["completeness_ratio"] == 1.0
        assert rollups[0]["is_complete"] == 1
        assert rollups[1]["losses"] == 1
        assert rollups[1]["trophy_change_total"] == -20
        assert rollups[2]["losses"] == 1
        assert rollups[3]["battle_date"] == "2026-01-11"
        assert rollups[3]["captured_battles"] == 1
        assert rollups[3]["expected_battle_delta"] == 1
        assert rollups[3]["is_complete"] == 1
    finally:
        conn.close()


def test_clan_daily_battle_rollups_aggregate_member_daily_rollups():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "Alpha", "role": "leader"},
                {"tag": "#DEF456", "name": "Bravo", "role": "member"},
            ],
            conn=conn,
        )
        alpha_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#ABC123'"
        ).fetchone()["member_id"]
        bravo_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#DEF456'"
        ).fetchone()["member_id"]
        for member_id, before_count, after_count in ((alpha_id, 100, 102), (bravo_id, 50, 51)):
            conn.execute(
                "INSERT INTO player_profile_snapshots (member_id, fetched_at, battle_count) VALUES (?, ?, ?)",
                (member_id, "2026-01-10T05:00:00", before_count),
            )
            conn.execute(
                "INSERT INTO player_profile_snapshots (member_id, fetched_at, battle_count) VALUES (?, ?, ?)",
                (member_id, "2026-01-11T05:00:00", after_count),
            )
        conn.commit()

        db.snapshot_clan_daily_metrics(
            {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "members": 2,
                "memberList": [
                    {"tag": "#ABC123", "name": "Alpha", "trophies": 7100, "donations": 10},
                    {"tag": "#DEF456", "name": "Bravo", "trophies": 6900, "donations": 20},
                ],
            },
            observed_at="2026-01-11T18:00:00",
            conn=conn,
        )

        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "PvP",
                    "battleTime": "20260111T013000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "Alpha", "crowns": 2, "trophyChange": 30, "startingTrophies": 5000, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "PvP",
                    "battleTime": "20260111T023000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "Alpha", "crowns": 3, "trophyChange": 20, "startingTrophies": 5030, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 0, "cards": [], "supportCards": []}],
                },
            ],
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#DEF456",
            [
                {
                    "type": "PvP",
                    "battleTime": "20260111T033000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#DEF456", "name": "Bravo", "crowns": 1, "trophyChange": -10, "startingTrophies": 4000, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP3", "name": "Opp 3", "crowns": 2, "cards": [], "supportCards": []}],
                },
            ],
            conn=conn,
        )

        # Pin "today" near the fixture dates so the days window never expires
        # as the wall clock advances.
        with patch("storage.player.chicago_today", return_value="2026-01-12"):
            rollups = db.list_clan_daily_battle_rollups(days=120, conn=conn)

        assert len(rollups) == 1
        assert rollups[0]["battle_date"] == "2026-01-10"
        assert rollups[0]["clan_tag"] == "#J2RGCRVG"
        assert rollups[0]["mode_group"] == "ladder"
        assert rollups[0]["members_active"] == 2
        assert rollups[0]["battles"] == 3
        assert rollups[0]["wins"] == 2
        assert rollups[0]["losses"] == 1
        assert rollups[0]["draws"] == 0
        assert rollups[0]["trophy_change_total"] == 40
        assert rollups[0]["captured_battles"] == 3
        assert rollups[0]["expected_battle_delta"] == 3
        assert rollups[0]["completeness_ratio"] == 1.0
        assert rollups[0]["is_complete"] == 1
    finally:
        conn.close()


def test_trend_query_helpers_and_prompt_ready_summaries():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#ABC123'"
        ).fetchone()["member_id"]
        conn.execute("DELETE FROM member_daily_metrics WHERE member_id = ?", (member_id,))

        member_daily_rows = [
            ("2026-03-06", 7000, 7100, 3, 60),
            ("2026-03-07", 7020, 7100, 3, 60),
            ("2026-03-08", 7040, 7100, 2, 60),
            ("2026-03-09", 7060, 7120, 2, 61),
            ("2026-03-10", 7090, 7130, 2, 61),
            ("2026-03-11", 7110, 7140, 1, 61),
        ]
        for metric_date, trophies, best_trophies, clan_rank, exp_level in member_daily_rows:
            conn.execute(
                "INSERT INTO member_daily_metrics (member_id, metric_date, trophies, best_trophies, clan_rank, exp_level) VALUES (?, ?, ?, ?, ?, ?)",
                (member_id, metric_date, trophies, best_trophies, clan_rank, exp_level),
            )

        member_battle_rows = [
            ("2026-03-06", "ladder", 1, 1, 0, 0, 20),
            ("2026-03-07", "ladder", 2, 1, 1, 0, 5),
            ("2026-03-08", "ranked", 1, 1, 0, 0, 15),
            ("2026-03-09", "ladder", 2, 2, 0, 0, 30),
            ("2026-03-10", "ranked", 2, 1, 1, 0, 10),
            ("2026-03-11", "ladder", 3, 2, 1, 0, 25),
        ]
        for battle_date, mode_group, battles, wins, losses, draws, trophy_delta in member_battle_rows:
            conn.execute(
                "INSERT INTO member_daily_battle_rollups (member_id, battle_date, mode_group, battles, wins, losses, draws, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete, last_aggregated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (member_id, battle_date, mode_group, battles, wins, losses, draws, trophy_delta, battles, battles, 1.0, 1, "2026-03-11T12:00:00"),
            )

        clan_daily_rows = [
            ("2026-03-06", 20, 4500, 140000),
            ("2026-03-07", 20, 4510, 140500),
            ("2026-03-08", 21, 4520, 141200),
            ("2026-03-09", 21, 4540, 142000),
            ("2026-03-10", 22, 4560, 143100),
            ("2026-03-11", 22, 4580, 144200),
        ]
        for metric_date, member_count, clan_score, total_member_trophies in clan_daily_rows:
            conn.execute(
                "INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name, member_count, open_slots, clan_score, total_member_trophies, avg_member_trophies, top_member_trophies, joins_today, leaves_today, net_member_change, observed_at) VALUES (?, '#J2RGCRVG', 'POAP KINGS', ?, ?, ?, ?, ?, ?, 0, 0, 0, '2026-03-11T12:00:00')",
                (metric_date, member_count, 50 - member_count, clan_score, total_member_trophies, round(total_member_trophies / member_count, 2), 7200),
            )

        clan_battle_rows = [
            ("2026-03-06", 6, 4, 2, 0, 40),
            ("2026-03-07", 7, 4, 3, 0, 25),
            ("2026-03-08", 5, 3, 2, 0, 15),
            ("2026-03-09", 8, 5, 3, 0, 35),
            ("2026-03-10", 9, 6, 3, 0, 30),
            ("2026-03-11", 10, 7, 3, 0, 45),
        ]
        for battle_date, battles, wins, losses, draws, trophy_delta in clan_battle_rows:
            conn.execute(
                "INSERT INTO clan_daily_battle_rollups (battle_date, clan_tag, clan_name, mode_group, members_active, battles, wins, losses, draws, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete, last_aggregated_at) VALUES (?, '#J2RGCRVG', 'POAP KINGS', 'ladder', 5, ?, ?, ?, ?, ?, ?, ?, 1.0, 1, '2026-03-11T12:00:00')",
                (battle_date, battles, wins, losses, draws, trophy_delta, battles, battles),
            )
        conn.commit()

        with patch("storage.trends.chicago_today", return_value="2026-03-11"):
            trophy_history = db.get_member_trophy_history("#ABC123", days=7, conn=conn)
            member_cmp = db.compare_member_trend_windows("#ABC123", window_days=3, conn=conn)
            clan_cmp = db.compare_clan_trend_windows(window_days=3, conn=conn)
            member_summary = db.build_member_trend_summary_context("#ABC123", days=7, window_days=3, conn=conn)
            clan_summary = db.build_clan_trend_summary_context(days=7, window_days=3, conn=conn)

        assert len(trophy_history) == 6
        assert trophy_history[-1]["trophies"] == 7110
        assert member_cmp["current"]["trophies"]["delta"] == 50
        assert member_cmp["previous"]["trophies"]["delta"] == 40
        assert member_cmp["current"]["battle_activity"]["battles"] == 7
        assert member_cmp["previous"]["battle_activity"]["battles"] == 4
        assert clan_cmp["current"]["clan_score"]["delta"] == 40
        assert clan_cmp["previous"]["clan_score"]["delta"] == 20
        assert clan_cmp["current"]["battle_activity"]["battles"] == 27
        assert clan_cmp["previous"]["battle_activity"]["battles"] == 18
        assert "=== MEMBER TREND SUMMARY ===" in member_summary
        assert "current_3d_vs_previous_3d:" in member_summary
        assert "=== CLAN TREND SUMMARY ===" in clan_summary
        assert "clan: POAP KINGS (#J2RGCRVG)" in clan_summary
    finally:
        conn.close()


def test_snapshot_player_profile_emits_card_evolution_unlocked_signal():
    """v4.7 #33: evolution unlock (evo or hero) fires card_evolution_unlocked."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        # Seed: Hunter owned but not evolved.
        db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy",
                "currentDeck": [], "cards": [
                    {"name": "Hunter", "level": 14, "rarity": "epic",
                     "evolutionLevel": 0, "maxEvolutionLevel": 1}
                ],
            },
            conn=conn,
        )
        # Now: Hunter evolved.
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy",
                "currentDeck": [], "cards": [
                    {"name": "Hunter", "level": 14, "rarity": "epic",
                     "evolutionLevel": 1, "maxEvolutionLevel": 1}
                ],
            },
            conn=conn,
        )

        evo_signals = [s for s in signals if s["type"] == "card_evolution_unlocked"]
        assert len(evo_signals) == 1
        sig = evo_signals[0]
        assert sig["tag"] == "#ABC123"
        assert sig["card_name"] == "Hunter"
        assert sig["old_evolution_level"] == 0
        assert sig["new_evolution_level"] == 1
        assert sig["evolution_kind"] == "evo"
        assert sig["rarity"] == "epic"
    finally:
        conn.close()


def test_snapshot_player_profile_does_not_refire_evolution_on_subsequent_snapshot():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [],
             "cards": [{"name": "Hunter", "level": 14, "rarity": "epic",
                        "evolutionLevel": 1, "maxEvolutionLevel": 1}]},
            conn=conn,
        )
        # Next snapshot: evolution level unchanged.
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [],
             "cards": [{"name": "Hunter", "level": 14, "rarity": "epic",
                        "evolutionLevel": 1, "maxEvolutionLevel": 1}]},
            conn=conn,
        )
        assert not any(s["type"] == "card_evolution_unlocked" for s in signals)
    finally:
        conn.close()


def test_snapshot_player_profile_emits_hero_evolution_signal():
    """Hero evolution (evolutionLevel 0 -> 2) fires with kind='hero'."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [],
             "cards": [{"name": "Mega Knight", "level": 14, "rarity": "legendary",
                        "evolutionLevel": 0, "maxEvolutionLevel": 3}]},
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [],
             "cards": [{"name": "Mega Knight", "level": 14, "rarity": "legendary",
                        "evolutionLevel": 2, "maxEvolutionLevel": 3}]},
            conn=conn,
        )
        evo_signals = [s for s in signals if s["type"] == "card_evolution_unlocked"]
        assert len(evo_signals) == 1
        assert evo_signals[0]["evolution_kind"] == "hero"
        assert evo_signals[0]["new_evolution_level"] == 2
    finally:
        conn.close()


def test_snapshot_player_profile_emits_path_of_legend_promotion_signal():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        first = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 7, "trophies": 1600, "rank": None},
            },
            conn=conn,
        )
        second = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1800, "rank": None},
            },
            conn=conn,
        )
    finally:
        conn.close()

    assert first == []
    # No ranked battles ingested before the promotion → recent_opponents_summary
    # gracefully falls back to None instead of a half-populated dict.
    assert second == [{
        "type": "path_of_legend_promotion",
        "tag": "#ABC123",
        "name": "King Levy",
        "old_league_number": 7,
        "new_league_number": 8,
        "trophies": 1800,
        "rank": None,
        "recent_opponents_summary": None,
    }]


def test_snapshot_player_profile_emits_best_trophies_peak_signal():
    """v4.7 #28: new personal-best trophies record fires a signal when the PB
    crosses a 100-trophy boundary."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8460, "trophies": 8460},
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8520, "trophies": 8520},
            conn=conn,
        )
    finally:
        conn.close()

    peaks = [s for s in signals if s["type"] == "best_trophies_peak"]
    assert len(peaks) == 1
    assert peaks[0]["old_best"] == 8460
    assert peaks[0]["new_best"] == 8520


def test_snapshot_player_profile_skips_peak_below_100_trophy_boundary():
    """Routine +30/+60 PB ticks that stay inside the same 100-trophy bucket
    must not fire — the channel is for durable milestones."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8410, "trophies": 8410},
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8475, "trophies": 8475},
            conn=conn,
        )
    finally:
        conn.close()
    assert not any(s["type"] == "best_trophies_peak" for s in signals)


def test_snapshot_player_profile_does_not_emit_peak_when_best_flat():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8400, "trophies": 7900},
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "bestTrophies": 8400, "trophies": 7950},
            conn=conn,
        )
    finally:
        conn.close()
    assert not any(s["type"] == "best_trophies_peak" for s in signals)


def test_snapshot_player_profile_emits_challenge_milestone_signal():
    """v4.7 #30: crossing a challenge-wins milestone (10, 11, ..., 20) fires."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "challengeMaxWins": 9},
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "challengeMaxWins": 12},
            conn=conn,
        )
    finally:
        conn.close()
    milestones = sorted(s["milestone"] for s in signals if s["type"] == "challenge_performance_milestone")
    assert milestones == [10, 11, 12]


def test_snapshot_player_profile_emits_path_of_legend_demotion_signal():
    """v4.7 #23: parity with promotion — demotions fire too."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1600, "rank": None},
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 7, "trophies": 1400, "rank": None},
            },
            conn=conn,
        )
    finally:
        conn.close()

    demotions = [s for s in signals if s["type"] == "path_of_legend_demotion"]
    assert len(demotions) == 1
    assert demotions[0]["old_league_number"] == 8
    assert demotions[0]["new_league_number"] == 7


def test_snapshot_player_profile_emits_ultimate_champion_reached_signal():
    """v4.7 #24: league 10 (UC) is a distinct season-long milestone."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 9, "trophies": 2400, "rank": None},
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 50, "rank": None},
            },
            conn=conn,
        )
    finally:
        conn.close()

    types = [s["type"] for s in signals]
    assert "path_of_legend_promotion" in types
    assert "ultimate_champion_reached" in types


def test_snapshot_player_profile_does_not_emit_uc_for_8_to_9():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1600, "rank": None},
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 9, "trophies": 400, "rank": None},
            },
            conn=conn,
        )
    finally:
        conn.close()
    assert "ultimate_champion_reached" not in [s["type"] for s in signals]


def test_snapshot_player_profile_emits_global_rank_attained_signal():
    """v4.7 #25: breaking into top-1000 global rank is newsworthy."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 2400, "rank": None},
            },
            conn=conn,
        )
        first_break = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 2600, "rank": 742},
            },
            conn=conn,
        )
        improve = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 2800, "rank": 500},
            },
            conn=conn,
        )
        regress = db.snapshot_player_profile(
            {
                "tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 10, "trophies": 2700, "rank": 800},
            },
            conn=conn,
        )
    finally:
        conn.close()

    assert any(s["type"] == "path_of_legend_global_rank_attained" for s in first_break)
    assert any(s["type"] == "path_of_legend_global_rank_attained" for s in improve)
    # Regression (rank got worse) — no signal
    assert not any(s["type"] == "path_of_legend_global_rank_attained" for s in regress)


def test_snapshot_player_profile_treats_first_load_as_baseline_discovery():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )

        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 66,
                "wins": 1005,
                "currentDeck": [],
                "cards": [
                    {"name": "Archers", "level": 12, "maxLevel": 16, "rarity": "common"},
                    {"name": "Little Prince", "level": 1, "maxLevel": 6, "rarity": "champion"},
                ],
                "badges": [
                    {"name": "MasteryKnight", "level": 1, "maxLevel": 10, "progress": 10, "target": 25},
                    {"name": "Classic12Wins", "level": 1, "maxLevel": 8, "progress": 1, "target": 10},
                ],
                "achievements": [
                    {"name": "Friend in Need", "stars": 2, "value": 520, "target": 500, "info": "Donate 500 cards", "completionInfo": None},
                ],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1800, "rank": 5000},
            },
            conn=conn,
        )

        snapshot_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM player_profile_snapshots WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')"
        ).fetchone()["cnt"]
    finally:
        conn.close()

    assert signals == []
    assert snapshot_count == 1


def _ladder_battle(ts: str, trophies_before: int, trophy_change: int = 30, win: bool = True):
    return {
        "type": "PvP",
        "battleTime": ts,
        "gameMode": {"id": 72000006, "name": "Ladder"},
        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3 if win else 0,
                  "trophyChange": trophy_change if win else -trophy_change,
                  "startingTrophies": trophies_before, "cards": [], "supportCards": []}],
        "opponent": [{"tag": f"#OPP_{ts}", "name": "Opp", "crowns": 1 if win else 3,
                      "cards": [], "supportCards": []}],
    }


def _ranked_battle(ts: str, trophies_before: int, trophy_change: int = 40, win: bool = True, league: int = 7):
    return {
        "type": "pathOfLegend",
        "battleTime": ts,
        "gameMode": {"id": 72000464, "name": "Ranked1v1_NewArena2"},
        "leagueNumber": league,
        "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2 if win else 0,
                  "trophyChange": trophy_change if win else -trophy_change,
                  "startingTrophies": trophies_before, "cards": [], "supportCards": []}],
        "opponent": [{"tag": f"#OPP_{ts}", "name": "Opp", "crowns": 0 if win else 3,
                      "cards": [], "supportCards": []}],
    }


_PLAYER_DECK_HOG = [
    {"name": "Hog Rider"}, {"name": "Fireball"}, {"name": "Musketeer"},
    {"name": "Ice Spirit"}, {"name": "Skeletons"}, {"name": "Cannon"},
    {"name": "Ice Golem"}, {"name": "Log"},
]


def _ladder_battle_rich(ts, trophies_before, opp_trophies, opp_name="Opp", trophy_change=30, win=True):
    """Fixture with player deck (Hog 2.6) and opponent startingTrophies — for
    enrichment tests that need real opponent trophy + deck data."""
    return {
        "type": "PvP",
        "battleTime": ts,
        "gameMode": {"id": 72000006, "name": "Ladder"},
        "team": [{"tag": "#ABC123", "name": "King Levy",
                  "crowns": 3 if win else 0,
                  "trophyChange": trophy_change if win else -trophy_change,
                  "startingTrophies": trophies_before,
                  "cards": _PLAYER_DECK_HOG,
                  "supportCards": []}],
        "opponent": [{"tag": f"#OPP_{ts}", "name": opp_name,
                      "crowns": 1 if win else 3,
                      "startingTrophies": opp_trophies,
                      "cards": [{"name": "Giant"}, {"name": "Wizard"}, {"name": "Mini P.E.K.K.A"},
                                {"name": "Skeleton Army"}, {"name": "Goblins"}, {"name": "Inferno Tower"},
                                {"name": "Arrows"}, {"name": "Zap"}],
                      "supportCards": []}],
    }


def test_battle_hot_streak_carries_rich_recent_opponents_summary():
    """The signal must include opponent trophy aggregates, notable opponents,
    player avg-elixir, and win-condition cards — so the awareness loop can
    narrate without burning a cr_api(player_battles) call."""
    conn = db.get_connection(":memory:")
    try:
        # Seed card_catalog so avg_elixir_cost is computable for the Hog deck.
        for name, cost in [
            ("Hog Rider", 4), ("Fireball", 4), ("Musketeer", 4), ("Ice Spirit", 1),
            ("Skeletons", 1), ("Cannon", 3), ("Ice Golem", 2), ("Log", 2),
        ]:
            conn.execute(
                "INSERT INTO card_catalog (card_id, name, elixir_cost, rarity, max_level, "
                "card_type, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hash(name) % 1_000_000_000, name, cost, "common", 16, "troop", "2026-01-01T00:00:00"),
            )
        conn.commit()
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        # Seed prior state so the streak signal can detect a transition.
        db.snapshot_player_battlelog(
            "#ABC123",
            [_ladder_battle_rich("20260307T080000.000Z", 4970, 7100)],
            conn=conn,
        )
        # Five wins, three of them against 7K+ opponents.
        pulse = db.snapshot_player_battlelog(
            "#ABC123",
            [
                _ladder_battle_rich("20260307T130000.000Z", 5060, 7300),
                _ladder_battle_rich("20260307T120000.000Z", 5030, 7150, opp_name="Whale"),
                _ladder_battle_rich("20260307T110000.000Z", 5000, 6800),
                _ladder_battle_rich("20260307T100000.000Z", 5000, 7050),
                _ladder_battle_rich("20260307T090000.000Z", 4980, 5900),
            ],
            conn=conn,
        )
    finally:
        conn.close()

    hot = next(p for p in pulse if p["type"] == "battle_hot_streak")
    summary = hot["recent_opponents_summary"]
    assert summary is not None
    assert summary["wins"] >= 4
    assert summary["losses"] == 0
    # Trophy aggregates present because ladder battles carry startingTrophies.
    assert "avg_trophies" in summary["opponents"]
    assert summary["opponents"]["high_trophy_count"] >= 3
    # Notable opponents capped at 3 and sorted by trophies desc.
    notable = summary["opponents"]["notable_opponents"]
    assert len(notable) == 3
    trophies_seen = [n["trophies"] for n in notable]
    assert trophies_seen == sorted(trophies_seen, reverse=True)
    # Player deck enrichment.
    assert summary["player_deck"]["avg_elixir_cost"] == 2.62  # (4+4+4+1+1+3+2+2)/8
    assert summary["player_deck"]["win_condition_cards"] == ["Hog Rider"]


def test_path_of_legend_promotion_recent_opponents_summary_omits_trophy_aggregates():
    """Path of Legends opponents have no startingTrophies in the API payload,
    so promotion signals get notable_opponents (deck only) but no trophy
    aggregates. Other fields populate normally."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        # Initial league state.
        db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "currentPathOfLegendSeasonResult": {"leagueNumber": 7, "trophies": 1600, "rank": None}},
            conn=conn,
        )
        # Ingest 5 ranked battles (no opponent trophies in PoL).
        db.snapshot_player_battlelog(
            "#ABC123",
            [_ranked_battle(f"20260307T1{h}0000.000Z", 1500 + h * 20)
             for h in range(5)],
            conn=conn,
        )
        # Trigger promotion via second profile snapshot.
        signals = db.snapshot_player_profile(
            {"tag": "#ABC123", "name": "King Levy", "currentDeck": [], "cards": [],
             "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1800, "rank": None}},
            conn=conn,
        )
    finally:
        conn.close()

    promo = next(s for s in signals if s["type"] == "path_of_legend_promotion")
    summary = promo["recent_opponents_summary"]
    assert summary is not None
    assert summary["battles_considered"] == 5
    # Trophy aggregates absent for PoL.
    assert "avg_trophies" not in summary["opponents"]
    assert "median_trophies" not in summary["opponents"]
    assert "high_trophy_count" not in summary["opponents"]
    # notable_opponents present but trophies field omitted per entry.
    assert summary["opponents"]["notable_opponents"]
    assert all("trophies" not in entry for entry in summary["opponents"]["notable_opponents"])


def test_recent_opponents_summary_returns_none_when_no_battles_in_mode():
    """Graceful degradation: if there are no battles in the requested mode,
    the helper returns None and signal callers can omit/null the field."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy"}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?", ("#ABC123",),
        ).fetchone()["member_id"]
        from storage.player import _compute_recent_opponents_summary
        result = _compute_recent_opponents_summary(member_id, "is_ranked = 1", conn)
        assert result is None
    finally:
        conn.close()


def test_filter_win_condition_cards_picks_known_win_cons():
    """Win-condition extraction is case-insensitive and only returns cards
    on the canonical list — no inventing."""
    from cr_knowledge import filter_win_condition_cards
    deck = ["hog rider", "Knight", "Fireball", "GIANT", "Random Card"]
    assert filter_win_condition_cards(deck) == ["Hog Rider", "Giant"]
    assert filter_win_condition_cards([]) == []
    assert filter_win_condition_cards(None) == []


def test_snapshot_player_battlelog_emits_ladder_mode_battle_pulse():
    """v4.7 #22: ladder (Trophy Road) streaks fire with mode='ladder'."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        initial = db.snapshot_player_battlelog(
            "#ABC123",
            [
                _ladder_battle("20260307T090000.000Z", 5000),
                _ladder_battle("20260307T080000.000Z", 4970),
            ],
            conn=conn,
        )
        pulse = db.snapshot_player_battlelog(
            "#ABC123",
            [
                _ladder_battle("20260307T120000.000Z", 5100, trophy_change=40),
                _ladder_battle("20260307T110000.000Z", 5060, trophy_change=40),
                _ladder_battle("20260307T100000.000Z", 5030, trophy_change=30),
            ],
            conn=conn,
        )
    finally:
        conn.close()

    assert initial == []
    types = [(item["type"], item["mode"]) for item in pulse]
    assert ("battle_hot_streak", "ladder") in types
    assert ("battle_trophy_push", "ladder") in types
    # No ranked signals — only ladder battles in this fixture
    assert not any(m == "ranked" for _, m in types)

    hot = next(p for p in pulse if p["type"] == "battle_hot_streak")
    assert hot["streak"] == 5
    assert hot["new_battle_count"] == 3
    push = next(p for p in pulse if p["type"] == "battle_trophy_push")
    assert push["trophy_delta"] == 110
    assert push["from_trophies"] == 5030
    assert push["to_trophies"] == 5140


def test_snapshot_player_battlelog_emits_ranked_mode_battle_pulse():
    """v4.7 #22: ranked (Path of Legends) streaks fire with mode='ranked'
    distinctly from ladder — lets the agent post them with the right voice."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        initial = db.snapshot_player_battlelog(
            "#ABC123",
            [
                _ranked_battle("20260307T090000.000Z", 1500),
                _ranked_battle("20260307T080000.000Z", 1460),
            ],
            conn=conn,
        )
        pulse = db.snapshot_player_battlelog(
            "#ABC123",
            [
                _ranked_battle("20260307T120000.000Z", 1620),
                _ranked_battle("20260307T110000.000Z", 1580),
                _ranked_battle("20260307T100000.000Z", 1540),
            ],
            conn=conn,
        )
    finally:
        conn.close()

    assert initial == []
    types = [(item["type"], item["mode"]) for item in pulse]
    assert ("battle_hot_streak", "ranked") in types
    assert ("battle_trophy_push", "ranked") in types
    assert not any(m == "ladder" for _, m in types)

    push = next(p for p in pulse if p["type"] == "battle_trophy_push")
    assert push["trophy_delta"] == 120
    assert push["from_trophies"] == 1540
    assert push["to_trophies"] == 1660


def test_weekly_digest_summary_collects_war_progression_and_hot_streaks():
    conn = db.get_connection(":memory:")
    try:
        now = datetime.now(timezone.utc)
        race_created = now.strftime("%Y%m%dT120000.000Z")
        earlier_profile = (now - timedelta(days=5)).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")

        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member", "expLevel": 60, "trophies": 7000, "bestTrophies": 7100, "clanRank": 1, "donations": 200}],
            conn=conn,
        )
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 60,
                "wins": 100,
                "trophies": 7000,
                "bestTrophies": 7100,
                "currentFavouriteCard": {"name": "Hog Rider"},
                "currentDeck": [],
                "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 7, "trophies": 1600, "rank": 9000},
            },
            conn=conn,
        )
        conn.execute("UPDATE player_profile_snapshots SET fetched_at = ? WHERE snapshot_id = 1", (earlier_profile,))
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 61,
                "wins": 120,
                "trophies": 7090,
                "bestTrophies": 7220,
                "currentFavouriteCard": {"name": "Hog Rider"},
                "currentDeck": [],
                "cards": [],
                "currentPathOfLegendSeasonResult": {"leagueNumber": 8, "trophies": 1800, "rank": 5000},
            },
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "PvP",
                    "battleTime": "20260307T120000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "trophyChange": 30, "startingTrophies": 7000, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "PvP",
                    "battleTime": "20260307T110000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 30, "startingTrophies": 6970, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "pathOfLegend",
                    "battleTime": "20260307T100000.000Z",
                    "gameMode": {"id": 72000464, "name": "Ranked1v1_NewArena2"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 1, "trophyChange": 30, "startingTrophies": 6940, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP3", "name": "Opp 3", "crowns": 0, "cards": [], "supportCards": []}],
                },
                {
                    "type": "PvP",
                    "battleTime": "20260307T090000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 30, "startingTrophies": 6910, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP4", "name": "Opp 4", "crowns": 1, "cards": [], "supportCards": []}],
                },
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 130,
                        "sectionIndex": 1,
                        "createdDate": race_created,
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 20,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12000,
                                    "finishTime": race_created,
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            },
                            {
                                "rank": 2,
                                "trophyChange": -10,
                                "clan": {"tag": "#RIVAL", "name": "Rivals", "fame": 11300, "finishTime": race_created, "participants": []},
                            },
                        ],
                    }
                ],
            },
            "#J2RGCRVG",
            conn=conn,
        )

        summary = db.get_weekly_digest_summary(conn=conn)
    finally:
        conn.close()

    assert summary["recent_war_races"][0]["our_rank"] == 1
    assert summary["recent_war_races"][0]["top_participants"][0]["name"] == "King Levy"
    assert summary["progression_highlights"][0]["level_gain"] == 1
    assert summary["progression_highlights"][0]["pol_league_gain"] == 1
    assert summary["hot_streaks"][0]["current_streak"] == 4


def test_snapshot_player_battlelog_uses_api_outcome_priority_and_normalizes_extra_metadata():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "boatBattle",
                    "battleTime": "20260307T110000.000Z",
                    "gameMode": {"id": 72000266, "name": "ClanWar_BoatBattle"},
                    "deckSelection": "collection",
                    "arena": {"id": 54000046, "name": "Legendary Arena"},
                    "eventTag": "boat-weekend",
                    "isHostedMatch": False,
                    "leagueNumber": 1,
                    "boatBattleSide": "defender",
                    "boatBattleWon": True,
                    "newTowersDestroyed": 0,
                    "prevTowersDestroyed": 1,
                    "remainingTowers": 2,
                    "modifiers": [{"tag": "#ABC123", "modifiers": ["Rage1"]}],
                    "team": [{
                        "tag": "#ABC123",
                        "name": "King Levy",
                        "crowns": 0,
                        "cards": [],
                        "supportCards": [],
                    }],
                    "opponent": [{
                        "tag": "#DEF456",
                        "name": "Opponent",
                        "crowns": 3,
                        "cards": [],
                        "supportCards": [],
                    }],
                },
                {
                    "type": "riverRaceDuel",
                    "battleTime": "20260307T120000.000Z",
                    "gameMode": {"id": 72000267, "name": "CW_Duel_1v1"},
                    "deckSelection": "warDeckPick",
                    "arena": {"id": 1, "name": "Arena 1"},
                    "team": [{
                        "tag": "#ABC123",
                        "name": "King Levy",
                        "crowns": 2,
                        "cards": [],
                        "supportCards": [],
                        "rounds": [{"crowns": 1, "cards": [{"name": "Knight", "used": True}]}],
                    }],
                    "opponent": [{
                        "tag": "#XYZ789",
                        "name": "Opponent 2",
                        "crowns": 1,
                        "cards": [],
                        "supportCards": [],
                        "rounds": [{"crowns": 0, "cards": [{"name": "Knight", "used": False}]}],
                    }],
                },
            ],
            conn=conn,
        )

        boat = conn.execute(
            "SELECT outcome, event_tag, league_number, is_hosted_match, modifiers_json, boat_battle_side, "
            "boat_battle_won, new_towers_destroyed, prev_towers_destroyed, remaining_towers "
            "FROM member_battle_facts WHERE battle_type = 'boatBattle'"
        ).fetchone()
        assert boat["outcome"] == "W"
        assert boat["event_tag"] == "boat-weekend"
        assert boat["league_number"] == 1
        assert boat["is_hosted_match"] == 0
        assert json.loads(boat["modifiers_json"])[0]["modifiers"] == ["Rage1"]
        assert boat["boat_battle_side"] == "defender"
        assert boat["boat_battle_won"] == 1
        assert boat["new_towers_destroyed"] == 0
        assert boat["prev_towers_destroyed"] == 1
        assert boat["remaining_towers"] == 2

        duel = conn.execute(
            "SELECT outcome, team_rounds_json, opponent_rounds_json "
            "FROM member_battle_facts WHERE battle_type = 'riverRaceDuel'"
        ).fetchone()
        assert duel["outcome"] == "W"
        assert json.loads(duel["team_rounds_json"])[0]["cards"][0]["used"] is True
        assert json.loads(duel["opponent_rounds_json"])[0]["cards"][0]["used"] is False
    finally:
        conn.close()


def test_snapshot_player_profile_detects_level_wins_new_cards_and_card_upgrades():
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
                "expLevel": 64,
                "wins": 480,
                "currentDeck": [],
                "cards": [
                    {"name": "Fireball", "level": 10, "maxLevel": 11, "rarity": "epic"},
                    {"name": "Knight", "level": 14, "maxLevel": 16, "rarity": "common"},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 65,
                "wins": 1005,
                "currentDeck": [],
                "cards": [
                    {"name": "Fireball", "level": 11, "maxLevel": 11, "rarity": "epic"},
                    {"name": "Knight", "level": 15, "maxLevel": 16, "rarity": "common"},
                    {"name": "Archers", "level": 12, "maxLevel": 16, "rarity": "common"},
                    {"name": "Goblin Barrel", "level": 6, "maxLevel": 11, "rarity": "epic"},
                    {"name": "Little Prince", "level": 1, "maxLevel": 6, "rarity": "champion"},
                ],
            },
            conn=conn,
        )

        assert any(sig["type"] == "player_level_up" and sig["new_level"] == 65 for sig in signals)
        assert any(sig["type"] == "career_wins_milestone" and sig["milestone"] == 500 for sig in signals)
        assert any(sig["type"] == "career_wins_milestone" and sig["milestone"] == 1000 for sig in signals)
        assert not any(sig["type"] == "new_card_unlocked" and sig["card_name"] == "Archers" for sig in signals), "common unlocks should be suppressed"
        assert not any(sig["type"] == "new_card_unlocked" and sig["card_name"] == "Goblin Barrel" for sig in signals), "epic unlocks should be suppressed (legendary + champion only)"
        assert any(sig["type"] == "new_card_unlocked" and sig["card_name"] == "Little Prince" for sig in signals)
        assert any(sig["type"] == "new_champion_unlocked" and sig["card_name"] == "Little Prince" for sig in signals)
        assert any(sig["type"] == "new_card_unlocked" and sig["card_name"] == "Little Prince" and sig["is_champion"] is True for sig in signals)
        assert any(sig["type"] == "card_level_milestone" and sig["card_name"] == "Fireball" and sig["milestone"] == 16 for sig in signals)
        assert not any(sig["type"] == "card_level_milestone" and sig["card_name"] == "Knight" for sig in signals), "Knight at display level 15 is below max=16; should not fire"
    finally:
        conn.close()


def test_snapshot_player_profile_ignores_lower_level_card_upgrade_signals():
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
                "wins": 480,
                "currentDeck": [],
                "cards": [
                    {"name": "Knight", "level": 10, "maxLevel": 16, "rarity": "common"},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "expLevel": 65,
                "wins": 480,
                "currentDeck": [],
                "cards": [
                    {"name": "Knight", "level": 11, "maxLevel": 16, "rarity": "common"},
                ],
            },
            conn=conn,
        )

        assert not any(
            sig["type"] == "card_level_milestone" and sig["card_name"] == "Knight"
            for sig in signals
        )
    finally:
        conn.close()


def test_snapshot_player_profile_detects_badge_and_achievement_milestones():
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
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "MasteryKnight", "level": 4, "maxLevel": 10, "progress": 10, "target": 25},
                    {"name": "CrlSpectator2022", "progress": 1},
                ],
                "achievements": [
                    {"name": "Friend in Need", "stars": 1, "value": 240, "target": 250, "info": "Donate 250 cards", "completionInfo": None},
                    {"name": "Team Player", "stars": 0, "value": 0, "target": 1, "info": "Join a Clan", "completionInfo": None},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "MasteryKnight", "level": 5, "maxLevel": 10, "progress": 30, "target": 50},
                    {"name": "CrlSpectator2022", "progress": 1},
                    {"name": "Classic12Wins", "level": 1, "maxLevel": 8, "progress": 1, "target": 10},
                ],
                "achievements": [
                    {"name": "Friend in Need", "stars": 2, "value": 520, "target": 500, "info": "Donate 500 cards", "completionInfo": None},
                    {"name": "Team Player", "stars": 1, "value": 1, "target": 1, "info": "Join a Clan", "completionInfo": "Clan joined"},
                ],
            },
            conn=conn,
        )

        assert any(
            sig["type"] == "badge_level_milestone"
            and sig["badge_name"] == "MasteryKnight"
            and sig["badge_card_name"] == "Knight"
            and sig["old_level"] == 4
            and sig["new_level"] == 5
            for sig in signals
        )
        assert any(
            sig["type"] == "badge_earned"
            and sig["badge_name"] == "Classic12Wins"
            and sig["badge_category"] == "challenge"
            and sig["badge_label"] == "Classic Challenge 12 Wins"
            and sig["is_one_time"] is False
            for sig in signals
        )
        assert any(
            sig["type"] == "achievement_star_milestone"
            and sig["achievement_name"] == "Friend in Need"
            and sig["old_stars"] == 1
            and sig["new_stars"] == 2
            and sig["completed"] is False
            for sig in signals
        )
        assert any(
            sig["type"] == "achievement_star_milestone"
            and sig["achievement_name"] == "Team Player"
            and sig["old_stars"] == 0
            and sig["new_stars"] == 1
            and sig["achievement_info"] == "Join a Clan"
            for sig in signals
        )
    finally:
        conn.close()


def test_snapshot_player_profile_ties_anarchy_badge_to_current_event():
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
                "currentDeck": [],
                "cards": [],
                "badges": [],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "AnarchyLeagueCompletion", "level": 1, "maxLevel": 10, "progress": 1, "target": 2},
                ],
            },
            conn=conn,
        )
    finally:
        conn.close()

    event_badge = next(sig for sig in signals if sig["type"] == "badge_earned")
    assert event_badge["badge_name"] == "AnarchyLeagueCompletion"
    assert event_badge["badge_category"] == "event"
    assert event_badge["badge_label"] == "Anarchy League Completion"
    assert event_badge["event_name"] == "Anarchy League"
    assert "event_game_mode_name" not in event_badge


def test_snapshot_player_profile_ignores_badge_progress_without_tier_change():
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
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "MasteryKnight", "level": 1, "maxLevel": 10, "progress": 10, "target": 25},
                ],
                "achievements": [
                    {"name": "Friend in Need", "stars": 1, "value": 240, "target": 250, "info": "Donate 250 cards", "completionInfo": None},
                ],
            },
            conn=conn,
        )
        signals = db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "currentDeck": [],
                "cards": [],
                "badges": [
                    {"name": "MasteryKnight", "level": 1, "maxLevel": 10, "progress": 24, "target": 25},
                ],
                "achievements": [
                    {"name": "Friend in Need", "stars": 1, "value": 249, "target": 250, "info": "Donate 250 cards", "completionInfo": None},
                ],
            },
            conn=conn,
        )

        assert signals == []
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


def test_detect_milestones_skips_already_logged_arena_change():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{
                "tag": "#ABC123",
                "name": "King Levy",
                "role": "member",
                "arena": {"name": "Legendary Arena"},
            }],
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
            [{
                "tag": "#ABC123",
                "name": "King Levy",
                "role": "member",
                "arena": {"name": "Lumberlove Cabin"},
            }],
            conn=conn,
        )

        first = db.detect_milestones(conn=conn)
        assert len(first) == 1
        signal_log_type = first[0]["signal_log_type"]

        db.mark_signal_sent(signal_log_type, "2026-03-01", conn=conn)
        second = db.detect_milestones(conn=conn)
        assert second == []
    finally:
        conn.close()


def test_record_awareness_tick_persists_row_and_signal_outcomes_json():
    import json
    conn = db.get_connection(":memory:")
    try:
        tick_id = db.record_awareness_tick(
            workflow="clan_awareness",
            signals_in=3,
            posts_delivered=1,
            posts_rejected=0,
            covered_keys=1,
            considered_skipped=2,
            hard_fallback=0,
            hard_fallback_failed=0,
            all_ok=True,
            skipped_reason=None,
            signal_outcomes=[
                {"signal_key": "join:#A", "signal_type": "member_join", "status": "covered"},
                {"signal_key": "arena:#B", "signal_type": "arena_change", "status": "skipped"},
                {"signal_key": "streak:#C", "signal_type": "battle_hot_streak", "status": "skipped"},
            ],
            conn=conn,
        )
        assert tick_id > 0
        row = conn.execute(
            "SELECT workflow, signals_in, posts_delivered, covered_keys, considered_skipped, all_ok, signal_outcomes_json FROM awareness_ticks WHERE tick_id = ?",
            (tick_id,),
        ).fetchone()
        assert row["workflow"] == "clan_awareness"
        assert row["signals_in"] == 3
        assert row["posts_delivered"] == 1
        assert row["covered_keys"] == 1
        assert row["considered_skipped"] == 2
        assert row["all_ok"] == 1
        outcomes = json.loads(row["signal_outcomes_json"])
        assert {o["status"] for o in outcomes} == {"covered", "skipped"}
    finally:
        conn.close()


def test_detect_role_changes_skips_already_logged_role_change():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
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
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
            conn=conn,
        )

        first = db.detect_role_changes(conn=conn)
        assert len(first) == 1
        signal_log_type = first[0]["signal_log_type"]

        db.mark_signal_sent(signal_log_type, "2026-03-01", conn=conn)
        second = db.detect_role_changes(conn=conn)
        assert second == []
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
        # Tag exposure: LLM needs the player_tag to chain into cr_api.
        assert member_war["player_tag"] == "#ABC123"
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


def test_current_war_status_supports_absolute_training_period_index_and_period_logs():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 0,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 12850,
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
                "sectionIndex": 1,
                "periodIndex": 7,
                "periodType": "training",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
                "clans": [
                    {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "repairPoints": 0, "periodPoints": 0},
                ],
                "periodLogs": [
                    {
                        "periodIndex": 6,
                        "items": [
                            {
                                "clan": {"tag": "#J2RGCRVG"},
                                "pointsEarned": 4200,
                                "progressStartOfDay": 3311,
                                "progressEndOfDay": 6622,
                                "endOfDayRank": 0,
                                "progressEarned": 3000,
                                "numOfDefensesRemaining": 7,
                                "progressEarnedFromDefenses": 311,
                            }
                        ],
                    }
                ],
            },
            conn=conn,
        )

        war = db.get_current_war_status(conn=conn)
        defense = db.get_latest_clan_boat_defense_status(conn=conn)

        assert war["season_id"] == 129
        assert war["section_index"] == 1
        assert war["week"] == 2
        assert war["period_type"] == "training"
        assert war["period_index"] == 7
        assert war["period_offset"] == 0
        assert war["phase"] == "practice"
        assert war["practice_day_number"] == 1
        assert war["phase_display"] == "Practice Day 1"

        assert defense["season_id"] == 129
        assert defense["section_index"] == 0
        assert defense["week"] == 1
        assert defense["period_index"] == 6
        assert defense["period_offset"] == 6
        assert defense["phase"] == "battle"
        assert defense["battle_day_number"] == 4
        assert defense["phase_display"] == "Battle Day 4"
        assert defense["num_defenses_remaining"] == 7
        assert defense["progress_earned_from_defenses"] == 311
        assert defense["current_week_match"] is False
    finally:
        conn.close()


def _seed_active_war(
    conn,
    *,
    section_index=1,
    period_index=10,
    period_type="warDay",
    our_fame=0,
    our_period_points=0,
    rival_fame=3950,
    rival_period_points=0,
):
    """Seed a minimal current-war-state row so build_war_now_context() has data."""
    db.store_war_log(
        {
            "items": [
                {
                    "seasonId": 130,
                    "sectionIndex": 0,
                    "createdDate": "20260301T120000.000Z",
                    "standings": [
                        {
                            "rank": 1,
                            "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10000},
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
            "sectionIndex": section_index,
            "periodIndex": period_index,
            "periodType": period_type,
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": our_fame,
                "repairPoints": 0,
                "periodPoints": our_period_points,
                "clanScore": 140,
                "participants": [],
            },
            "clans": [
                {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": our_fame,
                    "repairPoints": 0,
                    "periodPoints": our_period_points,
                },
                {
                    "tag": "#OTHER1",
                    "name": "Dragon Riders",
                    "fame": rival_fame,
                    "repairPoints": 0,
                    "periodPoints": rival_period_points,
                },
            ],
            "periodLogs": [],
        },
        conn=conn,
    )


def test_build_war_now_context_returns_none_when_no_war():
    conn = db.get_connection(":memory:")
    try:
        data, text = db.build_war_now_context(conn=conn)
        assert data is None
        assert text == ""
    finally:
        conn.close()


def test_build_war_now_context_battle_day_regular_week():
    conn = db.get_connection(":memory:")
    try:
        _seed_active_war(conn, section_index=1, period_index=11, period_type="warDay")
        data, text = db.build_war_now_context(conn=conn)
        assert data is not None
        assert data["phase"] == "battle"
        assert data["phase_display"] == "Battle Day 2"
        assert data["day_number"] == 2
        # day_total intentionally omitted from data — see build_war_now_context.
        assert "day_total" not in data
        assert data["is_colosseum_week"] is False
        assert "RIVER RACE — CURRENT MOMENT" in text
        assert "Battle Day 2 of 4" in text
        # Precomputed "days left" phrasing — on day 2 of 4, 2 battle days remain
        # after today. Never "4 more days" (which was observed 2026-04-24 in
        # #river-race when the LLM miscounted `day_total`).
        assert "today + 2 more battle days" in text
        assert "Colosseum" not in text
    finally:
        conn.close()


def test_build_war_now_context_uses_period_points_when_fame_zero():
    conn = db.get_connection(":memory:")
    try:
        _seed_active_war(
            conn,
            section_index=1,
            period_index=10,
            period_type="warDay",
            our_fame=0,
            our_period_points=9125,
            rival_fame=0,
            rival_period_points=0,
        )
        data, text = db.build_war_now_context(conn=conn)

        assert data is not None
        ours = next(clan for clan in data["race_standings"] if clan["is_us"])
        assert ours["fame"] == 0
        assert ours["period_points"] == 9125
        assert ours["active_score"] == 9125
        assert ours["score_source"] == "period_points"
        assert ours["score_label"] == "period points"
        assert "9,125 period points" in text
        assert "0 fame" not in text
    finally:
        conn.close()


def test_build_war_now_context_final_battle_day_omits_after_today_parenthetical():
    conn = db.get_connection(":memory:")
    try:
        _seed_active_war(conn, section_index=1, period_index=13, period_type="warDay")
        data, text = db.build_war_now_context(conn=conn)
        assert data["phase"] == "battle"
        assert data["day_number"] == 4
        assert data["is_final_battle_day"] is True
        # On the final battle day there is no "today + N more" parenthetical —
        # the existing "Final battle day" marker already covers it.
        assert "today +" not in text
        assert "Final battle day" in text
    finally:
        conn.close()


def test_build_war_now_context_practice_day():
    conn = db.get_connection(":memory:")
    try:
        _seed_active_war(conn, section_index=1, period_index=8, period_type="training")
        data, text = db.build_war_now_context(conn=conn)
        assert data["phase"] == "practice"
        assert data["phase_display"] == "Practice Day 2"
        assert "day_total" not in data
        assert "Practice Day 2 of 3" in text
        # On practice day 2 of 3, one practice day remains after today.
        assert "today + 1 more practice day" in text
        assert "Colosseum" not in text
    finally:
        conn.close()


def test_build_war_now_context_confirms_colosseum_on_live_period_type():
    conn = db.get_connection(":memory:")
    try:
        _seed_active_war(conn, section_index=4, period_index=31, period_type="colosseum")
        data, text = db.build_war_now_context(conn=conn)
        assert data["is_colosseum_week"] is True
        assert "Colosseum (final week, 100 trophy stakes)" in text
    finally:
        conn.close()


def test_build_war_now_context_confirms_colosseum_from_logged_trophy_stakes():
    """During colosseum-week practice days periodType is 'training'; we still
    confirm Colosseum when the /riverracelog entry for this section shows
    trophy_change == ±100."""
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 130,
                        "sectionIndex": 4,
                        "createdDate": "20260401T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10000},
                                "trophyChange": 100,
                            },
                            {
                                "rank": 2,
                                "clan": {"tag": "#OTHER1", "name": "Dragon Riders", "fame": 9500},
                                "trophyChange": 40,
                            },
                            {
                                "rank": 3,
                                "clan": {"tag": "#OTHER2", "name": "Flame", "fame": 9000},
                                "trophyChange": -20,
                            },
                            {
                                "rank": 4,
                                "clan": {"tag": "#OTHER3", "name": "Rivals", "fame": 8500},
                                "trophyChange": -60,
                            },
                            {
                                "rank": 5,
                                "clan": {"tag": "#OTHER4", "name": "Bottom", "fame": 8000},
                                "trophyChange": -100,
                            },
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
                "sectionIndex": 4,
                "periodIndex": 30,
                "periodType": "training",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
                "clans": [
                    {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0},
                ],
                "periodLogs": [],
            },
            conn=conn,
        )

        data, text = db.build_war_now_context(conn=conn)
        assert data["period_type"] == "training"
        assert data["phase"] == "practice"
        assert data["is_colosseum_week"] is True
        assert "Colosseum (final week, 100 trophy stakes)" in text
    finally:
        conn.close()


def test_fresh_time_left_seconds_uses_now_anchor():
    from datetime import datetime, timezone
    from storage.war_status import _fresh_time_left_seconds

    state = {"period_ends_at": "2026-04-21T10:00:00Z"}
    frozen_now = datetime(2026, 4, 21, 7, 0, tzinfo=timezone.utc)
    assert _fresh_time_left_seconds(state, now=frozen_now) == 3 * 60 * 60


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


def test_hot_streak_and_level_16_card_queries():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )
        levy_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#ABC123'"
        ).fetchone()["member_id"]
        vijay_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#DEF456'"
        ).fetchone()["member_id"]
        conn.execute(
            "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
            "VALUES (?, ?, 'ladder_ranked_10', 10, 8, 2, 0, 6, 'W', 0.8, 1.6, 28.0, 'hot', '8-2 over the last 10 battles (hot).')",
            (levy_id, "2026-03-07T12:00:00"),
        )
        conn.execute(
            "INSERT INTO member_card_collection_snapshots (member_id, fetched_at, cards_json, support_cards_json) VALUES (?, ?, ?, ?)",
            (
                levy_id,
                "2026-03-11T01:00:00",
                json.dumps([
                    {"name": "Hog Rider", "level": 14, "maxLevel": 14},
                    {"name": "Fireball", "level": 14, "maxLevel": 14},
                    {"name": "Cannon", "level": 13, "maxLevel": 14},
                ]),
                json.dumps([]),
            ),
        )
        conn.execute(
            "INSERT INTO member_card_collection_snapshots (member_id, fetched_at, cards_json, support_cards_json) VALUES (?, ?, ?, ?)",
            (
                vijay_id,
                "2026-03-11T01:00:00",
                json.dumps([
                    {"name": "Arrows", "level": 14, "maxLevel": 14},
                ]),
                json.dumps([
                    {"name": "Ice Spirit", "level": 14, "maxLevel": 14},
                ]),
            ),
        )
        conn.commit()

        hot = db.get_members_on_hot_streak(min_streak=4, conn=conn)
        elite = db.get_members_with_most_level_16_cards(limit=2, conn=conn)

        assert hot[0]["tag"] == "#ABC123"
        assert hot[0]["current_streak"] == 6
        assert elite[0]["tag"] == "#ABC123"
        assert elite[0]["level_16_count"] == 2
        assert elite[0]["level_16_cards"] == ["Fireball", "Hog Rider"]
        assert elite[1]["tag"] == "#DEF456"
        assert elite[1]["level_16_count"] == 2
    finally:
        conn.close()


def test_record_join_date_upgrades_existing_membership_to_observed_join():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#DEF456", "name": "Vijay", "role": "member", "clanRank": 1}],
            conn=conn,
        )

        db.record_join_date("#DEF456", "Vijay", "2026-03-08", conn=conn)

        row = conn.execute(
            "SELECT joined_at, join_source FROM clan_memberships cm "
            "JOIN members m ON m.member_id = cm.member_id "
            "WHERE m.player_tag = '#DEF456' AND cm.left_at IS NULL"
        ).fetchone()
        assert row["joined_at"] == "2026-03-08"
        assert row["join_source"] == "observed_join"
    finally:
        conn.close()


def test_member_membership_summary_marks_returning_member():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "MONICA", "role": "member", "clanRank": 1}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#ABC123'"
        ).fetchone()["member_id"]
        conn.execute("DELETE FROM clan_memberships WHERE member_id = ?", (member_id,))
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-04-04', '2026-04-24', 'observed_join', 'manual_clear')",
            (member_id,),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-04-26', '2026-06-12', 'observed_join', 'manual_clear')",
            (member_id,),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-06-19', NULL, 'observed_join', NULL)",
            (member_id,),
        )
        conn.commit()

        summary = db.get_member_membership_summary("#ABC123", conn=conn)
        profile = db.get_member_profile("#ABC123", conn=conn)

        assert summary["is_returning"] is True
        assert summary["join_count"] == 3
        assert summary["prior_stints"] == 2
        assert summary["current_joined_at"] == "2026-06-19"
        assert summary["last_left_at"] == "2026-06-12"
        assert profile["membership_summary"]["is_returning"] is True
    finally:
        conn.close()


def test_current_joined_at_ignores_bootstrap_and_backfill_duplicates():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#DEF456", "name": "Vijay", "role": "member", "clanRank": 1}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#DEF456'"
        ).fetchone()["member_id"]
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-03-07', NULL, 'backfill', NULL)",
            (member_id,),
        )
        conn.commit()

        assert db._current_joined_at(conn, member_id) is None
    finally:
        conn.close()


def test_current_joined_at_prefers_trusted_clan_api_snapshot_over_backfill():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#NEW1", "name": "Ditika", "role": "member", "clanRank": 1}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#NEW1'"
        ).fetchone()["member_id"]
        conn.execute(
            "UPDATE clan_memberships SET join_source = 'bootstrap_seed', joined_at = '2026-03-07' WHERE member_id = ? AND left_at IS NULL",
            (member_id,),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-03-07', NULL, 'backfill', NULL)",
            (member_id,),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, '2026-03-07', NULL, 'clan_api_snapshot', NULL)",
            (member_id,),
        )
        conn.commit()
        db.backfill_join_dates(conn=conn)

        assert db._current_joined_at(conn, member_id) == "2026-03-07"
    finally:
        conn.close()


def test_recent_joins_excludes_initial_snapshot_cluster_but_keeps_later_real_additions():
    conn = db.get_connection(":memory:")
    try:
        recent_date = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
        recent_ts = f"{recent_date}T12:00:00"
        baseline_members = [
            {"tag": f"#TAG{i}", "name": f"Member {i}", "role": "member", "clanRank": i}
            for i in range(1, 6)
        ]
        db.snapshot_members(baseline_members, conn=conn)

        # Simulate a later real addition observed after the baseline snapshot date.
        newcomer_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#NEW1'"
        ).fetchone()
        assert newcomer_id is None
        conn.execute(
            "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) VALUES ('#NEW1', 'Ditika', 'active', ?, ?)",
            (recent_ts, recent_ts),
        )
        newcomer_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#NEW1'"
        ).fetchone()["member_id"]
        conn.execute(
            "INSERT INTO member_current_state (member_id, observed_at, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api, source, raw_json) "
            "VALUES (?, ?, 'member', 50, 6000, 6000, 6, NULL, 0, 0, NULL, NULL, NULL, NULL, 'clan_api', NULL)",
            (newcomer_id, recent_ts),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'clan_api_snapshot', NULL)",
            (newcomer_id, recent_date),
        )
        conn.commit()
        db.backfill_join_dates(conn=conn)

        recent = db.list_recent_joins(days=30, conn=conn)

        assert [item["tag"] for item in recent] == ["#NEW1"]
        assert recent[0]["joined_date"] == recent_date
    finally:
        conn.close()


def test_recent_joins_excludes_bootstrap_and_backfill_rows_but_keeps_clan_api_snapshot_same_day():
    conn = db.get_connection(":memory:")
    try:
        recent_date = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
        db.snapshot_members(
            [
                {"tag": "#OLD1", "name": "Vijay", "role": "member", "clanRank": 1},
                {"tag": "#NEW1", "name": "Ditika", "role": "member", "clanRank": 2},
            ],
            conn=conn,
        )
        old_id = conn.execute("SELECT member_id FROM members WHERE player_tag = '#OLD1'").fetchone()["member_id"]
        new_id = conn.execute("SELECT member_id FROM members WHERE player_tag = '#NEW1'").fetchone()["member_id"]
        conn.execute(
            "UPDATE clan_memberships SET join_source = 'bootstrap_seed', joined_at = ? WHERE member_id IN (?, ?) AND left_at IS NULL",
            (recent_date, old_id, new_id),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'backfill', NULL)",
            (old_id, recent_date),
        )
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'clan_api_snapshot', NULL)",
            (new_id, recent_date),
        )
        conn.commit()
        db.backfill_join_dates(conn=conn)

        recent = db.list_recent_joins(days=30, conn=conn)

        assert [item["tag"] for item in recent] == ["#NEW1"]
        assert recent[0]["joined_date"] == recent_date
    finally:
        conn.close()


def test_backfill_join_dates_promotes_trusted_current_membership_to_override_only():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#NEW1", "name": "Ditika", "role": "member", "clanRank": 1}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#NEW1'"
        ).fetchone()["member_id"]
        conn.execute(
            "UPDATE clan_memberships SET join_source = 'clan_api_snapshot', joined_at = '2026-03-08' WHERE member_id = ? AND left_at IS NULL",
            (member_id,),
        )
        conn.commit()

        db.backfill_join_dates(conn=conn)

        meta = conn.execute(
            "SELECT joined_at FROM member_metadata WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        membership_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM clan_memberships WHERE member_id = ? AND left_at IS NULL",
            (member_id,),
        ).fetchone()["cnt"]
        assert meta["joined_at"] == "2026-03-08"
        assert membership_count == 1
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


def test_store_war_log_captures_our_clan_score_on_each_race():
    """riverracelog always carries clanScore on every standing — we want it
    indexed on war_races for historical war-trophy trending, not buried in
    raw_json."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader", "trophies": 11000, "clanRank": 1}],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 131,
                        "sectionIndex": 0,
                        "createdDate": "20260406T095600.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 20,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "badgeId": 16000107,
                                    "fame": 10000,
                                    "repairPoints": 0,
                                    "finishTime": "20260406T095600.000Z",
                                    "periodPoints": 0,
                                    "clanScore": 340,
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            },
                            {
                                "rank": 2,
                                "trophyChange": 10,
                                "clan": {"tag": "#RIVAL", "name": "Rivals", "fame": 1800, "clanScore": 339, "participants": []},
                            },
                        ],
                    },
                    {
                        "seasonId": 131,
                        "sectionIndex": 1,
                        "createdDate": "20260413T095600.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 20,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "badgeId": 16000107,
                                    "fame": 10000,
                                    "repairPoints": 0,
                                    "finishTime": "20260413T095600.000Z",
                                    "periodPoints": 0,
                                    "clanScore": 360,
                                    "participants": [],
                                },
                            }
                        ],
                    },
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        rows = conn.execute(
            "SELECT section_index, our_clan_score FROM war_races "
            "WHERE season_id = 131 ORDER BY section_index"
        ).fetchall()
    finally:
        conn.close()

    assert [r["our_clan_score"] for r in rows] == [340, 360], (
        "our_clan_score should be captured from the rank-1 standing's clan.clanScore"
    )


def test_migration_31_backfills_our_clan_score_from_raw_json():
    """Existing war_races rows from before the column existed should be
    backfilled from raw_json. Simulates a DB where the column was added
    after ingest by clearing our_clan_score and re-running the migration."""
    import db._migrations as migrations

    conn = db.get_connection(":memory:")
    try:
        conn.execute(
            "INSERT INTO war_races (season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                130, 3, "20260401T095600.000Z", 2, -20, 8500, 5, "20260401T095600.000Z",
                json.dumps({
                    "seasonId": 130, "sectionIndex": 3, "createdDate": "20260401T095600.000Z",
                    "standings": [
                        {"rank": 1, "trophyChange": 100, "clan": {"tag": "#OTHER", "clanScore": 401}},
                        {"rank": 2, "trophyChange": -20, "clan": {"tag": "#J2RGCRVG", "clanScore": 321}},
                    ],
                }),
            ),
        )
        conn.execute("UPDATE war_races SET our_clan_score = NULL WHERE season_id = 130")
        conn.execute("ALTER TABLE war_races RENAME COLUMN our_clan_score TO _doomed")
        conn.execute("ALTER TABLE war_races DROP COLUMN _doomed")
        conn.commit()

        migrations._migration_31(conn)

        row = conn.execute(
            "SELECT our_clan_score FROM war_races WHERE season_id = 130 AND section_index = 3"
        ).fetchone()
        assert row["our_clan_score"] == 321, "backfill should match our rank-2 standing"
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


def test_upsert_war_current_state_uses_period_key_for_war_day_status():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1}],
            conn=conn,
        )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T01:00:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 10,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 1},
                        ],
                    },
                },
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T11:00:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 10,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 800, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 2},
                        ],
                    },
                },
                conn=conn,
            )

        row = conn.execute(
            "SELECT battle_date, observed_at, fame, decks_used_today, season_id, section_index, period_index, phase, phase_day_number FROM war_day_status"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM war_day_status").fetchone()["cnt"]
        assert count == 1
        assert row["battle_date"] == "s00129-w01-p010"
        assert row["observed_at"] == "2026-03-13T11:00:00"
        assert row["fame"] == 800
        assert row["decks_used_today"] == 2
        assert row["season_id"] == 129
        assert row["section_index"] == 1
        assert row["period_index"] == 10
        assert row["phase"] == "battle"
        assert row["phase_day_number"] == 1
    finally:
        conn.close()


def test_current_war_day_state_tracks_engagement_points_and_time_left():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
                {"tag": "#DEF456", "name": "Vijay", "role": "member", "expLevel": 64, "trophies": 9020, "clanRank": 2},
            ],
            conn=conn,
        )

        first_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 300,
                "repairPoints": 0,
                "periodPoints": 300,
                "clanScore": 150,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 100, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 300, "repairPoints": 0, "periodPoints": 300, "clanScore": 150}],
        }
        second_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 1000,
                "repairPoints": 0,
                "periodPoints": 1000,
                "clanScore": 155,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 2},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 1000, "repairPoints": 0, "periodPoints": 1000, "clanScore": 155}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
            db.upsert_war_current_state(first_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T12:30:00"):
            db.upsert_war_current_state(second_payload, conn=conn)

        state = db.get_current_war_day_state(conn=conn)
        deck_status = db.get_war_deck_status_today(conn=conn)
        snapshot_count = conn.execute("SELECT COUNT(*) AS cnt FROM war_participant_snapshots").fetchone()["cnt"]

        assert snapshot_count == 4
        assert state["week"] == 2
        assert state["phase"] == "battle"
        assert state["phase_display"] == "Battle Day 1"
        assert state["engaged_count"] == 2
        assert state["finished_count"] == 1
        assert state["untouched_count"] == 0
        assert state["time_left_seconds"] == 77400
        assert state["top_fame_today"][0]["name"] == "King Levy"
        assert state["top_fame_today"][0]["fame_today"] == 500
        assert deck_status["time_left_text"] == "21h 30m"
        assert deck_status["used_all_4"][0]["name"] == "King Levy"
        assert deck_status["used_some"][0]["name"] == "Vijay"
    finally:
        conn.close()


def test_current_war_day_state_anchors_to_first_observed_period():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader", "expLevel": 66, "trophies": 11429, "clanRank": 1},
            ],
            conn=conn,
        )

        late_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 800,
                "repairPoints": 0,
                "periodPoints": 800,
                "clanScore": 151,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 2},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 800, "repairPoints": 0, "periodPoints": 800, "clanScore": 151}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T18:00:00"):
            db.upsert_war_current_state(late_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T19:00:00"):
            db.upsert_war_current_state(late_payload, conn=conn)

        state = db.get_current_war_day_state(conn=conn)

        assert state["period_started_at"] == "2026-03-14T18:00:00"
        assert state["period_ends_at"] == "2026-03-15T18:00:00"
        assert state["time_left_seconds"] == 23 * 3600
        assert state["time_left_text"] == "23h 0m"
        assert state["first_observed_at"] == "2026-03-14T18:00:00"
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
                    # 20 days before activity anchor (2026-03-07). At 9020
                    # trophies the trophy-scaled threshold is ~12.6d, so
                    # this comfortably trips the inactive flag.
                    "lastSeen": "20260215T120000.000Z",
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
        assert all(member["tag"] != "#ABC123" for member in risk["members"])

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


def test_at_risk_uses_battle_activity_not_login_only():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {
                    "tag": "#LOG1N",
                    "name": "Daily Collector",
                    "role": "member",
                    "expLevel": 50,
                    "trophies": 5000,
                    "clanRank": 10,
                    "donations": 100,
                    "lastSeen": "20260613T120000.000Z",
                },
                {
                    "tag": "#PVP123",
                    "name": "Casual Battler",
                    "role": "member",
                    "expLevel": 50,
                    "trophies": 5000,
                    "clanRank": 11,
                    "donations": 0,
                    "lastSeen": "20260613T120000.000Z",
                },
            ],
            conn=conn,
        )
        battler = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?",
            ("#PVP123",),
        ).fetchone()
        conn.execute(
            "INSERT INTO member_battle_facts (member_id, battle_time, battle_type, is_war) "
            "VALUES (?, ?, ?, ?)",
            (battler["member_id"], "20260613T110000.000Z", "PvP", 0),
        )

        risk = db.get_members_at_risk(
            inactivity_days=7,
            min_donations_week=0,
            require_war_participation=False,
            conn=conn,
        )
        by_tag = {member["tag"]: member for member in risk["members"]}

        assert "#LOG1N" in by_tag
        inactive_reason = next(
            reason for reason in by_tag["#LOG1N"]["reasons"] if reason["type"] == "inactive"
        )
        assert inactive_reason["battle_days_ago"] == 90
        assert inactive_reason["login_days_ago"] == 0
        assert "#PVP123" not in by_tag
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
        db.set_member_join_date("#GHI789", "Finn", "2025-10-01", conn=conn)
        conn.execute(
            "INSERT INTO member_battle_facts (member_id, battle_time, battle_type, outcome) "
            "SELECT member_id, '20260307T110000.000Z', 'PvP', 'win' "
            "FROM members WHERE player_tag = '#DEF456'"
        )
        conn.execute(
            "INSERT INTO member_battle_facts (member_id, battle_time, battle_type, outcome) "
            "SELECT member_id, '20260307T100000.000Z', 'PvP', 'win' "
            "FROM members WHERE player_tag = '#GHI789'"
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
        db.upsert_war_current_state(
            {
                "state": "full",
                "seasonId": 129,
                "sectionIndex": 1,
                "periodIndex": 3,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 100,
                    "participants": [],
                },
            },
            conn=conn,
        )

        review = db.get_promotion_candidates(conn=conn)
        assert review["recommended"][0]["tag"] == "#DEF456"
        assert review["borderline"][0]["tag"] == "#GHI789"
        assert review["borderline"][0]["missing"] == ["war"]
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


def test_war_player_types_by_tag_batches_classification():
    from storage.war_analytics import war_player_types_by_tag

    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#REG", "name": "Regular", "role": "member"},
                {"tag": "#OCC", "name": "Occasional", "role": "member"},
                {"tag": "#RARE", "name": "Rare", "role": "member"},
                {"tag": "#NEVER", "name": "Never", "role": "member"},
            ],
            conn=conn,
        )
        member_ids = {
            row["player_tag"]: row["member_id"]
            for row in conn.execute("SELECT player_tag, member_id FROM members").fetchall()
        }
        # 4 war races, season 129
        for section in range(1, 5):
            conn.execute(
                "INSERT INTO war_races (season_id, section_index, our_rank, our_fame, total_clans) "
                "VALUES (?, ?, 1, 50000, 5)",
                (129, section),
            )
        race_ids = [
            row["war_race_id"]
            for row in conn.execute("SELECT war_race_id FROM war_races ORDER BY section_index").fetchall()
        ]
        # Regular: played all 4 (100%). Occasional: played 2 (50%). Rare: played 1 (25% = occasional boundary).
        # Never: no participation rows.
        for race_id in race_ids:
            conn.execute(
                "INSERT INTO war_participation (war_race_id, member_id, player_tag, decks_used) "
                "VALUES (?, ?, ?, 4)",
                (race_id, member_ids["#REG"], "#REG"),
            )
        # Occasional: played 2 of 4 (50%); filler rows for the 2 unplayed races
        for i, race_id in enumerate(race_ids):
            decks = 4 if i < 2 else 0
            conn.execute(
                "INSERT INTO war_participation (war_race_id, member_id, player_tag, decks_used) "
                "VALUES (?, ?, ?, ?)",
                (race_id, member_ids["#OCC"], "#OCC", decks),
            )
        # Rare: played 1 of 4 (25% = boundary, should still land as occasional since
        # the threshold is >=25%). To force "rare" (<25%), we need played/total < 0.25.
        # Use 1 of 5 races: add one more race so rare has 1 played / 5 total = 20%.
        conn.execute(
            "INSERT INTO war_races (season_id, section_index, our_rank, our_fame, total_clans) "
            "VALUES (?, 99, 1, 50000, 5)",
            (129,),
        )
        extra_race = conn.execute(
            "SELECT war_race_id FROM war_races WHERE section_index = 99"
        ).fetchone()["war_race_id"]
        rare_races = race_ids + [extra_race]
        for i, race_id in enumerate(rare_races):
            decks = 2 if i == 0 else 0
            conn.execute(
                "INSERT INTO war_participation (war_race_id, member_id, player_tag, decks_used) "
                "VALUES (?, ?, ?, ?)",
                (race_id, member_ids["#RARE"], "#RARE", decks),
            )
        # Never member: insert a 0-deck participation so total_races > 0 but played = 0
        for race_id in race_ids:
            conn.execute(
                "INSERT INTO war_participation (war_race_id, member_id, player_tag, decks_used) "
                "VALUES (?, ?, ?, 0)",
                (race_id, member_ids["#NEVER"], "#NEVER"),
            )
        conn.commit()

        result = war_player_types_by_tag(conn, ["#REG", "#OCC", "#RARE", "#NEVER", "#UNKNOWN"])
        assert result["#REG"] == "regular"
        assert result["#OCC"] == "occasional"
        assert result["#RARE"] == "rare"
        assert result["#NEVER"] == "never"
        assert "#UNKNOWN" not in result

    finally:
        conn.close()


def test_pick_best_match_scoring_rules():
    from storage.roster import pick_best_match

    # Empty input
    assert pick_best_match([]) is None

    # Single exact-ish match (score >= 850) → accept
    assert pick_best_match([{"match_score": 950, "player_tag": "#A"}])["player_tag"] == "#A"

    # Two exact-ish matches → reject (ambiguous)
    assert pick_best_match([
        {"match_score": 950, "player_tag": "#A"},
        {"match_score": 900, "player_tag": "#B"},
    ]) is None

    # Single fuzzy match → accept
    assert pick_best_match([{"match_score": 600, "player_tag": "#A"}])["player_tag"] == "#A"

    # Top outscores second by >=100 → accept top
    assert pick_best_match([
        {"match_score": 775, "player_tag": "#A"},
        {"match_score": 650, "player_tag": "#B"},
    ])["player_tag"] == "#A"

    # Top-second gap <100 → ambiguous
    assert pick_best_match([
        {"match_score": 650, "player_tag": "#A"},
        {"match_score": 625, "player_tag": "#B"},
    ]) is None


def test_war_player_types_by_tag_empty_input_returns_empty():
    from storage.war_analytics import war_player_types_by_tag

    conn = db.get_connection(":memory:")
    try:
        assert war_player_types_by_tag(conn, []) == {}
        assert war_player_types_by_tag(conn, [""]) == {}
    finally:
        conn.close()
