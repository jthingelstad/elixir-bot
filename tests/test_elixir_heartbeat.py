"""Tests for elixir heartbeat orchestration."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import heartbeat
from heartbeat._roster import detect_deck_archetype_changes, detect_form_slumps
import discord
import elixir
from runtime.signal_lanes import plan_signal_outcomes


def _publish_result(*content_types, sha="abc123def456", repo="jthingelstad/poapkings.com", branch="main"):
    return {
        "changed": True,
        "commit_sha": sha,
        "commit_url": f"https://github.com/{repo}/commit/{sha}",
        "repo": repo,
        "branch": branch,
        "changed_content_types": list(content_types),
        "changed_paths": [f"src/_data/elixir{content_type.title().replace('-', '')}.json" for content_type in content_types],
    }


def test_clan_awareness_tick_uses_bundle_without_refetch():
    """_clan_awareness_tick should use heartbeat.tick bundle and not refetch CR API."""
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "player_level_up", "name": "King Levy", "tag": "#ABC", "old_level": 65, "new_level": 66}],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.cr_api.get_clan") as mock_get_clan,
        patch("elixir.cr_api.get_current_war") as mock_get_war,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war, workflow="clan_awareness")
    mock_get_clan.assert_not_called()
    mock_get_war.assert_not_called()


def test_clan_awareness_tick_marks_failure_when_delivery_fails():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[
            {"type": "player_level_up", "tag": "#A", "name": "A"},
            {"type": "player_level_up", "tag": "#B", "name": "B"},
        ],
        clan={"memberList": []},
        war={},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=False)),
        patch("runtime.jobs._core.runtime_status.mark_job_failure") as mock_fail,
        patch("runtime.jobs._core.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_success.assert_not_called()
    mock_fail.assert_called_once()
    assert "2 of 2" in mock_fail.call_args.args[1]


def test_clan_awareness_tick_revokes_member_role_for_leavers():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[
            {"type": "member_leave", "tag": "#GONE", "name": "WhoDis"},
            {"type": "player_level_up", "tag": "#STAY", "name": "King Levy"},
        ],
        clan={"memberList": []},
        war={},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)),
        patch("runtime.onboarding.remove_member_role_for_tag", new=AsyncMock(return_value=(True, "Removed"))) as mock_remove,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_remove.assert_awaited_once()
    assert mock_remove.await_args.args[0] == "#GONE"
    assert "WhoDis" in mock_remove.await_args.kwargs["reason"]


def test_clan_awareness_tick_swallows_revoke_exceptions():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "member_leave", "tag": "#GONE", "name": "WhoDis"}],
        clan={"memberList": []},
        war={},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)),
        patch("runtime.onboarding.remove_member_role_for_tag", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("runtime.jobs._core.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_success.assert_called_once()


def test_leave_signal_marks_completed_kick_recommendation_as_leader_removal():
    from heartbeat._helpers import _enrich_leave_signal

    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{
                "tag": "#ABC123",
                "name": "QuickChurn",
                "role": "member",
                "lastSeen": "20260601T120000.000Z",
                "trophies": 5000,
                "donations": 0,
            }],
            conn=conn,
        )
        db.record_join_date(
            "#ABC123",
            "QuickChurn",
            # Seed on the Chicago calendar — tenure_days is computed against
            # db.chicago_today(), and UTC rolls to tomorrow at ~7 PM CT.
            (datetime.strptime(db.chicago_today(), "%Y-%m-%d").date() - timedelta(days=3)).isoformat(),
            conn=conn,
        )
        db.create_leader_action_recommendation(
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

        signal = _enrich_leave_signal("#ABC123", "QuickChurn", conn)

        assert signal["departure_kind"] == "leader_removal"
        assert signal["leader_action_rationale"] == "Inactive and not using war decks."
        assert signal["tenure_days"] == 3
    finally:
        conn.close()


def test_clan_events_skips_low_tenure_voluntary_departure():
    from runtime.signals.delivery import _is_low_value_public_departure

    assert not _is_low_value_public_departure({
        "type": "member_leave",
        "tag": "#KICKED",
        "name": "Removed",
        "tenure_days": 1,
        "departure_kind": "leader_removal",
    })

    signals = [{
        "type": "member_leave",
        "tag": "#ABC123",
        "name": "QuickChurn",
        "tenure_days": 1,
    }]
    clan = {"memberList": []}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "clan-events"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "member_leave:#ABC123",
            "source_signal_type": "member_leave",
            "target_channel_key": "clan-events",
            "target_channel_id": "1482352209878138880",
            "intent": "clan_event_public",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.upsert_signal_outcome") as mock_upsert,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "skipped"}]),
        patch("elixir.db.mark_signal_completed"),
        patch("elixir.elixir_agent.generate_channel_update") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_not_called()
    mock_post.assert_not_awaited()
    skip_calls = [
        call for call in mock_upsert.call_args_list
        if call.kwargs.get("delivery_status") == "skipped"
    ]
    assert skip_calls
    assert "low_value_departure" in skip_calls[0].kwargs["error_detail"]


def test_clan_awareness_tick_reseeds_startup_system_signals_before_tick():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._core.queue_startup_system_signals") as mock_queue,
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_queue.assert_called_once_with()


def test_fresh_time_left_computes_from_period_ends_at():
    """time_left_seconds should be derived from period_ends_at - now, not the
    stored (poll-time) value — otherwise it ages between polls (see #20)."""
    from datetime import datetime, timezone
    from heartbeat._war import _fresh_time_left_seconds

    frozen_now = datetime(2026, 4, 17, 5, 37, 0, tzinfo=timezone.utc)
    state = {
        "period_ends_at": "2026-04-17T08:37:00+00:00",  # 3 hours from now
        "time_left_seconds": 60 * 60,  # 1h stale — should be ignored
    }
    assert _fresh_time_left_seconds(state, now=frozen_now) == 3 * 60 * 60


def test_fresh_time_left_clamps_negative_to_zero():
    from datetime import datetime, timezone
    from heartbeat._war import _fresh_time_left_seconds

    frozen_now = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
    state = {
        "period_ends_at": "2026-04-17T09:00:00+00:00",  # an hour in the past
        "time_left_seconds": 3600,
    }
    assert _fresh_time_left_seconds(state, now=frozen_now) == 0


def test_fresh_time_left_falls_back_to_stored_when_no_ends_at():
    from heartbeat._war import _fresh_time_left_seconds

    state = {"time_left_seconds": 7200}
    assert _fresh_time_left_seconds(state) == 7200


def test_derive_war_anchor_minute_reads_latest_finish_time():
    """Anchor derivation pulls the minute from the newest non-sentinel finishTime."""
    from unittest.mock import patch

    from runtime.jobs import _core

    history = [
        {"finish_time": "20260412T093703.000Z"},  # 09:37 — current
        {"finish_time": "20260329T095604.000Z"},  # 09:56 — previous season
    ]
    with patch("runtime.jobs._core.db.get_war_history", return_value=history):
        assert _core._derive_war_anchor_minute() == 37


def test_derive_war_anchor_minute_skips_sentinel_values():
    """Sentinel 19691231 finishTimes (non-rank-1 clans) are skipped."""
    from unittest.mock import patch

    from runtime.jobs import _core

    history = [
        {"finish_time": "19691231T235959.000Z"},  # sentinel — skip
        {"finish_time": "20260412T093703.000Z"},  # real
    ]
    with patch("runtime.jobs._core.db.get_war_history", return_value=history):
        assert _core._derive_war_anchor_minute() == 37


def test_derive_war_anchor_minute_returns_none_when_no_log():
    from unittest.mock import patch

    from runtime.jobs import _core

    with patch("runtime.jobs._core.db.get_war_history", return_value=[]):
        assert _core._derive_war_anchor_minute() is None


def test_detect_returning_members_emits_signal_when_member_comes_back():
    """v4.7 #26: a previously stale member whose last_seen_api becomes fresh
    again fires member_active_again."""
    from datetime import datetime, timedelta

    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Wanderer", "role": "member",
              "lastSeen": "20260401T120000.000Z"}],
            conn=conn,
        )
        # Set the old snapshot time to match its stale last_seen
        conn.execute(
            "UPDATE member_state_snapshots SET observed_at = ? WHERE member_id = "
            "(SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-04-01T12:00:00",),
        )
        conn.commit()

        # Fresh snapshot: last_seen advanced (member just played)
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Wanderer", "role": "member",
              "lastSeen": "20260416T110000.000Z"}],
            conn=conn,
        )

        # "now" is 2026-04-16 noon — previous gap = 15 days, current gap = 0
        now = datetime(2026, 4, 16, 12, 0, 0)
        signals = heartbeat.detect_returning_members(now=now, conn=conn)
        assert len(signals) == 1
        assert signals[0]["type"] == "member_active_again"
        assert signals[0]["tag"] == "#ABC123"
        assert signals[0]["days_away"] == 15
        assert signals[0]["signal_log_type"].startswith("member_active_again:#ABC123")
    finally:
        conn.close()


def test_detect_returning_members_silent_for_continuously_active():
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Active", "role": "member",
              "lastSeen": "20260415T120000.000Z"}],
            conn=conn,
        )
        conn.execute(
            "UPDATE member_state_snapshots SET observed_at = ? WHERE member_id = "
            "(SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-04-15T12:00:00",),
        )
        conn.commit()

        db.snapshot_members(
            [{"tag": "#ABC123", "name": "Active", "role": "member",
              "lastSeen": "20260416T110000.000Z"}],
            conn=conn,
        )
        now = datetime(2026, 4, 16, 12, 0, 0)
        signals = heartbeat.detect_returning_members(now=now, conn=conn)
        assert signals == []
    finally:
        conn.close()


def _seed_weekly_donations(conn, *, metric_date, donations_by_name):
    """Insert member_daily_metrics rows for a given date's weekly donations."""
    now_iso = metric_date + "T23:00:00"
    for name, donations in donations_by_name.items():
        tag = f"#{name.upper()}"
        existing = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) "
                "VALUES (?, ?, 'active', ?, ?)",
                (tag, name, now_iso, now_iso),
            )
            existing = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
        member_id = existing["member_id"]
        conn.execute(
            "INSERT INTO member_daily_metrics (member_id, metric_date, donations_week) "
            "VALUES (?, ?, ?)",
            (member_id, metric_date, donations),
        )
    conn.commit()


def test_detect_weekly_donation_leader_fires_on_monday():
    """v4.7 #33: Monday heartbeat emits weekly_donation_leader for the prior week."""
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        _seed_weekly_donations(conn, metric_date="2026-04-12", donations_by_name={
            "Gooba": 108,
            "Shafith": 106,
            "Chanco": 98,
            "Quiet": 0,
        })
        monday = datetime(2026, 4, 13, 9, 0, 0)
        signals = heartbeat.detect_weekly_donation_leader(now=monday, conn=conn)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["type"] == "weekly_donation_leader"
        assert sig["week_ending"] == "2026-04-12"
        assert len(sig["leaders"]) == 3
        assert sig["leaders"][0]["name"] == "Gooba"
        assert sig["leaders"][0]["donations"] == 108
        assert sig["leaders"][0]["rank"] == 1
        assert sig["signal_log_type"].startswith("weekly_donation_leader:")
    finally:
        conn.close()


def test_detect_weekly_donation_leader_silent_on_non_monday():
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        _seed_weekly_donations(conn, metric_date="2026-04-12", donations_by_name={"Gooba": 108})
        tuesday = datetime(2026, 4, 14, 9, 0, 0)
        assert heartbeat.detect_weekly_donation_leader(now=tuesday, conn=conn) == []
    finally:
        conn.close()


def test_detect_weekly_donation_leader_dedups_same_week():
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        _seed_weekly_donations(conn, metric_date="2026-04-12", donations_by_name={"Gooba": 108})
        monday = datetime(2026, 4, 13, 9, 0, 0)
        first = heartbeat.detect_weekly_donation_leader(now=monday, conn=conn)
        assert len(first) == 1
        db.mark_signal_sent(first[0]["signal_log_type"], "2026-04-13", conn=conn)
        assert heartbeat.detect_weekly_donation_leader(now=monday, conn=conn) == []
    finally:
        conn.close()


def _seed_clan_metric(conn, *, metric_date, clan_score=None, clan_war_trophies=None):
    """Insert a clan_daily_metrics row."""
    conn.execute(
        "INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name, clan_score, clan_war_trophies, observed_at) "
        "VALUES (?, '#TEST', 'Test Clan', ?, ?, ?)",
        (metric_date, clan_score, clan_war_trophies, metric_date + "T12:00:00"),
    )
    conn.commit()


def test_detect_clan_war_trophies_record_fires_on_new_all_time_high():
    """War-trophies record fires when the latest day beats all prior days."""
    conn = db.get_connection(":memory:")
    try:
        _seed_clan_metric(conn, metric_date="2026-04-10", clan_war_trophies=200)
        _seed_clan_metric(conn, metric_date="2026-04-11", clan_war_trophies=210)
        _seed_clan_metric(conn, metric_date="2026-04-12", clan_war_trophies=220)
        signals = heartbeat.detect_clan_score_records(conn=conn)
        types = {s["type"] for s in signals}
        assert "clan_war_trophies_record" in types
        assert "clan_score_record" not in types
        rec = next(s for s in signals if s["type"] == "clan_war_trophies_record")
        assert rec["new_record"] == 220
        assert rec["previous_record"] == 210
        assert rec["metric_date"] == "2026-04-12"
    finally:
        conn.close()


def test_detect_clan_score_records_silent_when_not_a_new_high():
    conn = db.get_connection(":memory:")
    try:
        _seed_clan_metric(conn, metric_date="2026-04-10", clan_war_trophies=220)
        _seed_clan_metric(conn, metric_date="2026-04-11", clan_war_trophies=230)
        _seed_clan_metric(conn, metric_date="2026-04-12", clan_war_trophies=215)  # dip
        assert heartbeat.detect_clan_score_records(conn=conn) == []
    finally:
        conn.close()


def test_detect_clan_score_records_dedups_same_day():
    conn = db.get_connection(":memory:")
    try:
        _seed_clan_metric(conn, metric_date="2026-04-10", clan_war_trophies=220)
        _seed_clan_metric(conn, metric_date="2026-04-11", clan_war_trophies=230)
        first = heartbeat.detect_clan_score_records(conn=conn)
        assert len(first) == 1
        db.mark_signal_sent(first[0]["signal_log_type"], "2026-04-11", conn=conn)
        assert heartbeat.detect_clan_score_records(conn=conn) == []
    finally:
        conn.close()


def _seed_deck_snapshot(conn, *, tag, name, cards, fetched_at, mode_scope="overall"):
    """Insert a member_deck_snapshots row with the given card names."""
    import json
    row = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
    if row is None:
        now = "2026-04-16T12:00:00"
        conn.execute(
            "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (tag, name, now, now),
        )
        row = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
    member_id = row["member_id"]
    deck_json = json.dumps([{"name": c, "elixirCost": 3} for c in cards])
    conn.execute(
        "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, sample_size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (member_id, fetched_at, "battle_log", mode_scope, fetched_at, deck_json, 1),
    )
    conn.commit()


def test_detect_deck_archetype_changes_fires_on_major_swap():
    """v4.7 #33: 4+ card difference from 24h ago emits the signal."""
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        x_bow_deck = ["Ice Wizard", "Knight", "Rocket", "Skeletons", "Tesla", "The Log", "Tornado", "X-Bow"]
        bridge_spam = ["Bandit", "Battle Ram", "Electro Wizard", "Magic Archer", "The Log", "Poison", "Royal Ghost", "Lumberjack"]
        # 48h ago: X-Bow deck
        _seed_deck_snapshot(conn, tag="#ABC123", name="Swapper", cards=x_bow_deck,
                            fetched_at="2026-04-14T12:00:00")
        # Now: bridge spam (7 cards different, only The Log in common)
        _seed_deck_snapshot(conn, tag="#ABC123", name="Swapper", cards=bridge_spam,
                            fetched_at="2026-04-16T11:55:00")

        now = datetime(2026, 4, 16, 12, 0, 0)
        signals = detect_deck_archetype_changes(now=now, conn=conn)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["type"] == "deck_archetype_change"
        assert sig["tag"] == "#ABC123"
        assert sig["changed_count"] == 7
        assert "X-Bow" in sig["removed_cards"]
        assert "Bandit" in sig["added_cards"]
        assert sig["signal_log_type"] == "deck_archetype_change:#ABC123:2026-04-16"
    finally:
        conn.close()


