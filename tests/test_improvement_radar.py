from types import SimpleNamespace

import db
from scripts import review_improvement_opportunities as radar


def test_improvement_suggestion_upsert_is_idempotent():
    conn = db.get_connection(":memory:")
    try:
        first = db.upsert_improvement_suggestion(
            category="signal_gap",
            suggestion_key="signal_gap:test",
            title="Review uncovered signals",
            rationale="Signals are being observed without durable coverage.",
            proposed_change="Add a detector-specific coverage rule.",
            evidence={"basis": "test", "metrics": {"count": 1}},
            confidence=0.65,
            conn=conn,
        )
        second = db.upsert_improvement_suggestion(
            category="signal_gap",
            suggestion_key="signal_gap:test",
            title="Review uncovered signals",
            rationale="Signals are still being observed without durable coverage.",
            proposed_change="Add a detector-specific coverage rule and regression test.",
            evidence={"basis": "test", "metrics": {"count": 2}},
            confidence=0.75,
            conn=conn,
        )

        rows = db.list_improvement_suggestions(conn=conn)

        assert first["suggestion_id"] == second["suggestion_id"]
        assert len(rows) == 1
        assert rows[0]["confidence"] == 0.75
        assert rows[0]["evidence"]["metrics"]["count"] == 2
        assert rows[0]["first_seen_at"] == first["first_seen_at"]
    finally:
        conn.close()


def test_improvement_radar_uses_leader_action_notes_edits_and_channel_comments():
    conn = db.get_connection(":memory:")
    try:
        action = db.create_leader_action_recommendation(
            action_type="kick_recommendation",
            objective="Review inactive member",
            prompt_text="Kick xian for inactivity.",
            rationale="10 days no battle.",
            target_player_tag="#UGQPVQ9U9",
            target_player_name="xian",
            conn=conn,
        )
        db.update_leader_action_copy_text(
            action["action_id"],
            copy_text="Hold xian for now; recheck after the deferral.",
            discord_user_id="leader-1",
            conn=conn,
        )
        db.decide_leader_action(
            action["action_id"],
            status=db.ACTION_REJECTED,
            discord_user_id="leader-1",
            emoji="x",
            decision_note="Too soon; the deferral is still useful context.",
            conn=conn,
        )
        db.save_message(
            "channel:900",
            "user",
            "Please stop recommending this as a kick when a deferral is fresh.",
            channel_id="900",
            channel_name="leader-actions",
            workflow="arena-relay",
            event_type="leader_action_note",
            conn=conn,
        )

        specs = radar.collect_improvement_specs(days=30, conn=conn)
        leader_spec = next(spec for spec in specs if spec["suggestion_key"].startswith("routing_quality:fold-leader-action"))

        assert leader_spec["evidence"]["metrics"]["decision_notes"] == 1
        assert leader_spec["evidence"]["metrics"]["copy_edits"] == 1
        assert leader_spec["evidence"]["metrics"]["leader_action_channel_comments"] == 1
        assert any("deferral is fresh" in sample["detail"] for sample in leader_spec["evidence"]["samples"])

        stored = radar.store_improvement_specs([leader_spec], conn=conn)
        assert stored[0]["category"] == "routing_quality"
    finally:
        conn.close()