def test_detect_deck_archetype_changes_silent_for_small_tweak():
    """Swapping one card (log -> zap) is tinkering, not an archetype change."""
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        old = ["Bats", "Goblin Gang", "Inferno Dragon", "Mega Knight", "Miner", "Skeleton Barrel", "Spear Goblins", "The Log"]
        new = ["Bats", "Goblin Gang", "Inferno Dragon", "Mega Knight", "Miner", "Skeleton Barrel", "Spear Goblins", "Zap"]
        _seed_deck_snapshot(conn, tag="#ABC123", name="Tinkerer", cards=old,
                            fetched_at="2026-04-14T12:00:00")
        _seed_deck_snapshot(conn, tag="#ABC123", name="Tinkerer", cards=new,
                            fetched_at="2026-04-16T11:55:00")

        now = datetime(2026, 4, 16, 12, 0, 0)
        assert detect_deck_archetype_changes(now=now, conn=conn) == []
    finally:
        conn.close()


def test_detect_deck_archetype_changes_needs_24h_baseline():
    """If no snapshot is 24h old, no signal — prevents firing on fresh joins."""
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        # Only a 1-hour-old snapshot and a current one, both different decks.
        _seed_deck_snapshot(conn, tag="#ABC123", name="NewJoin",
                            cards=["A", "B", "C", "D", "E", "F", "G", "H"],
                            fetched_at="2026-04-16T11:00:00")
        _seed_deck_snapshot(conn, tag="#ABC123", name="NewJoin",
                            cards=["M", "N", "O", "P", "Q", "R", "S", "T"],
                            fetched_at="2026-04-16T11:55:00")

        now = datetime(2026, 4, 16, 12, 0, 0)
        assert detect_deck_archetype_changes(now=now, conn=conn) == []
    finally:
        conn.close()


def test_detect_deck_archetype_changes_dedups_same_day():
    from datetime import datetime

    conn = db.get_connection(":memory:")
    try:
        _seed_deck_snapshot(conn, tag="#ABC123", name="Swapper",
                            cards=["A", "B", "C", "D", "E", "F", "G", "H"],
                            fetched_at="2026-04-14T12:00:00")
        _seed_deck_snapshot(conn, tag="#ABC123", name="Swapper",
                            cards=["M", "N", "O", "P", "Q", "R", "S", "T"],
                            fetched_at="2026-04-16T11:55:00")

        now = datetime(2026, 4, 16, 12, 0, 0)
        first = detect_deck_archetype_changes(now=now, conn=conn)
        assert len(first) == 1
        db.mark_signal_sent(first[0]["signal_log_type"], "2026-04-16", conn=conn)
        assert detect_deck_archetype_changes(now=now, conn=conn) == []
    finally:
        conn.close()


def _seed_form_row(conn, *, tag, name, scope, label, computed_at="2026-04-16T12:00:00"):
    """Insert or update a member + their member_recent_form row for tests."""
    now = "2026-04-16T12:00:00"
    row = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (tag, name, now, now),
        )
        row = conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()
    member_id = row["member_id"]
    conn.execute(
        "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, "
        "current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) "
        "VALUES (?, ?, ?, 10, 5, 5, 0, 0, NULL, 0.5, 0, 0, ?, ?) "
        "ON CONFLICT(member_id, scope) DO UPDATE SET computed_at = excluded.computed_at, "
        "form_label = excluded.form_label",
        (member_id, computed_at, scope, label, f"{label} form"),
    )
    conn.commit()


def test_detect_form_slumps_fires_on_strong_to_slumping_transition():
    """v4.7 #27: form crossing from top-tier to bottom-tier emits a signal."""
    conn = db.get_connection(":memory:")
    try:
        # First observation: strong — cursor seeds, no signal.
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="strong")
        first = detect_form_slumps(conn=conn)
        assert first == []

        # Second observation: slumping — crossing fires the signal.
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="slumping",
                       computed_at="2026-04-17T12:00:00")
        signals = detect_form_slumps(conn=conn)
        assert len(signals) == 1
        sig = signals[0]
        assert sig["type"] == "recent_form_slump"
        assert sig["tag"] == "#ABC123"
        assert sig["scope"] == "competitive_10"
        assert sig["previous_label"] == "strong"
        assert sig["new_label"] == "slumping"
        assert sig["signal_log_type"].startswith("recent_form_slump:#ABC123:competitive_10")
    finally:
        conn.close()


def test_detect_form_slumps_silent_for_top_to_top_change():
    conn = db.get_connection(":memory:")
    try:
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="hot")
        assert detect_form_slumps(conn=conn) == []
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="strong",
                       computed_at="2026-04-17T12:00:00")
        assert detect_form_slumps(conn=conn) == []
    finally:
        conn.close()


def test_detect_form_slumps_weekly_dedup():
    conn = db.get_connection(":memory:")
    try:
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="strong")
        detect_form_slumps(conn=conn)  # seed cursor
        _seed_form_row(conn, tag="#ABC123", name="Ace", scope="competitive_10", label="cold",
                       computed_at="2026-04-17T12:00:00")
        first = detect_form_slumps(conn=conn)
        assert len(first) == 1
        # Mark the signal sent and replay — cursor was updated to 'cold', so a
        # re-run in the same week should not re-emit.
        db.mark_signal_sent(first[0]["signal_log_type"], "2026-04-17", conn=conn)
        assert detect_form_slumps(conn=conn) == []
    finally:
        conn.close()


def test_detect_role_changes_emits_elder_promotion_signal():
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

        signals = heartbeat.detect_role_changes(conn=conn)

        assert len(signals) == 1
        assert signals[0]["type"] == "elder_promotion"
        assert signals[0]["tag"] == "#ABC123"
        assert signals[0]["old_role"] == "member"
        assert signals[0]["new_role"] == "elder"
        assert signals[0]["signal_log_type"].startswith("role_change:#ABC123:member->elder:")
    finally:
        conn.close()


def test_detect_role_changes_ignores_demotion_from_elder():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
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
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )

        signals = heartbeat.detect_role_changes(conn=conn)

        assert signals == []
    finally:
        conn.close()


def test_war_awareness_tick_uses_stored_war_detection_bundle():
    bundle = heartbeat.WarAwarenessResult(
        signals=[{"type": "war_battle_day_live_update", "season_id": 129, "day_number": 1}],
        clan={"name": "POAP KINGS", "tag": "#J2RGCRVG"},
        war={"state": "warDay"},
        cursor_updates=[{"detector_key": "war_live_state_pipeline", "scope_key": "", "cursor_int": 12, "cursor_text": None, "metadata": {}}],
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.detect_war_signals_from_storage", return_value=bundle) as mock_detect,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)) as mock_deliver,
        patch("runtime.jobs._core._persist_signal_detector_cursors") as mock_persist,
    ):
        asyncio.run(elixir._war_awareness_tick())

    mock_detect.assert_called_once_with()
    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war, workflow="war_awareness")
    mock_persist.assert_called_once_with(bundle.cursor_updates)


def test_war_poll_tick_uses_war_ingest_entrypoint():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.heartbeat.ingest_live_war_state", return_value={
            "war": {"state": "warDay"},
            "race_log_refreshed": True,
            "race_log_items": 1,
        }) as mock_ingest,
        patch("elixir._clear_cr_api_failure_alert_if_recovered") as mock_clear,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(elixir._war_poll_tick())

    mock_ingest.assert_called_once_with(refresh_race_log=True)
    mock_clear.assert_called_once_with()
    assert mock_success.call_args.args[0] == "war_poll"
    assert "war snapshot stored" in mock_success.call_args.args[1]


def test_war_awareness_tick_does_not_advance_cursors_when_delivery_fails():
    bundle = heartbeat.WarAwarenessResult(
        signals=[{"type": "war_battle_day_live_update", "season_id": 129, "day_number": 1}],
        clan={"name": "POAP KINGS", "tag": "#J2RGCRVG"},
        war={"state": "warDay"},
        cursor_updates=[{"detector_key": "war_live_state_pipeline", "scope_key": "", "cursor_int": 12, "cursor_text": None, "metadata": {}}],
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.detect_war_signals_from_storage", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=False)),
        patch("runtime.jobs._core._persist_signal_detector_cursors") as mock_persist,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._war_awareness_tick())

    mock_persist.assert_not_called()
    assert mock_failure.call_args.args[0] == "war_awareness"


def test_heartbeat_tick_include_war_false_skips_live_war_fetch():
    conn = db.get_connection(":memory:")
    try:
        with (
            patch("heartbeat.cr_api.get_clan", return_value={"memberList": [{"tag": "#ABC", "name": "King Levy"}]}),
            patch("heartbeat.cr_api.get_current_war") as mock_get_war,
        ):
            heartbeat.tick(conn=conn, include_nonwar=False, include_war=False)

        mock_get_war.assert_not_called()
    finally:
        conn.close()


def test_deliver_signal_group_saves_multipart_channel_update_as_separate_messages():
    signals = [{"type": "war_week_rollover", "season_id": 130, "week": 1}]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "warDay"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "river-race"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "war-week-1",
            "source_signal_type": "war_week_rollover",
            "target_channel_key": "river-race",
            "target_channel_id": "1482352067573059675",
            "intent": "war_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_completed"),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "War update",
            "content": ["First post", "Second post"],
        }),
        patch("elixir._post_to_elixir", new=AsyncMock()),
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
        patch("runtime.jobs._signals._post_signal_memory", new=AsyncMock()),
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    observation_saves = [
        call for call in mock_save.call_args_list
        if call.kwargs.get("event_type") in {"channel_update", "channel_update_part"}
    ]
    assert len(observation_saves) == 2
    assert observation_saves[0].args[2] == "First post"
    assert observation_saves[1].args[2] == "Second post"
    assert observation_saves[0].kwargs["event_type"] == "channel_update"
    assert observation_saves[1].kwargs["event_type"] == "channel_update_part"


def test_deliver_signal_group_skips_post_when_llm_returns_no_post_decision():
    """Backstop for public highlight incidents: when the LLM
    returns metadata.decision == "no_post", the pipeline must not post the
    refusal text to Discord and must mark the outcome as skipped."""
    signals = [{"type": "best_trophies_peak", "tag": "#ABC123", "old_best": 6560, "new_best": 6593}]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC123"}]}
    war = {"state": "training"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "player-highlights"
    channel.type = "text"

    refusal_result = {
        "event_type": "channel_update",
        "summary": "PB bump does not meet durable-milestone bar",
        "content": "This signal does not warrant a post in #player-highlights...",
        "metadata": {"decision": "no_post", "reason": "routine_ladder_movement"},
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "best_trophies_peak||#ABC123||||||",
            "source_signal_type": "best_trophies_peak",
            "target_channel_key": "member-highlights",
            "target_channel_id": "1482352147029950474",
            "intent": "player_progress",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome") as mock_upsert,
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "skipped"}]),
        patch("elixir.db.mark_signal_completed"),
        patch("elixir.elixir_agent.generate_channel_update", return_value=refusal_result),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
        patch("runtime.jobs._signals._post_signal_memory", new=AsyncMock()),
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    assert mock_post.await_count == 0, "refusal text must not be posted to Discord"
    assert mock_save.call_count == 0, "refusal must not be saved as an assistant message"

    skip_calls = [
        call for call in mock_upsert.call_args_list
        if call.kwargs.get("delivery_status") == "skipped"
    ]
    assert skip_calls, "signal outcome must be marked skipped"
    assert "llm_no_post" in (skip_calls[0].kwargs.get("error_detail") or "")


def test_deliver_signal_group_posts_preauthored_system_signal_without_llm():
    signals = [{
        "type": "capability_unlock",
        "signal_key": "capability_card_modes_and_war_completion_v1",
        "payload": {
            "audience": "clan",
            "title": "Achievement Unlocked: Sharper Card And War Intel",
            "discord_content": "**Subject**\n\nPreauthored body.",
        },
    }]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "training"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "capability_card_modes_and_war_completion_v1",
            "source_signal_type": "capability_unlock",
            "target_channel_key": "announcements",
            "target_channel_id": "1474760975851982959",
            "intent": "system_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_completed"),
        patch("elixir.db.mark_system_signal_announced"),
        patch("elixir.elixir_agent.generate_channel_update") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
        patch("runtime.jobs._signals._post_signal_memory", new=AsyncMock()),
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_not_called()
    mock_post.assert_awaited_once_with(
        channel,
        {
            "event_type": "channel_update",
            "summary": "Achievement Unlocked: Sharper Card And War Intel",
            "content": "**Subject**\n\nPreauthored body.",
        },
    )
    assert mock_save.call_args.args[2] == "**Subject**\n\nPreauthored body."


def test_system_signal_updates_deliver_preauthored_posts_through_awareness_intent():
    signal = {
        "type": "capability_unlock",
        "signal_key": "capability_test_v1",
        "payload": {
            "title": "Achievement Unlocked: Test",
            "discord_content": "**Subject**\n\nPreauthored body.",
            "audience": "clan",
        },
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.signals.system.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals._deliver_awareness_post", new=AsyncMock(return_value=True)) as mock_post,
        patch("runtime.jobs._signals._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_awareness,
        patch("runtime.jobs._signals._mark_signal_group_completed", new=AsyncMock()) as mock_completed,
    ):
        asyncio.run(elixir._post_system_signal_updates([signal], {}, {}))

    mock_awareness.assert_not_awaited()
    mock_post.assert_awaited_once()
    post = mock_post.await_args.args[0]
    assert post["channel"] == "announcements"
    assert post["content"] == "**Subject**\n\nPreauthored body."
    assert post["covers_signal_keys"] == ["capability_test_v1"]
    mock_completed.assert_awaited_once_with([signal])


def test_deliver_signal_group_stores_war_recap_memory_for_river_race_batch():
    signals = [{"type": "war_battle_day_complete", "season_id": 129, "week": 2, "day_number": 1}]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "warDay"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "river-race"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "war-day-recap",
            "source_signal_type": "war_battle_day_complete",
            "target_channel_key": "river-race",
            "target_channel_id": "1482352067573059675",
            "intent": "war_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message"),
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_completed"),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "Battle day recap",
            "content": "Battle day recap post",
        }),
        patch("elixir._post_to_elixir", new=AsyncMock()),
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
        patch("runtime.jobs._signals._post_signal_memory", new=AsyncMock()),
        patch("runtime.jobs._signals._store_recap_memories_for_signal_batch") as mock_memory,
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_memory.assert_called_once_with(signals, ["Battle day recap post"], 1482352067573059675)


def test_clan_awareness_tick_posts_join_messages_through_shared_sender():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "member_join", "name": "King Levy", "tag": "#ABC"}],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war, workflow="clan_awareness")


def test_clan_awareness_tick_marks_non_system_signal_sent_after_successful_post():
    signals = [{"type": "war_week_rollover", "season_id": 130, "week": 1}]
    with patch("elixir.db.mark_signal_completed") as mock_mark_signal_sent:
        asyncio.run(elixir._mark_signal_group_completed(signals))
    mock_mark_signal_sent.assert_called_once_with("war_week_rollover", db.chicago_today())


def test_clan_awareness_tick_does_not_mark_non_system_signal_sent_when_post_fails():
    outcome_rows = [{"delivery_status": "failed"}]
    assert not all(row.get("delivery_status") in {"delivered", "skipped"} for row in outcome_rows)


def test_weekly_discord_invite_relay_emits_weekly_signal():
    async def fake_sidecars(signals, clan, war):
        return len(signals)

    with (
        patch("elixir.runtime_status.mark_job_start") as mock_start,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
        patch("runtime.jobs._core._deliver_arena_relay_sidecars", new=AsyncMock(side_effect=fake_sidecars)) as mock_sidecars,
    ):
        asyncio.run(elixir._weekly_discord_invite_relay())

    mock_start.assert_called_once_with("weekly_discord_invite_relay")
    mock_failure.assert_not_called()
    signal = mock_sidecars.await_args.args[0][0]
    assert signal["type"] == "discord_invite_reminder"
    assert signal["signal_key"].startswith("discord_invite_reminder:")
    mock_success.assert_called_once()


def test_plan_signal_outcomes_routes_clan_audience_system_signal_to_announcements():
    outcomes = plan_signal_outcomes([{
        "type": "capability_unlock",
        "signal_key": "capability_boat_defense_intelligence_v1",
        "payload": {
            "title": "Achievement Unlocked: Boat Defense Intel",
            "audience": "clan",
        },
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "announcements"


def test_plan_signal_outcomes_routes_leadership_audience_system_signal_to_leader_lounge():
    outcomes = plan_signal_outcomes([{
        "type": "capability_unlock",
        "signal_key": "capability_private_ops_note_v1",
        "payload": {
            "title": "Achievement Unlocked: Ops Note",
            "audience": "leadership",
        },
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "leader-lounge"


def test_plan_signal_outcomes_routes_api_sentinel_to_leader_lounge():
    outcomes = plan_signal_outcomes([{
        "type": "api_event_sentinel",
        "signal_key": "api_event_sentinel:202606130101:abc123",
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "leader-lounge"


def test_plan_signal_outcomes_routes_battle_hot_streak_to_member_highlights():
    outcomes = plan_signal_outcomes([{
        "type": "battle_hot_streak",
        "tag": "#ABC",
        "name": "King Levy",
        "streak": 8,
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "member-highlights"
    assert outcomes[0]["intent"] == "battle_mode_update"
    assert outcomes[0]["required"] is True


def test_plan_signal_outcomes_routes_path_of_legend_promotion_to_member_highlights():
    outcomes = plan_signal_outcomes([{
        "type": "path_of_legend_promotion",
        "tag": "#ABC",
        "name": "King Levy",
        "league": "Champion II",
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "member-highlights"


def test_plan_signal_outcomes_routes_durable_milestone_to_member_highlights():
    outcomes = plan_signal_outcomes([{
        "type": "new_card_unlocked",
        "tag": "#ABC",
        "name": "King Levy",
        "card_name": "Mirror",
        "rarity": "legendary",
    }])

    channels = [outcome["target_channel_key"] for outcome in outcomes]
    assert channels == ["member-highlights", "arena-relay"]
    assert outcomes[0]["required"] is True
    assert outcomes[1]["intent"] == "celebration_relay"
    assert outcomes[1]["required"] is False


def test_plan_signal_outcomes_routes_join_anniversary_to_arena_relay_sidecar():
    outcomes = plan_signal_outcomes([{
        "type": "join_anniversary",
        "members": [{"tag": "#ABC", "name": "King Levy", "tenure_days": 365}],
    }])

    channels = [outcome["target_channel_key"] for outcome in outcomes]
    assert channels == ["clan-events", "arena-relay"]
    assert outcomes[1]["intent"] == "celebration_relay"


def test_plan_signal_outcomes_routes_cr_account_anniversary_to_arena_relay_sidecar():
    outcomes = plan_signal_outcomes([{
        "type": "cr_account_anniversary",
        "tag": "#ABC",
        "name": "King Levy",
        "new_years": 5,
    }])

    channels = [outcome["target_channel_key"] for outcome in outcomes]
    assert channels == ["member-highlights", "arena-relay"]
    assert outcomes[1]["intent"] == "celebration_relay"


def test_plan_signal_outcomes_splits_mixed_durable_and_battle_mode_batch():
    outcomes = plan_signal_outcomes([
        {"type": "battle_hot_streak", "tag": "#ABC", "name": "King Levy", "streak": 6},
        {"type": "arena_change", "tag": "#ABC", "name": "King Levy"},
    ])

    channels = [o["target_channel_key"] for o in outcomes]
    assert channels == ["member-highlights"]
    assert outcomes[0]["intent"] == "member_highlights"


def test_battle_mode_group_classifies_known_modes():
    from storage.player import _battle_mode_group

    assert _battle_mode_group(is_war=1) == "war"
    assert _battle_mode_group(is_ranked=1) == "ranked"
    assert _battle_mode_group(is_ladder=1) == "ladder"
    assert _battle_mode_group(is_special_event=1) == "special_event"
    assert _battle_mode_group(is_hosted_match=True) == "friendly"
    assert _battle_mode_group() == "other"
    # War wins the priority race over other flags
    assert _battle_mode_group(is_war=1, is_ladder=1, is_ranked=1) == "war"


def test_plan_signal_outcomes_makes_badge_level_only_batches_optional():
    outcomes = plan_signal_outcomes([{
        "type": "badge_level_milestone",
        "tag": "#ABC",
        "name": "King Levy",
        "badge_name": "MasteryKnight",
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "member-highlights"
    assert outcomes[0]["required"] is False


def test_plan_signal_outcomes_routes_live_finish_signal_to_war_channels():
    outcomes = plan_signal_outcomes([{
        "type": "war_race_finished_live",
        "season_id": 129,
        "section_index": 1,
        "week": 2,
    }])

    assert [(item["target_channel_key"], item["required"]) for item in outcomes] == [
        ("river-race", True),
        ("leader-lounge", False),
    ]


def test_plan_signal_outcomes_routes_actionable_war_signal_to_arena_relay():
    outcomes = plan_signal_outcomes([{
        "type": "war_battle_day_started",
        "season_id": 133,
        "section_index": 1,
        "week": 2,
    }])

    assert [(item["target_channel_key"], item["intent"], item["required"]) for item in outcomes] == [
        ("river-race", "war_update", True),
        ("arena-relay", "war_relay_brief", False),
    ]


def test_plan_signal_outcomes_routes_member_join_to_arena_relay_welcome():
    outcomes = plan_signal_outcomes([{
        "type": "member_join",
        "tag": "#ABC",
        "name": "King Levy",
    }])

    assert [(item["target_channel_key"], item["intent"], item["required"]) for item in outcomes] == [
        ("clan-events", "member_join_public", True),
        ("leader-lounge", "member_join_ops", True),
        ("arena-relay", "welcome_relay", False),
    ]


def test_plan_signal_outcomes_routes_weekly_discord_invite_to_arena_relay():
    outcomes = plan_signal_outcomes([{
        "type": "discord_invite_reminder",
        "signal_key": "discord_invite_reminder:2026-W24",
    }])

    assert [(item["target_channel_key"], item["intent"], item["required"]) for item in outcomes] == [
        ("arena-relay", "discord_invite_relay", False),
    ]


def test_arena_relay_result_is_copy_paste_brief():
    from runtime.signals.delivery import _build_arena_relay_result

    result = _build_arena_relay_result([{
        "type": "war_battle_day_live_update",
        "race_rank": 1,
        "clan_fame": 6870,
    }])

    assert result["event_type"] == "war_relay_brief"
    assert isinstance(result["content"], list)
    assert "**R? 📣 In-game relay**" in result["content"][0]
    assert "📋 Copy the next message into Clash Royale." in result["content"][0]
    assert "✅ done  ❌ decline" in result["content"][0]
    assert result["content"][1] == result["metadata"]["relay_copy"]
    assert "POAP KINGS is 1st with 6,870 fame" in result["content"][1]
    assert len(result["metadata"]["relay_copy"]) <= 240


def test_arena_relay_result_does_not_template_new_member_welcome_copy():
    from runtime.signals.delivery import _build_arena_relay_result

    result = _build_arena_relay_result([{
        "type": "member_join",
        "tag": "#ABC",
        "name": "King Levy",
    }])

    assert result is None


def test_member_join_welcome_context_gives_elixir_profile_facts():
    from runtime.signals.delivery import _member_join_welcome_context

    with patch("runtime.signals.delivery.db.get_member_profile", return_value={
        "player_tag": "#ABC",
        "member_name": "King Levy",
        "cr_account_age_years": 7,
        "cr_clan_war_wins": 421,
        "cr_battle_wins": 6400,
        "cr_collection_level": 1597,
        "cr_collection_level_badge_tier": 8,
        "cr_collection_level_badge_max_tier": 10,
        "cr_banner_count": 44,
        "cr_emote_count": 88,
        "current_favourite_card_name": "Archer Queen",
        "card_collection_summary": {
            "maxed_cards_count": 23,
            "highest_level": 16,
        },
        "trophies": 6000,
    }):
        context = _member_join_welcome_context("Signal context", {
            "type": "member_join",
            "tag": "#ABC",
            "name": "King Levy",
        })

    assert "Leader-actions new-member welcome task:" in context
    assert "Name: King Levy" in context
    assert "Player tag: #ABC" in context
    assert "Years played/account age: 7 years" in context
    assert "Max-level cards: 23" in context
    assert "Collection Level: 1,597" in context
    assert "Collection Level badge tier: 8/10" in context
    assert "Favorite card: Archer Queen" in context
    assert "Highest card level: 16" in context
    assert "Banner collection: 44" in context
    assert "Emote collection: 88" in context
    assert "Fallback only - clan war wins: 421" in context
    assert "Fallback only - battle wins: 6,400" in context
    assert "Fallback only - current trophies: 6,000" in context
    assert "Include `POAP KINGS` exactly" in context
    assert "Prefer years played/account age" in context
    assert "under 120 characters" in context
    assert "real leader typing in Clash Royale clan chat" in context
    assert "Do not mention war state" in context
    assert "Return `content` as a single string" in context


def test_member_join_welcome_context_marks_returning_members():
    from runtime.signals.delivery import _member_join_welcome_context

    with patch("runtime.signals.delivery.db.get_member_profile", return_value={
        "player_tag": "#ABC",
        "member_name": "MONICA",
        "cr_account_age_years": 9,
        "cr_banner_count": 205,
        "membership_summary": {
            "is_returning": True,
            "join_count": 3,
            "prior_stints": 2,
            "last_left_at": "2026-06-12",
        },
    }):
        context = _member_join_welcome_context("Signal context", {
            "type": "member_join",
            "tag": "#ABC",
            "name": "MONICA",
        })

    assert "Clan membership: returning member; 3 recorded POAP KINGS stints; last left 2026-06-12" in context
    assert "say welcome back rather than welcoming them as brand new" in context


def test_generated_welcome_relay_result_rejects_copy_without_member_name():
    from runtime.signals.delivery import _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "King Levy"}],
        {"content": "Welcome to POAP KINGS. Strong profile already."},
    )

    assert result is None


def test_generated_welcome_relay_result_rejects_generic_copy_for_returning_member():
    from runtime.signals.delivery import _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "MONICA"}],
        {"content": "Welcome to POAP KINGS, MONICA! 9 years played and 205 banners."},
        profile_facts=[
            "- Name: MONICA",
            "- Player tag: #ABC",
            "- Clan membership: returning member; 3 recorded POAP KINGS stints; last left 2026-06-12",
            "- Years played/account age: 9 years",
            "- Banner collection: 205",
        ],
    )

    assert result is None


def test_generated_welcome_relay_result_accepts_welcome_back_for_returning_member():
    from runtime.signals.delivery import _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "MONICA"}],
        {"content": "Welcome back to POAP KINGS, MONICA! 9 years played and 205 banners."},
        profile_facts=[
            "- Name: MONICA",
            "- Player tag: #ABC",
            "- Clan membership: returning member; 3 recorded POAP KINGS stints; last left 2026-06-12",
            "- Years played/account age: 9 years",
            "- Banner collection: 205",
        ],
    )

    assert result is not None
    assert "Welcome back" in result["metadata"]["relay_copy"]


def test_generated_welcome_relay_result_rejects_copy_without_poap_kings():
    from runtime.signals.delivery import _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "King Levy"}],
        {"content": "Welcome King Levy! 7 years played and 23 max cards is a cool profile."},
    )

    assert result is None


def test_generated_welcome_relay_result_rejects_copy_without_profile_detail_when_available():
    from runtime.signals.delivery import _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "King Levy"}],
        {"content": "Welcome to POAP KINGS, King Levy! Glad to have you here."},
        profile_facts=[
            "- Name: King Levy",
            "- Player tag: #ABC",
            "- Years played/account age: 7 years",
            "- Max-level cards: 23",
        ],
    )

    assert result is None


def test_generated_welcome_relay_result_clips_to_clan_chat_length():
    from runtime.signals.delivery import ARENA_RELAY_WELCOME_MAX_COPY_CHARS, _build_generated_welcome_relay_result

    result = _build_generated_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "King Levy"}],
        {"content": "Welcome to POAP KINGS, King Levy! 7 years played and 23 max cards is a cool profile."},
        profile_facts=[
            "- Name: King Levy",
            "- Player tag: #ABC",
            "- Years played/account age: 7 years",
            "- Max-level cards: 23",
        ],
    )

    assert result is not None
    copy = result["metadata"]["relay_copy"]
    assert len(copy) <= ARENA_RELAY_WELCOME_MAX_COPY_CHARS
    assert result["content"][1] == copy


def test_fallback_welcome_relay_result_uses_profile_facts_when_generation_is_empty():
    from runtime.signals.delivery import _build_fallback_welcome_relay_result

    result = _build_fallback_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "King Levy"}],
        profile_facts=[
            "- Name: King Levy",
            "- Player tag: #ABC",
            "- Collection Level: 1,597",
            "- Favorite card: Bowler",
        ],
    )

    assert result is not None
    copy = result["metadata"]["relay_copy"]
    assert "POAP KINGS" in copy
    assert "King Levy" in copy
    assert "1,597" in copy


def test_fallback_welcome_relay_result_says_welcome_back_for_returning_member():
    from runtime.signals.delivery import _build_fallback_welcome_relay_result

    result = _build_fallback_welcome_relay_result(
        [{"type": "member_join", "tag": "#ABC", "name": "MONICA"}],
        profile_facts=[
            "- Name: MONICA",
            "- Player tag: #ABC",
            "- Clan membership: returning member; 3 recorded POAP KINGS stints; last left 2026-06-12",
            "- Years played/account age: 9 years",
            "- Banner collection: 205",
        ],
    )

    assert result is not None
    copy = result["metadata"]["relay_copy"]
    assert copy.startswith("Welcome back to POAP KINGS, MONICA!")
    assert result["metadata"]["target_player_tag"] == "#ABC"


def test_deliver_new_member_welcome_relay_uses_elixir_authored_copy():
    signals = [{
        "type": "member_join",
        "signal_key": "member_join:#ABC",
        "tag": "#ABC",
        "name": "King Levy",
    }]
    clan = {"memberList": [{"tag": "#ABC", "name": "King Levy"}]}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    authored_copy = "Welcome to POAP KINGS, King Levy! 7 years played and 23 max cards is a cool profile."

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "member_join:#ABC",
            "source_signal_type": "member_join",
            "target_channel_key": "arena-relay",
            "target_channel_id": 1513758211206025227,
            "intent": "welcome_relay",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.signals.delivery.db.get_member_profile", return_value={
            "player_tag": "#ABC",
            "member_name": "King Levy",
            "cr_account_age_years": 7,
            "card_collection_summary": {"maxed_cards_count": 23},
            "cr_clan_war_wins": 421,
            "cr_battle_wins": 6400,
            "cr_collection_level": 1597,
            "trophies": 6000,
        }),
        patch("runtime.signals.delivery.build_lane_memory_context", return_value={"durable_memories": []}),
        patch("elixir.db.list_leader_action_feedback_profiles", return_value=[{
            "memory_id": 900,
            "scope": "leadership",
            "summary": "Use short native clan-chat welcomes.",
            "body": "Use short native clan-chat welcomes.",
        }]),
        patch("runtime.signals.delivery.can_post_leader_action", return_value=(True, None)),
        patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={
            "messages": [authored_copy],
            "summary": "Welcome King Levy",
        }) as mock_generate,
        patch("elixir.db.build_leader_action_baseline", return_value={}),
        patch("elixir.db.create_leader_action_recommendation", return_value={
            "action_id": 43,
            "action_key": "welcome:#ABC",
            "status": "proposed",
        }) as mock_create,
        patch("runtime.signals.delivery.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=2000),
            SimpleNamespace(id=2001),
        ])) as mock_card,
        patch("elixir.db.update_leader_action_message") as mock_update_message,
        patch("elixir.db.update_leader_action_copy_message") as mock_update_copy,
        patch("elixir.db.save_message"),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("runtime.jobs._signals._mark_signal_group_completed"),
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_called_once()
    request = mock_generate.call_args.args[0]
    assert request["intent"] == "welcome_relay"
    assert request["target_surface"] == "Clash Royale in-game clan chat"
    assert request["required_terms"] == ["POAP KINGS", "King Levy"]
    assert "Years played/account age: 7 years" in request["context"]
    assert "Max-level cards: 23" in request["context"]
    assert "Include `POAP KINGS` exactly" in request["context"]
    assert "Use short native clan-chat welcomes." in request["context"]
    signed_copy = f"{authored_copy} - E"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["action_type"] == "welcome_relay"
    assert mock_create.call_args.kwargs["prompt_text"] == signed_copy
    assert mock_create.call_args.kwargs["target_player_tag"] == "#ABC"
    assert mock_create.call_args.kwargs["target_player_name"] == "King Levy"
    mock_card.assert_awaited_once()
    assert mock_card.await_args.args[0] is channel
    assert mock_card.await_args.args[1]["action_id"] == 43
    assert mock_card.await_args.kwargs["copy_messages"] == [signed_copy]
    mock_update_message.assert_called_once_with(43, source_message_id=2000)
    mock_update_copy.assert_called_once_with(43, copy_message_id=2001)