def test_awareness_gap_spec_ignores_accounted_skips_and_rejected_posts():
    conn = db.get_connection(":memory:")
    try:
        db.record_awareness_tick(
            workflow="player_intel",
            signals_in=3,
            posts_rejected=1,
            covered_keys=3,
            considered_skipped=0,
            all_ok=False,
            signal_outcomes=[
                {"signal_key": "hot:#ABC", "signal_type": "battle_hot_streak", "status": "covered"},
                {"signal_key": "push:#ABC", "signal_type": "battle_trophy_push", "status": "covered"},
                {"signal_key": "badge:#ABC", "signal_type": "badge_level_milestone", "status": "covered"},
            ],
            conn=conn,
        )
        db.record_awareness_tick(
            workflow="player_intel",
            signals_in=4,
            posts_rejected=1,
            covered_keys=1,
            considered_skipped=0,
            all_ok=False,
            signal_outcomes=[
                {"signal_key": "card:#ABC:16", "signal_type": "card_level_milestone", "status": "covered"},
                {"signal_key": "card:#ABC:16", "signal_type": "card_level_milestone", "status": "covered"},
                {"signal_key": "card:#ABC:16", "signal_type": "card_level_milestone", "status": "covered"},
                {"signal_key": "card:#ABC:16", "signal_type": "card_level_milestone", "status": "covered"},
            ],
            conn=conn,
        )
        db.record_awareness_tick(
            workflow="clan_awareness",
            signals_in=3,
            covered_keys=1,
            considered_skipped=2,
            all_ok=True,
            skipped_reason="intentionally quiet",
            conn=conn,
        )
        db.record_awareness_tick(
            workflow="war_awareness",
            signals_in=3,
            covered_keys=1,
            considered_skipped=1,
            all_ok=False,
            skipped_reason="missed one",
            conn=conn,
        )
        db.record_awareness_tick(
            workflow="clanops",
            signals_in=1,
            covered_keys=1,
            write_calls_denied=1,
            all_ok=False,
            skipped_reason="write denied",
            conn=conn,
        )

        spec = radar._awareness_gap_spec(conn, days=30)

        assert spec is not None
        metrics = spec["evidence"]["metrics"]
        assert metrics["gap_ticks"] == 2
        assert metrics["unaccounted_signals"] == 1
        assert metrics["write_calls_denied"] == 1
        sample_details = [sample["detail"] for sample in spec["evidence"]["samples"]]
        assert any("unaccounted=1" in detail for detail in sample_details)
        assert all("signals=3 covered=3" not in detail for detail in sample_details)
        assert all("skipped=2 unaccounted=0" not in detail for detail in sample_details)
    finally:
        conn.close()


def test_github_promotion_dry_run_does_not_call_runner():
    conn = db.get_connection(":memory:")
    try:
        suggestion = db.upsert_improvement_suggestion(
            category="data_health",
            suggestion_key="data_health:test",
            title="Inspect API drift",
            rationale="The API sentinel observed new paths.",
            proposed_change="Review the new paths and decide whether to model them.",
            evidence={"basis": "test"},
            confidence=0.9,
            conn=conn,
        )

        def fail_runner(args):
            raise AssertionError(f"runner should not be called in dry-run: {args}")

        results = radar.promote_suggestions_to_github(
            [suggestion],
            write=False,
            runner=fail_runner,
            conn=conn,
        )

        assert results == [{
            "suggestion_key": "data_health:test",
            "action": "create",
            "dry_run": True,
            "title": "Inspect API drift",
            "labels": ["data-health", "elixir-improvement", "enhancement", "generated", "needs-human-triage"],
            "github_issue_number": None,
        }]
        assert db.get_improvement_suggestion("data_health:test", conn=conn)["github_issue_number"] is None
    finally:
        conn.close()


def test_github_promotion_write_marks_created_issue():
    conn = db.get_connection(":memory:")
    try:
        suggestion = db.upsert_improvement_suggestion(
            category="cost_reliability",
            suggestion_key="cost_reliability:test",
            title="Reduce repeated prompt failures",
            rationale="Failures are recurring.",
            proposed_change="Add a guard and regression test.",
            evidence={"basis": "test"},
            confidence=0.9,
            conn=conn,
        )

        def runner(args):
            assert args[:3] == ["gh", "issue", "create"]
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/jthingelstad/elixir-bot/issues/99\n",
                stderr="",
            )

        results = radar.promote_suggestions_to_github(
            [suggestion],
            write=True,
            runner=runner,
            conn=conn,
        )
        updated = db.get_improvement_suggestion("cost_reliability:test", conn=conn)

        assert results[0]["ok"] is True
        assert results[0]["github_issue_number"] == 99
        assert updated["status"] == db.SUGGESTION_PROMOTED
        assert updated["github_issue_number"] == 99
    finally:
        conn.close()


def test_github_promotion_skips_low_confidence_suggestions():
    conn = db.get_connection(":memory:")
    try:
        suggestion = db.upsert_improvement_suggestion(
            category="data_health",
            suggestion_key="data_health:low",
            title="Low confidence hunch",
            rationale="The evidence is weak.",
            proposed_change="Do nothing until more evidence appears.",
            evidence={"basis": "test"},
            confidence=0.4,
            conn=conn,
        )

        results = radar.promote_suggestions_to_github([suggestion], min_confidence=0.8, conn=conn)

        assert results == [{
            "suggestion_key": "data_health:low",
            "action": "skip",
            "reason": "below_confidence_threshold",
        }]
    finally:
        conn.close()