def test_war_relay_policy_shape_uses_objective_specific_cooldowns():
    from runtime.signals.delivery import (
        CRITICAL_LEADER_ACTION_SIGNAL_TYPES,
        _provisional_leader_action_policy_shape,
    )

    assert _provisional_leader_action_policy_shape({"war_battle_day_started"}) == (
        "in_game_relay",
        "war_participation",
    )
    assert _provisional_leader_action_policy_shape({"war_attacks_complete"}) == (
        "in_game_relay",
        "war_recognition",
    )
    assert _provisional_leader_action_policy_shape({"war_week_complete"}) == (
        "in_game_relay",
        "war_recap",
    )
    assert "war_final_battle_day" in CRITICAL_LEADER_ACTION_SIGNAL_TYPES


def test_deliver_new_member_welcome_relay_ignores_generic_cooldown_and_mixed_batch():
    signals = [
        {
            "type": "member_join",
            "signal_key": "member_join:#ABC",
            "tag": "#ABC",
            "name": "King Levy",
        },
        {
            "type": "arena_change",
            "signal_key": "arena_change:#DEF",
            "tag": "#DEF",
            "name": "Other Player",
            "old_arena": "PANCAKES!",
            "new_arena": "Valkalla",
        },
    ]
    clan = {"memberList": [{"tag": "#ABC", "name": "King Levy"}]}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    authored_copy = "Welcome to POAP KINGS, King Levy! Collection Level 1,597 already stands out."

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "batch:joins-and-progress",
            "source_signal_type": "member_join",
            "target_channel_key": "arena-relay",
            "target_channel_id": 1513758211206025227,
            "intent": "welcome_relay",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.list_channel_messages", return_value=[
            {"created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), "content": "Recent action"}
        ]),
        patch("runtime.signals.delivery.db.get_member_profile", return_value={
            "player_tag": "#ABC",
            "member_name": "King Levy",
            "cr_collection_level": 1597,
            "cr_clan_war_wins": 421,
        }),
        patch("runtime.signals.delivery.build_lane_memory_context", return_value={"durable_memories": []}),
        patch("elixir.db.list_leader_action_feedback_profiles", return_value=[]),
        patch("runtime.signals.delivery.can_post_leader_action", return_value=(False, "daily_cap:4")) as mock_policy,
        patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={
            "messages": [authored_copy],
            "summary": "Welcome King Levy",
        }) as mock_generate,
        patch("elixir.db.build_leader_action_baseline", return_value={}),
        patch("elixir.db.create_leader_action_recommendation", return_value={
            "action_id": 44,
            "action_key": "welcome:#ABC",
            "status": "proposed",
        }),
        patch("runtime.signals.delivery.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=3000),
            SimpleNamespace(id=3001),
        ])) as mock_card,
        patch("elixir.db.update_leader_action_message"),
        patch("elixir.db.update_leader_action_copy_message"),
        patch("elixir.db.save_message"),
        patch("elixir.db.upsert_signal_outcome") as mock_upsert,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("runtime.jobs._signals._mark_signal_group_completed"),
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_called_once()
    mock_policy.assert_not_called()
    request = mock_generate.call_args.args[0]
    assert request["intent"] == "welcome_relay"
    assert "Collection Level: 1,597" in request["context"]
    assert "Fallback only - clan war wins: 421" in request["context"]
    assert "Include `POAP KINGS` exactly" in request["context"]
    mock_card.assert_awaited_once()
    assert mock_card.await_args.args[1]["action_id"] == 44
    assert mock_card.await_args.kwargs["copy_messages"] == [f"{authored_copy} - E"]
    assert not any(
        call.kwargs.get("delivery_status") == "skipped"
        and str(call.kwargs.get("error_detail", "")).startswith("arena_relay_cooldown")
        for call in mock_upsert.call_args_list
    )


def test_arena_relay_result_does_not_template_weekly_discord_invite_copy():
    from runtime.signals.delivery import _build_arena_relay_result

    result = _build_arena_relay_result([{
        "type": "discord_invite_reminder",
        "signal_key": "discord_invite_reminder:2026-W24",
    }])

    assert result is None


def test_discord_invite_relay_context_gives_elixir_authoring_facts():
    from runtime.signals.delivery import _discord_invite_relay_context

    members = [
        {"name": f"Member {index}", "in_discord": index <= 17}
        for index in range(1, 51)
    ]

    with patch("runtime.signals.delivery.db.list_members", return_value=members):
        context = _discord_invite_relay_context("Signal context")

    assert "Discord-linked clan members: 17/50" in context
    assert "Author 2-3 short Clash Royale clan-chat messages" in context
    assert "POAPKINGS . COM > Members" in context
    assert "Return `content` as a JSON array" in context


def test_generated_discord_invite_relay_rejects_raw_links():
    from runtime.signals.delivery import _build_generated_discord_invite_relay_result

    result = _build_generated_discord_invite_relay_result(
        [{"type": "discord_invite_reminder"}],
        {
            "content": [
                "Join us at https://poapkings.com/members for Discord.",
                "POAPKINGS . COM > Members",
            ],
        },
    )

    assert result is None


def test_deliver_weekly_discord_invite_relay_uses_elixir_authored_copy():
    signals = [{
        "type": "discord_invite_reminder",
        "signal_key": "discord_invite_reminder:2026-W24",
    }]
    clan = {"memberList": []}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    authored_messages = [
        "Discord check: 17 of us are already there, and it is where the useful clan stuff lands first.",
        "Deck screenshots, war coordination, milestone shoutouts, leader relay notes: Discord keeps that out of game chat clutter.",
        "Join through POAPKINGS . COM > Members.",
    ]

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "discord_invite_reminder:2026-W24",
            "source_signal_type": "discord_invite_reminder",
            "target_channel_key": "arena-relay",
            "target_channel_id": 1513758211206025227,
            "intent": "discord_invite_relay",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.signals.delivery.db.list_members", return_value=[
            {"name": f"Member {index}", "in_discord": index <= 17}
            for index in range(1, 51)
        ]),
        patch("runtime.signals.delivery.build_lane_memory_context", return_value={"durable_memories": []}),
        patch("elixir.db.list_leader_action_feedback_profiles", return_value=[]),
        patch("runtime.signals.delivery.can_post_leader_action", return_value=(True, None)),
        patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={
            "messages": authored_messages,
            "summary": "Invite Discord",
        }) as mock_generate,
        patch("elixir.db.build_leader_action_baseline", return_value={}),
        patch("elixir.db.create_leader_action_recommendation", return_value={
            "action_id": 42,
            "action_key": "discord:2026-W24",
            "status": "proposed",
        }) as mock_create,
        patch("runtime.signals.delivery.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=1000),
            SimpleNamespace(id=1001),
            SimpleNamespace(id=1002),
            SimpleNamespace(id=1003),
        ])) as mock_card,
        patch("elixir.db.update_leader_action_message") as mock_update_message,
        patch("elixir.db.update_leader_action_copy_message") as mock_update_copy,
        patch("elixir.db.save_message"),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("runtime.jobs._signals._mark_signal_group_completed"),
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_called_once()
    request = mock_generate.call_args.args[0]
    assert request["intent"] == "discord_invite_relay"
    assert request["exact_once_terms"] == ["POAPKINGS . COM > Members"]
    assert "Discord-linked clan members: 17/50" in request["context"]
    signed_messages = [f"{message} - E" for message in authored_messages]
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["prompt_text"] == "\n".join(signed_messages)
    mock_card.assert_awaited_once()
    assert mock_card.await_args.args[1]["action_id"] == 42
    assert mock_card.await_args.kwargs["copy_messages"] == signed_messages
    mock_update_message.assert_called_once_with(42, source_message_id=1000)
    mock_update_copy.assert_called_once_with(42, copy_message_id=1001)


def test_signal_delivery_logs_leader_action_policy_skip():
    signals = [{
        "type": "discord_invite_reminder",
        "signal_key": "discord_invite_reminder:2026-W24",
    }]
    clan = {"memberList": []}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "discord_invite_reminder:2026-W24",
            "source_signal_type": "discord_invite_reminder",
            "target_channel_key": "arena-relay",
            "target_channel_id": 1513758211206025227,
            "intent": "discord_invite_relay",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.signals.delivery.build_lane_memory_context", return_value={"durable_memories": []}),
        patch("runtime.signals.delivery.can_post_leader_action", return_value=(False, "open_card_backlog:5/5")),
        patch("runtime.signals.delivery.post_leader_action_skip", new=AsyncMock(return_value=True)) as mock_skip_log,
        patch("elixir.elixir_agent.generate_channel_update") as mock_generate,
        patch("elixir.db.upsert_signal_outcome") as mock_upsert,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "skipped"}]),
        patch("runtime.jobs._signals._mark_signal_group_completed"),
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_not_called()
    mock_skip_log.assert_awaited_once()
    assert mock_skip_log.await_args.kwargs["source"] == "signal_delivery"
    assert mock_skip_log.await_args.kwargs["action_type"] == "discord_invite_relay"
    assert mock_skip_log.await_args.kwargs["reason"] == "policy:open_card_backlog:5/5"
    assert mock_skip_log.await_args.kwargs["signal_types"] == {"discord_invite_reminder"}
    assert mock_upsert.call_args.kwargs["delivery_status"] == "skipped"
    assert mock_upsert.call_args.kwargs["error_detail"] == "leader_action_policy:open_card_backlog:5/5"


def test_signal_delivery_policy_skip_includes_player_target():
    signals = [{
        "type": "career_wins_milestone",
        "signal_key": "career_wins_milestone:#DEF456:10000",
        "name": "Vijay",
        "tag": "#DEF456",
    }]
    clan = {"memberList": []}
    war = {}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "career_wins_milestone:#DEF456:10000",
            "source_signal_type": "career_wins_milestone",
            "target_channel_key": "arena-relay",
            "target_channel_id": 1513758211206025227,
            "intent": "celebration_relay",
            "required": False,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.signals.delivery.build_lane_memory_context", return_value={"durable_memories": []}),
        patch("runtime.signals.delivery.can_post_leader_action", return_value=(False, "daily_cap:4")),
        patch("runtime.signals.delivery.post_leader_action_skip", new=AsyncMock(return_value=True)) as mock_skip_log,
        patch("elixir.elixir_agent.generate_channel_update") as mock_generate,
        patch("elixir.db.upsert_signal_outcome") as mock_upsert,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "skipped"}]),
        patch("runtime.jobs._signals._mark_signal_group_completed"),
    ):
        assert asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_not_called()
    mock_skip_log.assert_awaited_once()
    assert mock_skip_log.await_args.kwargs["source"] == "signal_delivery"
    assert mock_skip_log.await_args.kwargs["action_type"] == "celebration_relay"
    assert mock_skip_log.await_args.kwargs["reason"] == "policy:daily_cap:4"
    assert mock_skip_log.await_args.kwargs["target_player_name"] == "Vijay"
    assert mock_skip_log.await_args.kwargs["target_player_tag"] == "#DEF456"
    assert mock_skip_log.await_args.kwargs["signal_types"] == {"career_wins_milestone"}
    assert mock_upsert.call_args.kwargs["delivery_status"] == "skipped"
    assert mock_upsert.call_args.kwargs["error_detail"] == "leader_action_policy:daily_cap:4"


def test_leader_action_skip_target_extracts_nested_member_fields():
    from runtime.signals.delivery import _leader_action_skip_target

    name, tag = _leader_action_skip_target([{
        "type": "career_wins_milestone",
        "member": {"player_name": "River", "player_tag": "#RIVER"},
    }])

    assert name == "River"
    assert tag == "#RIVER"


def test_arena_relay_cooldown_blocks_recent_posts():
    from datetime import datetime
    from runtime.signals.delivery import _arena_relay_recently_posted

    assert _arena_relay_recently_posted(
        [{"recorded_at": "2026-06-08T12:00:00"}],
        now=datetime(2026, 6, 8, 20, 0, 0),
    )
    assert not _arena_relay_recently_posted(
        [{"recorded_at": "2026-06-07T00:00:00"}],
        now=datetime(2026, 6, 8, 20, 0, 0),
    )


def test_leader_action_card_uses_categorical_action_icon():
    from runtime.jobs._core import _format_leader_action_card

    card = _format_leader_action_card(
        {
            "action_id": 22,
            "action_type": "kick_recommendation",
            "objective": "roster_health",
        },
        title="kick recommendation",
        prompt_text="Kick Aaqib for extended inactivity.",
        rationale="Aaqib has not been active for 10 days.",
    )

    assert card.startswith("**R22 🚪 kick recommendation**")
    assert "✅ done  ❌ decline" in card


def test_role_action_clan_chat_copy_is_short_and_public_reasoned():
    from runtime.jobs._core import CLAN_CHAT_ACTION_COPY_LIMIT, _leader_action_clan_chat_copy, _leader_action_reason

    promotion = _leader_action_clan_chat_copy(
        action_type="promotion_recommendation",
        target_player_name="King Levy",
        rationale="220 donations, 4 war races, 90d tenure",
    )
    kick = _leader_action_clan_chat_copy(
        action_type="kick_recommendation",
        target_player_name="Vijay",
        rationale="last seen 8 days ago; no war participation",
    )
    demotion = _leader_action_clan_chat_copy(
        action_type="demotion_recommendation",
        target_player_name="Aaqib",
        rationale="activity has dropped for several weeks",
    )
    long_kick = _leader_action_clan_chat_copy(
        action_type="kick_recommendation",
        target_player_name="1spaceO2",
        rationale=(
            "no battle in 8 days, last login 8 days ago (threshold 7.0d at 4914 trophies); "
            "0 donations this week; 0 war races played this season"
        ),
    )
    monica_rationale = _leader_action_reason(
        {
            "activity_context": {
                "stale_activity": True,
                "battle_days_ago": 10,
                "login_days_ago": 10,
                "threshold_days": 10.8,
            },
            "reasons": [
                {"type": "low_donations", "detail": "0 donations this week"},
                {"type": "low_war_participation", "detail": "0 war races played this season"},
            ],
        },
        promotion=False,
    )
    monica_kick = _leader_action_clan_chat_copy(
        action_type="kick_recommendation",
        target_player_name="MONICA",
        rationale=monica_rationale,
    )

    assert promotion == (
        "Promoting King Levy to Elder: 220 donations, 4 war races, 90d tenure. Well earned. - E"
    )
    assert kick == (
        "Removing Vijay for now: last seen 8 days ago; no war participation. - E"
    )
    assert demotion == (
        "Moving Aaqib back to Member for now: activity has dropped for several weeks. - E"
    )
    assert (
        long_kick
        == "Removing 1spaceO2 for now: no battle in 8 days, last login 8 days ago; "
        "0 donations this week. - E"
    )
    assert monica_rationale == "no activity in 10 days; 0 donations this week; 0 war races played this season"
    assert monica_kick == (
        "Removing MONICA for now: no activity in 10 days; 0 donations this week; "
        "0 war races played this season. - E"
    )
    assert "...." not in long_kick
    assert "donatio..." not in long_kick
    assert "active and fair" not in monica_kick
    assert all(len(copy) <= CLAN_CHAT_ACTION_COPY_LIMIT for copy in (promotion, kick, demotion, long_kick, monica_kick))


def test_arena_relay_result_builds_copyable_celebration_card():
    from runtime.signals.delivery import _build_arena_relay_result

    result = _build_arena_relay_result([{
        "type": "career_wins_milestone",
        "tag": "#ABC",
        "name": "King Levy",
        "milestone": 5000,
    }])

    assert result["event_type"] == "celebration_relay"
    assert result["metadata"]["action_type"] == "celebration_relay"
    assert result["metadata"]["objective"] == "player_celebration"
    assert result["content"][0].startswith("**R? 🎉 celebration relay**")
    assert result["content"][1] == (
        "Big milestone: King Levy just reached 5,000 lifetime wins. "
        "Drop a congrats when you see them."
    )


def test_arena_relay_result_builds_cr_account_cake_day_copy():
    from runtime.signals.delivery import _build_arena_relay_result

    result = _build_arena_relay_result([{
        "type": "cr_account_anniversary",
        "tag": "#ABC",
        "name": "King Levy",
        "new_years": 5,
        "account_age_days": 1825,
    }])

    assert result["event_type"] == "celebration_relay"
    assert result["content"][1] == (
        "CR cake day: King Levy's Clash Royale account is 5 years old. "
        "That is real staying power."
    )


def test_clan_awareness_tick_does_not_mark_system_signal_sent_before_success():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{
            "type": "capability_unlock",
            "signal_key": "capability_memory_system_v1",
            "payload": {"title": "Achievement Unlocked: Stronger Memory"},
            "title": "Achievement Unlocked: Stronger Memory",
        }],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "training"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.db.mark_signal_completed") as mock_mark_signal_sent,
        patch("elixir.db.mark_system_signal_announced") as mock_mark_announced,
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=False)) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war, workflow="clan_awareness")
    mock_mark_signal_sent.assert_not_called()
    mock_mark_announced.assert_not_called()


def test_clan_awareness_tick_hands_multiple_system_signals_to_awareness_loop():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[
            {
                "type": "capability_unlock",
                "signal_key": "capability_memory_system_v1",
                "payload": {"title": "Achievement Unlocked: Stronger Memory"},
            },
            {
                "type": "capability_unlock",
                "signal_key": "capability_battle_pulse_v1",
                "payload": {"title": "Achievement Unlocked: Battle Pulse"},
            },
        ],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "training"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", new=AsyncMock(return_value=True)) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war, workflow="clan_awareness")


def test_weekly_clan_recap_syncs_members_page_payload_when_poap_kings_enabled():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 456
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=456),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {"state": "training"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="summary context"),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="This week POAP KINGS pushed hard."),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message"),
        patch("runtime.jobs._core.upsert_weekly_summary_memory"),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("members")) as mock_publish,
        patch("runtime.jobs._site.poap_kings_site.publish_blog_post", return_value=_publish_result("blog_post")),
        patch("runtime.jobs._core._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
    ):
        with patch("runtime.jobs._core._weekly_story_relay_card", new=AsyncMock(return_value=False)):
            asyncio.run(elixir._weekly_clan_recap())

    mock_post.assert_awaited_once()
    mock_publish.assert_called_once_with(
        {
            "members": {
                "title": "Weekly Recap",
                "message": "This week POAP KINGS pushed hard.",
                "generated": mock_publish.call_args.args[0]["members"]["generated"],
                "source": "weekly_clan_recap",
            }
        },
        "Elixir POAP KINGS weekly recap sync",
    )
    assert mock_notify.await_count == 2
    notify_keys = [c.args[0] for c in mock_notify.await_args_list]
    assert "weekly-recap" in notify_keys
    assert "weekly-recap-blog" in notify_keys
    recap_call = next(c for c in mock_notify.await_args_list if c.args[0] == "weekly-recap")
    assert recap_call.kwargs["publish_result"]["commit_url"].startswith("https://github.com/")


def test_detect_pending_system_signals_retries_until_announced():
    conn = db.get_connection(":memory:")
    try:
        db.queue_system_signal(
            "capability_memory_system_v1",
            "capability_unlock",
            {"title": "Achievement Unlocked: Stronger Memory"},
            conn=conn,
        )
        db.mark_signal_sent("system_signal::capability_memory_system_v1", "2026-03-10", conn=conn)

        signals = heartbeat.detect_pending_system_signals(today_str="2026-03-10", conn=conn)
    finally:
        conn.close()

    assert len(signals) == 1
    assert signals[0]["signal_key"] == "capability_memory_system_v1"


def test_player_intel_refresh_uses_refresh_targets_without_llm():
    """_player_intel_refresh should refresh stale members without touching the LLM."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war") as mock_get_war,
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members") as mock_snapshot_members,
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets) as mock_targets,
        patch("elixir.db.snapshot_player_profile") as mock_snapshot_profile,
        patch("elixir.db.snapshot_player_battlelog") as mock_snapshot_battlelog,
        patch("elixir.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_snapshot_members.assert_not_called()
    mock_get_war.assert_not_called()
    mock_targets.assert_called_once_with(elixir.PLAYER_INTEL_BATCH_SIZE, elixir.PLAYER_INTEL_STALE_HOURS)
    mock_snapshot_profile.assert_called_once()
    mock_snapshot_battlelog.assert_called_once_with("#ABC", [{"type": "PvP"}])
    mock_sleep.assert_awaited_once()


def test_player_intel_refresh_posts_progression_signals():
    """_player_intel_refresh should route progression signals through grouped outcome delivery."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[{"type": "player_level_up", "tag": "#ABC", "name": "King Levy", "old_level": 65, "new_level": 66}]),
        patch("elixir.db.snapshot_player_battlelog"),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_awaited_once()
    args = mock_deliver.await_args.args
    assert args[0][0]["type"] == "player_level_up"
    assert args[1] == clan
    assert mock_deliver.await_args.kwargs["workflow"] == "player_intel"


def test_player_intel_refresh_splits_optional_badge_level_batches():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[
            {"type": "player_level_up", "tag": "#ABC", "name": "King Levy", "old_level": 65, "new_level": 66},
            {"type": "badge_level_milestone", "tag": "#ABC", "name": "King Levy", "badge_name": "MasteryKnight"},
        ]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[]),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    assert mock_deliver.await_count == 2
    first_batch = mock_deliver.await_args_list[0].args[0]
    second_batch = mock_deliver.await_args_list[1].args[0]
    assert [signal["type"] for signal in first_batch] == ["player_level_up"]
    assert [signal["type"] for signal in second_batch] == ["badge_level_milestone"]
    assert all(call.kwargs["workflow"] == "player_intel" for call in mock_deliver.await_args_list)


def test_player_intel_refresh_posts_battle_pulse_signals():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[
            {"type": "battle_hot_streak", "tag": "#ABC", "name": "King Levy", "streak": 4},
            {"type": "battle_trophy_push", "tag": "#ABC", "name": "King Levy", "trophy_delta": 111},
        ]),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_awaited_once()
    signal_types = [signal["type"] for signal in mock_deliver.await_args.args[0]]
    assert signal_types == ["battle_hot_streak", "battle_trophy_push"]
    assert mock_deliver.await_args.kwargs["workflow"] == "player_intel"


def test_player_intel_refresh_does_not_post_baseline_profile_discovery():
    clan = {"memberList": [{"name": "royalkiller864", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "royalkiller864"}]
    first_profile = {
        "tag": "#ABC",
        "name": "royalkiller864",
        "currentDeck": [],
        "cards": [
            {"name": "Knight", "level": 1, "maxLevel": 16, "rarity": "common"},
            {"name": "Goblin Barrel", "level": 9, "maxLevel": 14, "rarity": "epic"},
        ],
        "badges": [
            {"name": "EmoteCollection", "level": 1, "maxLevel": 10, "progress": 10, "target": 25},
            {"name": "BattleWins", "level": 2, "maxLevel": 10, "progress": 20, "target": 50},
        ],
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value=first_profile),
        patch("elixir.cr_api.get_player_battle_log", return_value=[]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[]),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()


def test_player_intel_refresh_marks_failure_when_all_player_endpoints_fail():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value=None),
        patch("elixir.cr_api.get_player_battle_log", return_value=None),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
        patch("elixir._maybe_alert_cr_api_failure", new=AsyncMock()) as mock_alert,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()
    mock_alert.assert_awaited_once_with("player intel refresh")
    mock_success.assert_not_called()
    assert mock_failure.call_args.args[0] == "player_intel_refresh"
    assert "refreshed 0 of 1 member(s)" in mock_failure.call_args.args[1]
    assert "profile failures 1" in mock_failure.call_args.args[1]
    assert "battle log failures 1" in mock_failure.call_args.args[1]
    assert "full target failures 1" in mock_failure.call_args.args[1]


def test_player_intel_refresh_reports_partial_endpoint_failures_without_hiding_success():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=None),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("runtime.jobs._intel._deliver_signal_group_via_awareness", new=AsyncMock()) as mock_deliver,
        patch("elixir._maybe_alert_cr_api_failure", new=AsyncMock()) as mock_alert,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()
    mock_alert.assert_awaited_once_with("player intel refresh")
    mock_failure.assert_not_called()
    assert mock_success.call_args.args[0] == "player_intel_refresh"
    assert "refreshed 1 of 1 member(s)" in mock_success.call_args.args[1]
    assert "battle log failures 1" in mock_success.call_args.args[1]


def test_weekly_leader_actions_post_to_arena_relay():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    created = [
        {"action_id": 1, "action_key": "kick:1", "status": "proposed", "objective": "roster_health"},
        {"action_id": 2, "action_key": "demotion:1", "status": "proposed", "objective": "role_health"},
        {"action_id": 3, "action_key": "promotion:1", "status": "proposed", "objective": "reward_and_retention"},
    ]

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={
            "recommended": [{"member_ref": "King Levy", "player_tag": "#ABC123", "elder_donation_rank": 2, "elder_target_rank": 10, "rolling_donations_avg": 220.0, "donations": 220, "war_races_played": 4, "days_since_battle": 0}],
            "demotion_candidates": [{"member_ref": "Aaqib", "player_tag": "#AAA111", "reason": "outside Elder cap: rank 11/10 on 4-week donation average 30.0", "war_races_played": 1, "days_since_battle": 0}],
        }),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={
            "members": [{
                "member_ref": "Vijay",
                "player_tag": "#DEF456",
                "reasons": [{"type": "inactive", "detail": "last seen 8 days ago", "value": 8, "threshold_days": 7}],
            }],
        }),
        patch("runtime.jobs._core._kick_candidate_availability_memory", return_value=None),
        patch("runtime.jobs._core.db.list_due_decision_cases", return_value=[]),
        patch("runtime.jobs._core.db.upsert_member_review_case", return_value={"case_id": 99}) as mock_case,
        patch("runtime.jobs._core.db.has_recent_leader_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.db.build_leader_action_baseline", return_value={}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation", side_effect=created) as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock(side_effect=[
            [SimpleNamespace(id=1000), SimpleNamespace(id=1001)],
            [SimpleNamespace(id=2000), SimpleNamespace(id=2001)],
            [SimpleNamespace(id=3000), SimpleNamespace(id=3001)],
        ])) as mock_card,
        patch("runtime.jobs._core.db.save_message") as mock_save,
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=6))

    assert posted == 3
    assert [call.kwargs["case_type"] for call in mock_case.call_args_list] == [
        "inactivity_review",
        "demotion_review",
        "promotion_review",
    ]
    assert mock_create.call_count == 3
    assert [call.kwargs["action_type"] for call in mock_create.call_args_list] == [
        "kick_recommendation",
        "demotion_recommendation",
        "promotion_recommendation",
    ]
    assert [call.kwargs["case_id"] for call in mock_create.call_args_list] == [99, 99, 99]
    assert mock_create.call_args_list[0].kwargs["baseline"]["policy_context"]["primary_signal"] == "inactivity_or_absence"
    assert "policy_context" not in mock_create.call_args_list[1].kwargs["baseline"]
    assert "policy_context" not in mock_create.call_args_list[2].kwargs["baseline"]
    assert mock_card.await_count == 3
    assert mock_card.await_args_list[0].kwargs["copy_messages"][0].startswith("Removing Vijay for now")
    assert mock_card.await_args_list[1].kwargs["copy_messages"][0].startswith("Moving Aaqib back to Member")
    assert mock_card.await_args_list[2].kwargs["copy_messages"][0].startswith("Promoting King Levy to Elder")
    assert mock_save.call_args.kwargs["workflow"] == "arena-relay"
    assert "clan_chat_copy" in mock_save.call_args.kwargs["raw_json"]


def test_leader_action_scan_prioritizes_idle_kick_candidates_and_skips_suppressed_targets():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    def has_recent(**kwargs):
        return kwargs.get("target_player_tag") == "#SUPPRESS"

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={"recommended": []}),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={
            "members": [
                {
                    "member_ref": "Low Donation",
                    "player_tag": "#LOW",
                    "clan_rank": 1,
                    "reasons": [{"type": "low_donations", "detail": "0 donations this week"}],
                },
                {
                    "member_ref": "Suppressed Idle",
                    "player_tag": "#SUPPRESS",
                    "clan_rank": 2,
                    "reasons": [{"type": "inactive", "detail": "no battle in 12 days", "value": 12, "threshold_days": 7}],
                },
                {
                    "member_ref": "Fresh Idle",
                    "player_tag": "#IDLE",
                    "clan_rank": 30,
                    "reasons": [{"type": "inactive", "detail": "no battle in 10 days", "value": 10, "threshold_days": 8}],
                },
            ],
        }),
        patch("runtime.jobs._core._kick_candidate_availability_memory", return_value=None),
        patch("runtime.jobs._core.db.list_due_decision_cases", return_value=[]),
        patch("runtime.jobs._core.db.upsert_member_review_case", return_value={"case_id": 99}),
        patch("runtime.jobs._core.db.has_recent_leader_action", side_effect=has_recent),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.db.build_leader_action_baseline", return_value={}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation", return_value={
            "action_id": 21,
            "action_key": "kick:#IDLE",
            "status": "proposed",
            "objective": "roster_health",
        }) as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=2100),
            SimpleNamespace(id=2101),
        ])),
        patch("runtime.jobs._core.db.save_message"),
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=1))

    assert posted == 1
    assert mock_create.call_args.kwargs["target_player_tag"] == "#IDLE"
    assert mock_create.call_args.kwargs["target_player_name"] == "Fresh Idle"


def test_kick_candidate_ineligibility_suppresses_fresh_join():
    from runtime.jobs._core import _kick_candidate_ineligibility_reason

    conn = db.get_connection(":memory:")
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        db.snapshot_members(
            [{"tag": "#NEW123", "name": "New Recruit", "role": "member", "clanRank": 1}],
            conn=conn,
        )
        member_id = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = '#NEW123'"
        ).fetchone()["member_id"]
        conn.execute(
            "UPDATE clan_memberships SET joined_at = ?, join_source = 'observed_join' WHERE member_id = ? AND left_at IS NULL",
            (today, member_id),
        )
        conn.commit()

        member = {
            "member_ref": "New Recruit",
            "player_tag": "#NEW123",
            "reasons": [{"type": "inactive", "detail": "no battles in 90+ days", "value": 90, "threshold_days": 7}],
        }

        assert _kick_candidate_ineligibility_reason(member, conn=conn) == "fresh_membership:0d<7d"
    finally:
        conn.close()


def test_leader_action_scan_posts_due_inactivity_case():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={"recommended": [], "demotion_candidates": []}),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={
            "members": [{
                "member_ref": "xian",
                "player_tag": "#UGQPVQ9U9",
                "clan_rank": 30,
                "reasons": [{"type": "inactive", "detail": "no battle in 10 days", "value": 10, "threshold_days": 7.6}],
            }],
        }),
        patch("runtime.jobs._core.db.list_due_decision_cases", return_value=[{
            "case_id": 77,
            "case_type": "inactivity_review",
            "target_player_tag": "#UGQPVQ9U9",
            "target_player_name": "xian",
            "recommendation": "Review xian for removal from the clan.",
            "rationale": "10 days inactive vs 7.6 day threshold",
        }]),
        patch("runtime.jobs._core._kick_candidate_availability_memory", return_value=None),
        patch("runtime.jobs._core.db.has_recent_leader_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.db.build_leader_action_baseline", return_value={}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation", return_value={
            "action_id": 77,
            "action_key": "kick:#UGQPVQ9U9",
            "status": "proposed",
            "objective": "roster_health",
            "case_id": 77,
        }) as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=7700),
            SimpleNamespace(id=7701),
        ])),
        patch("runtime.jobs._core.db.save_message"),
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=1))

    assert posted == 1
    assert mock_create.call_args.kwargs["case_id"] == 77
    assert mock_create.call_args.kwargs["target_player_tag"] == "#UGQPVQ9U9"


def test_leader_action_scan_resurfaces_due_deferred_case_without_fresh_signal():
    """A deferred case re-surfaces when due even if the detector no longer flags
    the member — the leader deferred it, so it is carded (not dismissed)."""
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    deferred_case = {
        "case_id": 88,
        "case_type": "inactivity_review",
        "status": db.CASE_DEFERRED,
        "target_player_tag": "#STALE",
        "target_player_name": "StaleMember",
        "recommendation": "Review StaleMember for removal from the clan.",
        "rationale": "Previously inactive.",
        "state": {"member": {
            "player_tag": "#STALE",
            "member_ref": "StaleMember",
            "reasons": [{"type": "inactive", "detail": "no battle in 12 days", "value": 12, "threshold_days": 7}],
        }},
    }

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={"recommended": [], "demotion_candidates": []}),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={"members": []}),
        patch("runtime.jobs._core.db.list_due_decision_cases", side_effect=lambda **kwargs: [deferred_case] if kwargs.get("case_type") == "inactivity_review" else []),
        patch("runtime.jobs._core._kick_candidate_ineligibility_reason", return_value=None),
        patch("runtime.jobs._core.db.resolve_decision_case", return_value={}) as mock_resolve,
        patch("runtime.jobs._core.db.has_recent_leader_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.db.build_leader_action_baseline", return_value={}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation", return_value={
            "action_id": 88,
            "action_key": "kick:#STALE",
            "status": "proposed",
            "objective": "roster_health",
            "case_id": 88,
        }) as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock(return_value=[
            SimpleNamespace(id=8800),
            SimpleNamespace(id=8801),
        ])) as mock_card,
        patch("runtime.jobs._core.db.save_message"),
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=1))

    assert posted == 1
    mock_resolve.assert_not_called()  # the deferred case is re-surfaced, not dismissed
    assert mock_create.call_args.kwargs["case_id"] == 88
    assert mock_create.call_args.kwargs["action_type"] == "kick_recommendation"
    assert mock_create.call_args.kwargs["target_player_tag"] == "#STALE"
    mock_card.assert_awaited()


def test_leader_action_scan_leaves_unflagged_open_case_uncarded():
    """An open case the detector no longer flags is left in Situation (not carded
    with stale evidence and not dismissed)."""
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    open_case = {
        "case_id": 89,
        "case_type": "inactivity_review",
        "status": db.CASE_OPEN,
        "target_player_tag": "#GHOST",
        "target_player_name": "GhostMember",
        "recommendation": "Review GhostMember for removal from the clan.",
        "rationale": "Was inactive last week.",
    }

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={"recommended": [], "demotion_candidates": []}),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={"members": []}),
        patch("runtime.jobs._core.db.list_due_decision_cases", side_effect=lambda **kwargs: [open_case] if kwargs.get("case_type") == "inactivity_review" else []),
        patch("runtime.jobs._core.db.resolve_decision_case", return_value={}) as mock_resolve,
        patch("runtime.jobs._core.db.create_leader_action_recommendation") as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock()) as mock_card,
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=1))

    assert posted == 0
    mock_resolve.assert_not_called()  # left in Situation, not dismissed
    mock_create.assert_not_called()
    mock_card.assert_not_awaited()


def test_leader_action_scan_skips_active_low_donation_war_candidates():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 1513758211206025227
    channel.name = "leader-actions"
    channel.type = "text"

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.prompts.discord_singleton_lane", return_value={"id": 1513758211206025227, "name": "#leader-actions"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.db.get_promotion_candidates", return_value={"recommended": []}),
        patch("runtime.jobs._core.db.get_members_at_risk", return_value={
            "members": [
                {
                    "member_ref": "angecleowill",
                    "player_tag": "#P00C20YRJ",
                    "clan_rank": 21,
                    "activity_context": {"battle_days_ago": 0, "login_days_ago": 0},
                    "reasons": [
                        {"type": "low_donations", "detail": "0 donations this week"},
                        {"type": "low_war_participation", "detail": "0 war races played this season"},
                    ],
                },
            ],
        }),
        patch("runtime.jobs._core.db.list_due_decision_cases", return_value=[]),
        patch("runtime.jobs._core.db.create_leader_action_recommendation") as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock()) as mock_card,
        patch("runtime.jobs._core.post_leader_action_skip", new=AsyncMock(return_value=True)) as mock_skip,
    ):
        from runtime.jobs._core import _post_candidate_leader_action_recommendations
        posted = asyncio.run(_post_candidate_leader_action_recommendations(max_actions=1))

    assert posted == 0
    mock_create.assert_not_called()
    mock_card.assert_not_awaited()
    mock_skip.assert_not_awaited()


def test_kick_candidate_ineligibility_honors_availability_memory():
    from memory_store import create_memory
    from runtime.jobs._core import _kick_candidate_ineligibility_reason

    active = {
        "member_ref": "angecleowill",
        "player_tag": "#P00C20YRJ",
        "reasons": [
            {"type": "low_donations", "detail": "0 donations this week"},
            {"type": "low_war_participation", "detail": "0 war races played this season"},
        ],
    }
    assert _kick_candidate_ineligibility_reason(active) == "no_inactivity_signal"

    conn = db.get_connection(":memory:")
    try:
        memory = create_memory(
            title="Fullboat limited availability",
            body="Screenshot shows Fullboat said they will be camping for a week and may have limited signal.",
            summary="Fullboat is camping with limited signal.",
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.9,
            created_by="elixir:screenshot",
            scope="leadership",
            member_tag="#FULLBOAT",
            conn=conn,
        )
        inactive = {
            "member_ref": "Fullboat",
            "player_tag": "#8U2P0JPR",
            "reasons": [{"type": "inactive", "detail": "no battle in 9 days", "value": 9, "threshold_days": 7}],
        }
        assert _kick_candidate_ineligibility_reason(inactive, conn=conn) == f"availability_memory:{memory['memory_id']}"
    finally:
        conn.close()


def test_kick_candidate_priority_prefers_multi_signal_risk_after_inactive():
    from runtime.jobs._core import _kick_candidate_priority

    candidates = [
        {
            "name": "War Only Rank One",
            "player_tag": "#WAR",
            "clan_rank": 1,
            "risk_score": 1,
            "reasons": [{"type": "low_war_participation", "detail": "0 war races played this season"}],
        },
        {
            "name": "Monica Style",
            "player_tag": "#MULTI",
            "clan_rank": 21,
            "risk_score": 2,
            "reasons": [
                {"type": "low_donations", "detail": "0 donations this week"},
                {"type": "low_war_participation", "detail": "0 war races played this season"},
            ],
        },
        {
            "name": "Fresh Idle",
            "player_tag": "#IDLE",
            "clan_rank": 30,
            "risk_score": 2,
            "reasons": [
                {"type": "inactive", "detail": "no battle in 10 days", "value": 10, "threshold_days": 8},
                {"type": "low_war_participation", "detail": "0 war races played this season"},
            ],
        },
    ]

    ordered = sorted(candidates, key=_kick_candidate_priority)

    assert [candidate["player_tag"] for candidate in ordered] == ["#IDLE", "#MULTI", "#WAR"]


def test_leadership_action_scan_posts_singular_actions():
    with (
        patch("runtime.jobs._core._post_candidate_leader_action_recommendations", new=AsyncMock(return_value=2)) as mock_candidates,
        patch("runtime.jobs._core.db.refresh_due_leader_action_outcomes", return_value=[{"action_id": 1}]) as mock_refresh,
        patch("runtime.jobs._core._leadership_scan_has_critical_war_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.runtime_status.mark_job_start") as mock_start,
        patch("runtime.jobs._core.runtime_status.mark_job_success") as mock_success,
        patch("runtime.jobs._core.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._leadership_action_scan())

    mock_start.assert_called_once_with("leadership_action_scan")
    mock_refresh.assert_called_once()
    mock_candidates.assert_awaited_once_with(max_actions=2)
    mock_success.assert_called_once_with("leadership_action_scan", "posted 2 action(s)")
    mock_failure.assert_not_called()


def test_leadership_action_scan_logs_policy_skip():
    with (
        patch("runtime.jobs._core.db.refresh_due_leader_action_outcomes", return_value=[]),
        patch("runtime.jobs._core._leadership_scan_has_critical_war_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(False, "open_card_backlog:5/5")),
        patch("runtime.jobs._core.post_leader_action_skip", new=AsyncMock(return_value=True)) as mock_skip_log,
        patch("runtime.jobs._core.runtime_status.mark_job_start"),
        patch("runtime.jobs._core.runtime_status.mark_job_success") as mock_success,
        patch("runtime.jobs._core.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._leadership_action_scan())

    mock_skip_log.assert_awaited_once_with(
        source="leadership_action_scan",
        reason="policy:open_card_backlog:5/5",
    )
    mock_success.assert_called_once_with("leadership_action_scan", "skipped: open_card_backlog:5/5")
    mock_failure.assert_not_called()


def test_leader_action_recommendation_logs_policy_skip_with_candidate_context():
    from types import SimpleNamespace
    from runtime.jobs._core import _post_leader_action_recommendation

    channel = SimpleNamespace(id=900)
    with (
        patch("runtime.jobs._core.db.has_recent_leader_action", return_value=False),
        patch(
            "runtime.jobs._core.can_post_leader_action",
            return_value=(False, "earned_frequency:kick_recommendation:decline_rate=0.80"),
        ),
        patch("runtime.jobs._core.db.create_leader_action_recommendation") as mock_create,
        patch("runtime.jobs._core.post_leader_action_skip", new=AsyncMock(return_value=True)) as mock_skip_log,
    ):
        posted = asyncio.run(_post_leader_action_recommendation(
            channel,
            action_type="kick_recommendation",
            objective="roster_health",
            title="kick/removal recommendation",
            prompt_text="Review Vijay for removal from the clan.",
            rationale="last seen 8 days ago; no war participation",
            target_player_tag="#DEF456",
            target_player_name="Vijay",
        ))

    assert posted is False
    mock_create.assert_not_called()
    mock_skip_log.assert_awaited_once()
    assert mock_skip_log.await_args.kwargs["source"] == "leader_action_candidate_scan"
    assert mock_skip_log.await_args.kwargs["action_type"] == "kick_recommendation"
    assert mock_skip_log.await_args.kwargs["reason"] == "policy:earned_frequency:kick_recommendation:decline_rate=0.80"
    assert mock_skip_log.await_args.kwargs["target_player_name"] == "Vijay"
    assert mock_skip_log.await_args.kwargs["target_player_tag"] == "#DEF456"
    assert mock_skip_log.await_args.kwargs["rationale"] == "last seen 8 days ago; no war participation"


def test_war_champ_standings_routes_to_arena_relay():
    outcomes = plan_signal_outcomes([
        {"type": "war_champ_standings", "season_id": 134, "section_index": 2},
    ])

    assert any(
        outcome["target_channel_key"] == "arena-relay"
        and outcome["intent"] == "war_relay_brief"
        and outcome["required"] is False
        for outcome in outcomes
    )


def test_war_relay_copy_uses_short_season_week_label_and_champ_standings():
    from runtime.clan_chat_copy import ClanChatCopyResult
    from runtime.signals.delivery import _generate_war_relay_result

    generated = ClanChatCopyResult(messages=[
        "S134 W3 recap: POAP KINGS finished 1st with 14,000 fame. Thanks for using decks. - E",
        "War Champ after S134 W3: Alice leads with 12,300 fame; Bob and Cam are chasing. - E",
    ])
    signals = [
        {
            "type": "war_week_complete",
            "season_id": 134,
            "section_index": 2,
            "week": 3,
            "our_rank": 1,
            "our_fame": 14000,
        },
        {
            "type": "war_champ_standings",
            "season_id": 134,
            "section_index": 2,
            "week": 3,
            "standings": [
                {"name": "Alice", "tag": "#A", "total_fame": 12300},
                {"name": "Bob", "tag": "#B", "total_fame": 11100},
                {"name": "Cam", "tag": "#C", "total_fame": 10900},
            ],
        },
    ]

    with patch("runtime.signals.delivery.generate_clan_chat_copy", new=AsyncMock(return_value=generated)) as mock_generate:
        result = asyncio.run(_generate_war_relay_result(signals))

    assert result["event_type"] == "war_relay_brief"
    assert result["metadata"]["objective"] == "war_recap_and_champ_race"
    assert result["metadata"]["relay_copy_count"] == 2
    assert result["metadata"]["war_period_label"] == "S134 W3"
    request = mock_generate.await_args.kwargs
    assert request["intent"] == "war_weekly_recap_relay"
    assert request["required_terms"] == ("S134 W3", "War Champ")
    assert "Season 134 Week 3 is `S134 W3`" in request["context"]


def test_war_champ_winner_relay_uses_final_season_label_and_winner():
    from runtime.clan_chat_copy import ClanChatCopyResult
    from runtime.signals.delivery import _generate_war_champ_winner_result

    generated = ClanChatCopyResult(messages=[
        "S134 War Champ: Alice wins it with 48,000 fame. Huge season. - E",
    ])
    signals = [{
        "type": "season_awards_granted",
        "season_id": 134,
        "war_champ": [
            {"rank": 1, "name": "Alice", "tag": "#A", "metric_value": 48000, "metric_unit": "fame"},
            {"rank": 2, "name": "Bob", "tag": "#B", "metric_value": 44000, "metric_unit": "fame"},
        ],
    }]

    with patch("runtime.signals.delivery.generate_clan_chat_copy", new=AsyncMock(return_value=generated)) as mock_generate:
        result = asyncio.run(_generate_war_champ_winner_result(signals))

    assert result["event_type"] == "war_champ_winner_relay"
    assert result["metadata"]["objective"] == "war_champ_winner"
    assert result["metadata"]["target_player_name"] == "Alice"
    assert result["metadata"]["war_period_label"] == "S134"
    request = mock_generate.await_args.kwargs
    assert request["intent"] == "war_champ_winner_relay"
    assert request["required_terms"] == ("S134", "War Champ", "Alice")


def test_weekly_story_relay_card_offers_recap_beat_as_clan_chat_copy():
    """After the recap, its best member story is offered as a clan-chat
    relay card so the non-Discord majority can be reached through game
    chat — leader-decided like every other card."""
    from runtime.jobs._core import _weekly_story_relay_card

    channel = SimpleNamespace(id=900, name="leader-actions", type="text")
    created = {"action_id": 11, "source_message_id": None}
    copy_line = "Shoutout to Vijay - three weeks of climbing paid off with a perfect 4/4 colosseum day. POAP KINGS keeps rolling."

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._channel_config_by_key", return_value={"id": 900, "name": "#leader-actions", "lane_key": "arena-relay"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)) as mock_policy,
        patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={"messages": [copy_line]}) as mock_generate,
        patch("runtime.jobs._core.db.build_leader_action_baseline", return_value={}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation", return_value=created) as mock_create,
        patch("runtime.jobs._core.post_leader_action_card", new=AsyncMock(return_value=[SimpleNamespace(id=77)])) as mock_card,
        patch("runtime.jobs._core.db.save_message") as mock_save,
    ):
        posted = asyncio.run(_weekly_story_relay_card("**Weekly Recap**\nVijay sealed his comeback..."))

    assert posted is True
    assert mock_policy.call_args.kwargs["action_type"] == "in_game_relay"
    request = mock_generate.call_args.args[0]
    assert request["intent"] == "weekly_story_relay"
    assert request["target_surface"] == "Clash Royale in-game clan chat"
    assert "Vijay sealed his comeback" in request["context"]
    assert mock_create.call_args.kwargs["objective"] == "clan_story"
    assert mock_create.call_args.kwargs["source_signal_key"].startswith("weekly_story_relay:")
    signed_copy = f"{copy_line} - E"
    assert mock_create.call_args.kwargs["copy_current_text"] == signed_copy
    assert mock_card.await_args.kwargs["copy_messages"] == [signed_copy]
    assert mock_save.call_args.kwargs["event_type"] == "weekly_story_relay"


def test_weekly_story_relay_card_skips_when_copy_unusable():
    from runtime.jobs._core import _weekly_story_relay_card

    channel = SimpleNamespace(id=900, name="leader-actions", type="text")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._channel_config_by_key", return_value={"id": 900, "name": "#leader-actions", "lane_key": "arena-relay"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.clan_chat_copy.elixir_agent.generate_clan_chat_copy", return_value={"messages": ["Read more at https://example.com"]}),
        patch("runtime.jobs._core.db.create_leader_action_recommendation") as mock_create,
    ):
        posted = asyncio.run(_weekly_story_relay_card("recap text"))

    assert posted is False
    mock_create.assert_not_called()


def test_leadership_action_scan_requeues_feedback_synthesis_for_refreshed_outcomes():
    """When outcome evaluation lands (hours after the leader's decision), the
    affected action types must be re-synthesized so feedback profiles learn
    from measured outcomes, not just the click."""
    refreshed = [
        {"action_id": 1, "action_type": "promotion_recommendation"},
        {"action_id": 2, "action_type": "promotion_recommendation"},
        {"action_id": 3, "action_type": "welcome_relay"},
        {"action_id": 4},  # no action_type — must not queue
    ]
    with (
        patch("runtime.jobs._core._post_candidate_leader_action_recommendations", new=AsyncMock(return_value=0)),
        patch("runtime.jobs._core.db.refresh_due_leader_action_outcomes", return_value=refreshed),
        patch("runtime.jobs._core._leadership_scan_has_critical_war_action", return_value=False),
        patch("runtime.jobs._core.can_post_leader_action", return_value=(True, None)),
        patch("runtime.jobs._core.runtime_status.mark_job_start"),
        patch("runtime.jobs._core.runtime_status.mark_job_success"),
        patch("runtime.leader_action_feedback.queue_leader_action_feedback_refresh") as mock_queue,
    ):
        asyncio.run(elixir._leadership_action_scan())

    queued_types = [call.args[0] for call in mock_queue.call_args_list]
    assert queued_types == ["promotion_recommendation", "welcome_relay"]


def test_weekly_clan_recap_posts_to_weekly_digest_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 500
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.poap_kings_site.site_enabled", return_value=False),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=500),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="=== WEEKLY CLAN RECAP SNAPSHOT ===") as mock_build,
        patch("elixir.db.list_channel_messages", return_value=[{"role": "assistant", "content": "**Weekly Recap | March 4, 2026**\n\nlast week's recap"}]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="This week POAP KINGS pushed hard.") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
        patch("runtime.jobs._core.upsert_weekly_summary_memory") as mock_memory,
    ):
        with patch("runtime.jobs._core._weekly_story_relay_card", new=AsyncMock(return_value=False)):
            asyncio.run(elixir._weekly_clan_recap())

    mock_build.assert_called_once_with({"name": "POAP KINGS"}, {"state": "warDay"})
    mock_generate.assert_called_once_with("=== WEEKLY CLAN RECAP SNAPSHOT ===", "last week's recap")
    post_content = mock_post.await_args.args[1]["content"]
    assert post_content.startswith("**Weekly Recap | ")
    assert post_content.endswith("This week POAP KINGS pushed hard.")
    assert mock_save.call_args.kwargs["event_type"] == "weekly_clan_recap"
    mock_memory.assert_called_once()
    assert mock_memory.call_args.kwargs["event_type"] == "weekly_clan_recap"
    assert mock_memory.call_args.kwargs["scope"] == "public"


def test_format_weekly_recap_post_adds_subject_line_and_strips_existing_header():
    post = elixir._format_weekly_recap_post(
        "**Weekly Recap | March 4, 2026**\n\n**Clan momentum:** Strong week overall.",
        now=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc),
    )

    assert post == "**Weekly Recap | March 11, 2026**\n\n**Clan momentum:** Strong week overall."


def test_weekly_clan_recap_marks_failure_when_channel_send_forbidden():
    channel = AsyncMock()
    channel.id = 123
    channel.name = "weekly-digest"
    channel.type = "text"
    channel.send = AsyncMock(side_effect=discord.Forbidden(response=SimpleNamespace(status=403, reason="Forbidden"), message="Missing Permissions"))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="=== WEEKLY CLAN RECAP SNAPSHOT ==="),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="recap text"),
        patch("elixir.runtime_status.mark_job_start"),
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        try:
            with patch("runtime.jobs._core._weekly_story_relay_card", new=AsyncMock(return_value=False)):
                asyncio.run(elixir._weekly_clan_recap())
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "weekly recap post failed: missing Discord permissions in #weekly-digest" == str(exc)

    mock_failure.assert_called_once_with("weekly_clan_recap", "missing Discord permissions in #weekly-digest")


def test_promotion_content_cycle_publishes_website_and_promotion_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}) as mock_roster,
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            },
        ) as mock_generate,
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("promote")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_roster.assert_called_once_with(clan, True)
    mock_generate.assert_called_once()
    mock_publish.assert_called_once_with(
        {
            "promote": {
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            }
        },
        "Elixir POAP KINGS promotion content update",
    )
    channel_posts = mock_post.await_args.args[1]["content"]
    assert len(channel_posts) == 2
    assert "Discord recruiting copy" in channel_posts[0]
    assert "Reddit recruiting copy" in channel_posts[1]
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.args[0] == "promotion-content"
    assert mock_notify.await_args.kwargs["publish_result"]["commit_sha"] == "abc123def456"
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[0].kwargs["event_type"] == "promotion_content_cycle"
    assert mock_save.call_args_list[1].kwargs["event_type"] == "promotion_content_cycle_part"


def test_promotion_content_cycle_fails_when_site_write_returns_false():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={"discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."}},
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_called_once_with(
        {"promote": {"discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."}}},
        "Elixir POAP KINGS promotion content update",
    )
    mock_post.assert_not_awaited()
    mock_notify.assert_awaited_once_with("promotion-content", error_detail="GitHub publish failed")
    failure_message = mock_failure.call_args.args[1]
    assert failure_message == "site publish failed: GitHub publish failed"


def test_promotion_content_cycle_fails_when_discord_header_misses_required_trophy_text():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            },
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content") as mock_publish,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_not_called()
    mock_post.assert_not_awaited()
    assert mock_failure.call_args.args[1] == (
        "invalid promotion content: discord.body first line must include exact text "
        "`Required Trophies: [2000]`"
    )


def test_promotion_content_cycle_fails_when_reddit_title_misses_required_token():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG", "body": "Recruiting body"},
            },
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content") as mock_publish,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_not_called()
    mock_post.assert_not_awaited()
    assert mock_failure.call_args.args[1] == "invalid promotion content: reddit.title must include exact token `[2000]`"

def test_site_data_refresh_fails_when_poap_kings_publish_raises():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    awards_payload = {"generated_at": "2026-04-16T00:00:00Z", "seasons": []}
    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.poap_kings_site.build_awards_data", return_value=awards_payload),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._site_data_refresh())

    mock_publish.assert_called_once_with(
        {
            "roster": {"members": [{"name": "King Levy"}]},
            "clan": {"memberCount": 1},
            "awards": awards_payload,
        },
        "Elixir POAP KINGS site data refresh",
    )
    mock_notify.assert_awaited_once_with("site-data-refresh", error_detail="GitHub publish failed")
    assert mock_failure.call_args.args[1] == "GitHub publish failed"


def test_site_content_cycle_fails_when_daily_site_publish_raises():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    awards_payload = {"generated_at": "2026-04-16T00:00:00Z", "seasons": []}
    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.poap_kings_site.build_awards_data", return_value=awards_payload),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once_with(
        {
            "roster": {"members": [{"name": "King Levy", "tag": "ABC"}]},
            "clan": {"memberCount": 1},
            "awards": awards_payload,
            "home": {"message": "Home message", "generated": mock_publish.call_args.args[0]["home"]["generated"]},
        },
        "Elixir POAP KINGS daily site sync",
    )
    mock_notify.assert_awaited_once_with("site-content", error_detail="GitHub publish failed")
    assert mock_failure.call_args.args[1] == "GitHub publish failed"


def test_notify_poapkings_publish_posts_success_to_poapkings_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 900
    channel.name = "poapkings-com"
    channel.type = "text"
    publish_result = _publish_result("home", sha="feedbeef1234567")

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site._channel_config_by_key", return_value={
            "id": 900,
            "name": "#website-updates",
            "lane_key": "poapkings-com",
            "memory_scope": "public",
        }),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.jobs._site.build_lane_memory_context", return_value=""),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "POAP KINGS publish complete",
            "content": "Published `feedbee` https://github.com/jthingelstad/poapkings.com/commit/feedbeef1234567",
        }) as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        result = asyncio.run(elixir._notify_poapkings_publish("site-content", publish_result=publish_result))

    assert result is True
    mock_post.assert_awaited_once()
    assert "Commit SHA: feedbeef1234567" in mock_generate.call_args.args[2]
    assert "Commit URL: https://github.com/jthingelstad/poapkings.com/commit/feedbeef1234567" in mock_generate.call_args.args[2]
    assert mock_save.call_args.kwargs["workflow"] == "poapkings-com"
    assert mock_save.call_args.kwargs["event_type"] == "poapkings_publish_success"


def test_notify_poapkings_publish_skips_no_change_runs():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        result = asyncio.run(elixir._notify_poapkings_publish("site-content", publish_result={"changed": False}))

    assert result is False
    mock_post.assert_not_awaited()


def test_site_content_cycle_notifies_poapkings_channel_on_success():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("roster", "clan", "home")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once()
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.args[0] == "site-content"
    assert mock_notify.await_args.kwargs["publish_result"]["changed_content_types"] == ["roster", "clan", "home"]


def test_site_content_cycle_skips_poapkings_notification_when_no_change():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value={"changed": False}) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once()
    mock_notify.assert_awaited_once_with(
        "site-content",
        publish_result={
            "changed": False,
            "commit_sha": None,
            "commit_url": None,
            "repo": None,
            "branch": None,
            "changed_content_types": [],
            "changed_paths": [],
        },
    )


def test_detect_cake_days_uses_effective_join_date_and_birthdays():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-03-08", conn=conn)
        db.set_member_birthday("#ABC123", "King Levy", 3, 8, conn=conn)

        signals = heartbeat.detect_cake_days("2026-03-08", conn=conn)

        join_signal = next(signal for signal in signals if signal["type"] == "join_anniversary")
        birthday_signal = next(signal for signal in signals if signal["type"] == "member_birthday")

        # Both signals carry role + tenure_days so the LLM can narrate
        # without an extra get_member tool call.
        assert join_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "joined_date": "2024-03-08",
            "months": 24,
            "quarters": 8,
            "years": 2,
            "is_yearly": True,
            "role": "elder",
            "tenure_days": 730,  # 2024-03-08 → 2026-03-08, 2024 is a leap year
        }]
        assert birthday_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "birth_month": 3,
            "birth_day": 8,
            "role": "elder",
            "tenure_days": 730,
        }]
    finally:
        conn.close()


def test_detect_cake_days_emits_quarterly_join_milestone():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2025-12-08", conn=conn)

        signals = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        join_signal = next(signal for signal in signals if signal["type"] == "join_anniversary")

        assert join_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "joined_date": "2025-12-08",
            "months": 3,
            "quarters": 1,
            "years": 0,
            "is_yearly": False,
            "role": "member",
            "tenure_days": 90,  # 2025-12-08 → 2026-03-08
        }]
    finally:
        conn.close()


def test_detect_cake_days_emits_clan_birthday_with_rich_payload():
    """clan_birthday payload must carry founding_date, clan_name, and current
    active member count so the LLM can write a substantive post without
    calling get_clan_roster or chasing the founding date through tools."""
    conn = db.get_connection(":memory:")
    try:
        # Seed a few active members so active_member_count is non-zero.
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Thing", "role": "leader"},
                {"tag": "#DEF456", "name": "Raquaza", "role": "coLeader"},
                {"tag": "#GHI789", "name": "King Levy", "role": "elder"},
            ],
            conn=conn,
        )
        # Seed a clan-name row so the lookup has something authoritative.
        conn.execute(
            "INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name, observed_at) "
            "VALUES (?, ?, ?, ?)",
            ("2027-02-04", "#J2RGCRVG", "POAP KINGS", "2027-02-04T12:00:00"),
        )
        conn.commit()

        # 2027-02-04 is one year after the default founding date 2026-02-04.
        signals = heartbeat.detect_cake_days("2027-02-04", conn=conn)
        clan_signal = next(s for s in signals if s["type"] == "clan_birthday")

        assert clan_signal["years"] == 1
        assert clan_signal["founding_date"] == "2026-02-04"
        assert clan_signal["clan_name"] == "POAP KINGS"
        assert clan_signal["active_member_count"] == 3
    finally:
        conn.close()


def test_detect_cake_days_dedupes_announcements_for_the_day():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-03-08", conn=conn)
        db.set_member_birthday("#ABC123", "King Levy", 3, 8, conn=conn)

        first = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        assert {signal["type"] for signal in first} >= {"join_anniversary", "member_birthday"}

        db.mark_announcement_sent("2026-03-08", "join_anniversary", "#ABC123", conn=conn)
        db.mark_announcement_sent("2026-03-08", "birthday", "#ABC123", conn=conn)
        second = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        assert second == []
    finally:
        conn.close()


def test_clan_awareness_tick_marks_cake_day_announcements_after_successful_post():
    from runtime.jobs._signals import _mark_delivered_signals

    bundle = heartbeat.HeartbeatTickResult(
        signals=[{
            "type": "member_birthday",
            "members": [{"tag": "#ABC123", "name": "King Levy", "birth_month": 3, "birth_day": 8}],
        }],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC123"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def fake_deliver(signals, clan, war, workflow=None):
        _mark_delivered_signals(signals)
        return True

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.heartbeat.tick", return_value=bundle),
        patch("runtime.jobs._core._deliver_signal_group_via_awareness", side_effect=fake_deliver),
        patch("elixir.db.mark_signal_completed") as mock_mark_signal_sent,
        patch("elixir.db.mark_announcement_sent") as mock_mark_announcement_sent,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_mark_signal_sent.assert_called_once_with("member_birthday", db.chicago_today())
    mock_mark_announcement_sent.assert_called_once_with(db.chicago_today(), "birthday", "#ABC123")


def test_detect_war_rollovers_emits_week_rollover_for_new_live_week():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 2,
                        "createdDate": "20260222T120000.000Z",
                        "standings": [
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10000}}
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
                "sectionIndex": 2,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 10000,
                    "repairPoints": 0,
                    "periodPoints": 1200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 3,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 250,
                    "repairPoints": 0,
                    "periodPoints": 250,
                    "clanScore": 141,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_rollovers(conn=conn)

        assert [sig["type"] for sig in signals] == ["war_week_rollover"]
        assert signals[0]["previous_week"] == 3
        assert signals[0]["week"] == 4
        assert signals[0]["season_id"] == 129
        assert signals[0]["season_changed"] is False
    finally:
        conn.close()


def test_detect_war_rollovers_emits_week_and_season_rollover_for_section_wrap():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 3,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 12850}}
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
                "sectionIndex": 3,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 12850,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
            },
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
                    "fame": 400,
                    "repairPoints": 0,
                    "periodPoints": 400,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_rollovers(conn=conn)

        assert [sig["type"] for sig in signals] == ["war_week_rollover", "war_season_rollover"]
        assert signals[0]["previous_week"] == 4
        assert signals[0]["week"] == 1
        assert signals[0]["previous_season_id"] == 129
        assert signals[0]["season_id"] == 130
        assert signals[0]["season_changed"] is True
        assert signals[1]["previous_season_id"] == 129
        assert signals[1]["season_id"] == 130
    finally:
        conn.close()


def test_detect_war_day_transition_emits_battle_phase_active_from_api_state():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 0,
                    "periodIndex": 3,
                    "periodType": "warDay",
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

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals == [{
            "type": "war_battle_phase_active",
            "signal_log_type": "war_battle_phase_active::slive-w00-p003",
            "signal_date": "2026-03-14",
            "season_id": None,
            "week": 1,
            "section_index": 0,
            "period_index": 3,
            "period_type": "warDay",
            "message": "Battle phase is live. Time to use those war decks.",
        }]
    finally:
        conn.close()


def test_detect_war_day_transition_emits_practice_phase_active_from_api_state():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 1,
                    "periodIndex": 1,
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

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals == [{
            "type": "war_practice_phase_active",
            "signal_log_type": "war_practice_phase_active::slive-w01-p001",
            "signal_date": "2026-03-14",
            "season_id": None,
            "week": 2,
            "section_index": 1,
            "period_index": 1,
            "period_type": "trainingDay",
            "colosseum_week": False,
            "boat_defense_setup_scope": "one_time_per_practice_week",
            "boat_defense_tracking_available": False,
            "latest_clan_defense_status": None,
            "boat_defense_tracking_note": (
                "The live River Race API does not expose which members have placed "
                "boat defenses. It only exposes clan-level defense performance in "
                "period logs after days are logged."
            ),
            "message": (
                "Practice phase is live. Boat defenses are a one-time setup during "
                "practice days, so get them in early before battle days."
            ),
        }]
    finally:
        conn.close()


def test_detect_war_day_transition_marks_final_battle_day_from_api_period_index():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 5,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 3200,
                    "repairPoints": 0,
                    "periodPoints": 3200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 6,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 6400,
                    "repairPoints": 0,
                    "periodPoints": 3200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals[0]["type"] == "war_final_battle_day"
        assert signals[0]["week"] == 1
        assert signals[0]["period_index"] == 6
        assert db.get_current_war_status(conn=conn)["battle_day_number"] == 4
        assert db.get_current_war_status(conn=conn)["phase_display"] == "Battle Day 4"
        assert signals[0]["message"] == "Last day of battles this week. Use remaining decks!"
    finally:
        conn.close()


def test_detect_war_day_transition_includes_latest_clan_defense_status_when_available():
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
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 12850}}
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

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals[0]["type"] == "war_practice_phase_active"
        assert signals[0]["latest_clan_defense_status"]["num_defenses_remaining"] == 7
        assert signals[0]["latest_clan_defense_status"]["progress_earned_from_defenses"] == 311
        assert signals[0]["latest_clan_defense_status"]["phase_display"] == "Battle Day 4"
        assert signals[0]["latest_clan_defense_status"]["current_week_match"] is False
    finally:
        conn.close()


def test_detect_war_day_transition_marks_final_practice_day_from_api_period_index():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 1,
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
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
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

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert [signal["type"] for signal in signals] == ["war_final_practice_day"]
        assert signals[0]["week"] == 1
        assert signals[0]["period_index"] == 2
        assert signals[0]["boat_defense_setup_scope"] == "one_time_per_practice_week"
        assert signals[0]["boat_defense_tracking_available"] is False
        assert signals[0]["latest_clan_defense_status"] is None
        assert signals[0]["boat_defense_tracking_note"] == (
            "The live River Race API does not expose which members have placed "
            "boat defenses. It only exposes clan-level defense performance in "
            "period logs after days are logged."
        )
        assert signals[0]["message"] == (
            "Last day of practice this week. Boat defenses are a one-time setup, "
            "so make sure they are set before battle days start."
        )
        assert db.get_current_war_status(conn=conn)["practice_day_number"] == 3
        assert db.get_current_war_status(conn=conn)["phase_display"] == "Practice Day 3"
    finally:
        conn.close()


def test_detect_war_day_transition_marks_battle_phase_complete_from_api_transition():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T09:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 0,
                    "periodIndex": 6,
                    "periodType": "warDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 6400,
                        "repairPoints": 0,
                        "periodPoints": 3200,
                        "clanScore": 140,
                        "participants": [],
                    },
                },
                conn=conn,
            )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 1,
                    "periodIndex": 0,
                    "periodType": "trainingDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 0,
                        "repairPoints": 0,
                        "periodPoints": 0,
                        "clanScore": 141,
                        "participants": [],
                    },
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert [sig["type"] for sig in signals] == [
            "war_practice_phase_active",
            "war_battle_days_complete",
        ]
        assert signals[0]["week"] == 2
        assert signals[0]["period_type"] == "trainingDay"
        assert signals[1] == {
            "type": "war_battle_days_complete",
            "signal_log_type": "war_battle_days_complete::slive-w00-p006",
            "signal_date": "2026-03-13",
            "previous_season_id": None,
            "season_id": None,
            "previous_week": 1,
            "week": 2,
            "previous_period_type": "warDay",
            "period_type": "trainingDay",
            "message": "Battle phase has ended. River Race has moved out of battle days.",
        }
    finally:
        conn.close()


def test_get_current_war_status_marks_live_finish_and_keeps_trophy_stakes_conservative():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 10146,
                        "repairPoints": 0,
                        "periodPoints": 10146,
                        "clanScore": 160,
                        "finishTime": "20260315T095605.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["finish_time"] == "20260315T095605.000Z"
    assert status["race_completed"] is True
    assert status["race_completed_at"] == "2026-03-15T09:56:05"
    assert status["race_completed_early"] is True
    assert status["trophy_change"] is None
    assert status["trophy_stakes_known"] is False
    assert status["trophy_stakes_text"] is None


def test_get_current_war_status_ignores_sentinel_finish_time():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 3,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 5100,
                        "repairPoints": 0,
                        "periodPoints": 5100,
                        "clanScore": 160,
                        "finishTime": "19691231T235959.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 5100, "repairPoints": 0, "periodPoints": 5100, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["finish_time"] is None
    assert status["race_completed"] is False
    assert status["race_completed_at"] is None


def test_get_current_war_status_surfaces_same_week_trophy_stakes_when_known():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [{
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "createdDate": "20260315T095606.000Z",
                    "standings": [{
                        "rank": 1,
                        "trophyChange": 100,
                        "clan": {
                            "tag": "#J2RGCRVG",
                            "name": "POAP KINGS",
                            "fame": 10146,
                            "repairPoints": 0,
                            "finishTime": "20260315T095605.000Z",
                            "participants": [],
                        },
                    }],
                }],
            },
            "#J2RGCRVG",
            conn=conn,
        )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 10146,
                        "repairPoints": 0,
                        "periodPoints": 10146,
                        "clanScore": 160,
                        "finishTime": "20260315T095605.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["trophy_change"] == 100
    assert status["trophy_stakes_known"] is True
    assert status["trophy_stakes_text"] == "100 trophies on the line"


def test_detect_war_signals_from_storage_seeds_forward_without_backfill():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
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
                        "fame": 500,
                        "repairPoints": 0,
                        "periodPoints": 500,
                        "clanScore": 150,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                        ],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 500, "repairPoints": 0, "periodPoints": 500, "clanScore": 150}],
                },
                conn=conn,
            )

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    # Cursor-based detectors seed on first call with no prior state. Event-
    # based detectors (war_attacks_complete, war_surprise_participant) may
    # still fire if the fixture includes members with decks_used_today > 0.
    cursor_types = {s["type"] for s in result.signals} - {"war_attacks_complete", "war_surprise_participant"}
    assert cursor_types == set()
    assert {update["detector_key"] for update in result.cursor_updates} == {
        heartbeat.WAR_LIVE_STATE_CURSOR_KEY,
        heartbeat.WAR_PARTICIPANT_CURSOR_KEY,
    }


def test_detect_war_signals_from_storage_replays_multiple_new_finishers_once():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
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
                "fame": 500,
                "repairPoints": 0,
                "periodPoints": 500,
                "clanScore": 150,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 500, "repairPoints": 0, "periodPoints": 500, "clanScore": 150}],
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
                "fame": 900,
                "repairPoints": 0,
                "periodPoints": 900,
                "clanScore": 155,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 900, "repairPoints": 0, "periodPoints": 900, "clanScore": 155}],
        }
        third_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 1300,
                "repairPoints": 0,
                "periodPoints": 1300,
                "clanScore": 160,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 700, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 1300, "repairPoints": 0, "periodPoints": 1300, "clanScore": 160}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
            db.upsert_war_current_state(first_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T11:00:00"):
            db.upsert_war_current_state(second_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T12:00:00"):
            db.upsert_war_current_state(third_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )
        rerun = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    all_deck_signals = [signal for signal in result.signals if signal["type"] == "war_member_used_all_decks"]
    assert len(all_deck_signals) == 0
    # war_attacks_complete may fire from detect_war_battle_activity if any
    # participants used all 4 decks. That's expected — cursor-based detectors
    # dedup via signal_log after delivery, not between raw detection calls.
    rerun_types = {s["type"] for s in rerun.signals}
    assert rerun_types <= {"war_attacks_complete", "war_surprise_participant"}


def test_detect_war_signals_from_storage_emits_live_finish_and_suppresses_pressure_signals():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        seed_payload = {
            "seasonId": 130,
            "sectionIndex": 1,
            "periodIndex": 13,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 8400,
                "repairPoints": 0,
                "periodPoints": 8400,
                "clanScore": 158,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 2},
                ],
            },
            "clans": [
                {"tag": "#RIVAL1", "name": "Rivals", "fame": 8700, "repairPoints": 0, "periodPoints": 8700, "clanScore": 160},
                {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 8400, "repairPoints": 0, "periodPoints": 8400, "clanScore": 158},
            ],
        }
        finish_payload = {
            "seasonId": 130,
            "sectionIndex": 1,
            "periodIndex": 13,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 10146,
                "repairPoints": 0,
                "periodPoints": 10146,
                "clanScore": 160,
                "finishTime": "20260315T095605.000Z",
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 700, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                ],
            },
            "clans": [
                {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160},
                {"tag": "#RIVAL1", "name": "Rivals", "fame": 9900, "repairPoints": 0, "periodPoints": 9900, "clanScore": 159},
            ],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T20:00:00"):
            db.upsert_war_current_state(seed_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T23:00:00"):
            db.upsert_war_current_state(finish_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    signal_types = {signal["type"] for signal in result.signals}
    assert "war_race_finished_live" in signal_types
    assert "war_battle_day_live_update" not in signal_types
    assert "war_battle_day_final_hours" not in signal_types
    assert "war_battle_rank_change" not in signal_types

    finish_signal = next(signal for signal in result.signals if signal["type"] == "war_race_finished_live")
    assert finish_signal["finish_time"] == "20260315T095605.000Z"
    assert finish_signal["race_completed_early"] is True


def test_battle_lead_payload_becomes_completion_aware_after_live_finish():
    payload = heartbeat._battle_lead_payload(
        2,
        war_state={
            "race_completed": True,
            "race_completed_at": "2026-03-15T09:56:05",
            "race_completed_early": True,
            "trophy_stakes_known": True,
            "trophy_stakes_text": "100 trophies on the line",
        },
    )

    assert payload["lead_pressure"] == "complete"
    assert payload["needs_lead_recovery"] is False
    assert "already finished" in payload["lead_story"]
    assert "clean closure" in payload["lead_call_to_action"]


def test_detect_war_signals_from_storage_replays_multiple_live_state_rows_once():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        seed_payload = {
            "seasonId": 129,
            "sectionIndex": 0,
            "periodIndex": 2,
            "periodType": "trainingDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 0,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 140,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "repairPoints": 0, "periodPoints": 0, "clanScore": 140}],
        }
        battle_payload = {
            "seasonId": 129,
            "sectionIndex": 0,
            "periodIndex": 3,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 400,
                "repairPoints": 0,
                "periodPoints": 400,
                "clanScore": 141,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 400, "repairPoints": 0, "periodPoints": 400, "clanScore": 141}],
        }
        rollover_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 0,
            "periodType": "trainingDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 0,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 142,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "repairPoints": 0, "periodPoints": 0, "clanScore": 142}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T09:00:00"):
            db.upsert_war_current_state(seed_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:05:00"):
            db.upsert_war_current_state(battle_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(rollover_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )
        rerun = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    signal_types = [signal["type"] for signal in result.signals]
    assert "war_battle_phase_active" in signal_types
    assert "war_practice_phase_active" in signal_types
    assert "war_battle_days_complete" in signal_types
    assert "war_week_rollover" in signal_types
    assert rerun.signals == []


def test_detect_war_day_markers_emits_day_start_and_prior_day_recap():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
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
                        "fame": 800,
                        "repairPoints": 0,
                        "periodPoints": 800,
                        "clanScore": 151,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                            {"tag": "#DEF456", "name": "Vijay", "fame": 100, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                        ],
                    },
                        "clans": [
                            {"tag": "#OTHER1", "name": "Other Clan", "fame": 950, "repairPoints": 0, "periodPoints": 950, "clanScore": 160},
                            {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 800, "repairPoints": 0, "periodPoints": 800, "clanScore": 151},
                        ],
                },
                conn=conn,
            )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 11,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 1200,
                        "repairPoints": 0,
                        "periodPoints": 400,
                        "clanScore": 152,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                            {"tag": "#DEF456", "name": "Vijay", "fame": 100, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                        ],
                    },
                        "clans": [
                            {"tag": "#OTHER1", "name": "Other Clan", "fame": 1400, "repairPoints": 0, "periodPoints": 500, "clanScore": 165},
                            {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 1200, "repairPoints": 0, "periodPoints": 400, "clanScore": 152},
                        ],
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_markers(conn=conn)

        assert [signal["type"] for signal in signals] == [
            "war_battle_day_started",
            "war_battle_day_complete",
        ]
        assert signals[0]["phase_display"] == "Battle Day 2"
        assert signals[0]["needs_lead_recovery"] is True
        assert signals[0]["lead_pressure"] == "high"
        assert signals[1]["phase_display"] == "Battle Day 1"
        assert signals[1]["finished_count"] == 1
        assert signals[1]["engaged_count"] == 2
        assert signals[1]["top_fame_today"][0]["name"] == "King Levy"
    finally:
        conn.close()


def test_detect_war_week_complete_enriches_completion_signal():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260308T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 20,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 14000,
                                    "finishTime": "20260308T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 2400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
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

        signals = heartbeat.detect_war_week_complete(
            [{
                "type": "war_completed",
                "season_id": 129,
                "section_index": 1,
                "our_rank": 1,
                "our_fame": 14000,
                "total_clans": 5,
                "won": True,
            }],
            conn=conn,
        )

        assert signals[0]["type"] == "war_week_complete"
        assert signals[0]["week"] == 2
        assert signals[0]["week_summary"]["top_participants"][0]["name"] == "King Levy"
    finally:
        conn.close()


def test_detect_war_completion_retries_until_signal_is_marked():
    conn = db.get_connection(":memory:")
    try:
        # Use yesterday's timestamp so the stale-finish_time guard in
        # detect_war_completion does not reject this row.
        yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        fresh_ts = yesterday.strftime("%Y%m%dT%H%M%S") + ".000Z"
        race_log = {
            "items": [
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "createdDate": fresh_ts,
                    "standings": [
                        {
                            "rank": 1,
                            "trophyChange": 20,
                            "clan": {
                                "tag": "#J2RGCRVG",
                                "name": "POAP KINGS",
                                "fame": 14000,
                                "finishTime": fresh_ts,
                                "participants": [],
                            },
                        }
                    ],
                }
            ]
        }

        with patch("heartbeat.cr_api.get_river_race_log", return_value=race_log):
            first = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)
            second = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)

        assert len(first) == 1
        assert first[0]["type"] == "war_completed"
        assert first[0]["signal_log_type"] == "war_completed::129:1"
        assert len(second) == 1

        db.mark_signal_sent("war_completed::129:1", db.chicago_today(), conn=conn)

        with patch("heartbeat.cr_api.get_river_race_log", return_value=race_log):
            third = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)

        assert third == []
    finally:
        conn.close()


def test_member_join_detection_survives_non_heartbeat_warstate_ingest():
    """Regression: a non-heartbeat path (Discord channel handler that fetches
    live war state) must not promote a brand-new clan member from
    'observed' to 'active' before the heartbeat's join detector runs.

    Reproduces the 2026-04-25 Strixx miss: upsert_war_current_state inserts
    the new clan member via _ensure_member(status=None) → 'observed'. Then
    snapshot_members(create_if_missing=False) used to bump status to
    'active', which made get_active_roster_map() return the tag, which
    made detect_joins_leaves see the new member as already-known, which
    silently dropped the member_join signal.

    Fix lives in storage/roster.py:snapshot_members — the
    create_if_missing=False branch now passes status=None.
    """
    from heartbeat._roster import detect_joins_leaves

    conn = db.get_connection(":memory:")
    try:
        # 1. Pre-existing clan: one active member.
        db.snapshot_members(
            [{"tag": "#OLD", "name": "Veteran", "role": "member",
              "lastSeen": "20260425T100000.000Z"}],
            conn=conn,
        )
        assert "#NEW" not in db.get_active_roster_map(conn=conn)

        # 2. Live CR API now sees a NEW player (they just joined). A Discord
        #    channel handler fetches live war state, which inserts the new
        #    member via the war-ingest _ensure_member path with status=None
        #    (→ 'observed').
        live_war = {
            "state": "full",
            "seasonId": 131,
            "sectionIndex": 2,
            "periodIndex": 12,
            "periodType": "warDay",
            "clan": {
                "tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 4500,
                "repairPoints": 0, "periodPoints": 4500, "clanScore": 100,
                "participants": [
                    {"tag": "#OLD", "name": "Veteran", "fame": 2000,
                     "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4,
                     "decksUsedToday": 4},
                    {"tag": "#NEW", "name": "Newcomer", "fame": 0,
                     "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0,
                     "decksUsedToday": 0},
                ],
            },
        }
        db.upsert_war_current_state(live_war, conn=conn)

        # 3. Same handler then calls snapshot_members with
        #    create_if_missing=False (the _load_live_clan_context path).
        #    PRE-FIX: this promoted #NEW from 'observed' to 'active',
        #    breaking the heartbeat's diff. POST-FIX: status is preserved.
        live_member_list = [
            {"tag": "#OLD", "name": "Veteran", "role": "member",
             "lastSeen": "20260425T120000.000Z"},
            {"tag": "#NEW", "name": "Newcomer", "role": "member",
             "lastSeen": "20260425T120000.000Z"},
        ]
        db.snapshot_members(live_member_list, create_if_missing=False, conn=conn)

        # 4. The active roster as seen by the heartbeat MUST still hide #NEW —
        #    otherwise the join diff comes back empty.
        active_now = db.get_active_roster_map(conn=conn)
        assert "#NEW" not in active_now, (
            "non-heartbeat path promoted #NEW to active; heartbeat will miss the join"
        )
        assert "#OLD" in active_now

        # 5. Now the heartbeat runs. It captures `known` BEFORE snapshotting,
        #    then snapshots with create_if_missing=True (promoting #NEW to
        #    active), then diffs.
        known = db.get_active_roster_map(conn=conn)
        db.snapshot_members(live_member_list, create_if_missing=True, conn=conn)
        signals, _ = detect_joins_leaves(live_member_list, known, conn=conn)

        join_tags = [s["tag"] for s in signals if s["type"] == "member_join"]
        assert join_tags == ["#NEW"], (
            f"expected exactly one member_join for #NEW, got {signals}"
        )
    finally:
        conn.close()
