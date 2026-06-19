"""Guardrails for the internal data subsystem pivot.

These tests snapshot transition-only legacy routing plus event identity
behavior while `_deliver_signal_group()` remains as a compatibility shim.
"""

import pytest

import db
from runtime.signal_lanes import plan_signal_outcomes, signal_source_key
from storage.event_stream import event_key_for_signal, record_signal_events


def _routing_snapshot(signals):
    return [
        (
            outcome["target_channel_key"],
            outcome["intent"],
            bool(outcome["required"]),
        )
        for outcome in plan_signal_outcomes(signals)
    ]


@pytest.mark.parametrize(
    ("label", "signals", "expected"),
    [
        (
            "inactivity leadership note",
            [
                {
                    "type": "inactive_members",
                    "signal_date": "2026-06-19",
                    "members": [{"name": "xian", "tag": "#UGQPVQ9U9"}],
                }
            ],
            [("leader-lounge", "leadership_note", True)],
        ),
        (
            "member join fanout",
            [
                {
                    "type": "member_join",
                    "tag": "#NEW123",
                    "name": "Newbie",
                    "signal_log_type": "member_join:#NEW123",
                }
            ],
            [
                ("clan-events", "member_join_public", True),
                ("leader-lounge", "member_join_ops", True),
                ("arena-relay", "welcome_relay", False),
            ],
        ),
        (
            "war rank change",
            [
                {
                    "type": "war_battle_rank_change",
                    "signal_log_type": "war_battle_rank_change::s134-w02-p3::rank2",
                    "season_id": 134,
                    "week": 2,
                    "race_rank": 2,
                }
            ],
            [
                ("river-race", "war_update", True),
                ("leader-lounge", "war_ops_note", False),
            ],
        ),
        (
            "war week complete",
            [
                {
                    "type": "war_week_complete",
                    "signal_log_type": "war_week_complete::134:1",
                    "season_id": 134,
                    "section_index": 1,
                    "week": 2,
                }
            ],
            [
                ("river-race", "war_update", True),
                ("arena-relay", "war_relay_brief", False),
                ("leader-lounge", "war_ops_note", False),
            ],
        ),
        (
            "promotion candidate leadership audience",
            [
                {
                    "type": "promotion_review",
                    "audience": "leadership",
                    "tag": "#PROMO1",
                    "name": "Promo",
                }
            ],
            [("leader-lounge", "leadership_note", True)],
        ),
        (
            "battle-mode player highlight",
            [
                {
                    "type": "battle_hot_streak",
                    "signal_log_type": "battle_hot_streak:#HOT:ranked:2026-06-19",
                    "tag": "#HOT",
                    "mode": "ranked",
                }
            ],
            [("member-highlights", "battle_mode_update", True)],
        ),
        (
            "durable milestone player highlight",
            [
                {
                    "type": "new_card_unlocked",
                    "signal_log_type": "new_card_unlocked:#CARD:123",
                    "tag": "#CARD",
                    "card_name": "Monk",
                }
            ],
            [
                ("member-highlights", "player_progress", True),
                ("arena-relay", "celebration_relay", False),
            ],
        ),
    ],
)
def test_representative_signal_routing_snapshots(label, signals, expected):
    assert _routing_snapshot(signals) == expected, label


def test_event_key_policy_ignores_downstream_event_annotations():
    signal = {
        "type": "inactive_members",
        "signal_date": "2026-06-19",
        "members": [{"name": "xian", "tag": "#UGQPVQ9U9"}],
    }

    first = event_key_for_signal(
        signal,
        source_system="clan_awareness",
        source_detector="detect_inactivity",
    )
    signal["event_key"] = "game_event:already-recorded"
    signal["event_id"] = 123
    second = event_key_for_signal(
        signal,
        source_system="clan_awareness",
        source_detector="detect_inactivity",
    )

    assert second == first


def test_event_key_policy_prefers_signal_key_over_payload_hash():
    signal = {
        "type": "capability_unlock",
        "signal_key": "capability:stream-aware-situation",
        "payload": {"body": "original"},
    }
    first = event_key_for_signal(signal, source_system="system_signals")

    signal["payload"]["body"] = "changed wording"
    second = event_key_for_signal(signal, source_system="system_signals")

    assert second == first


def test_signal_source_key_is_canonical_across_runtime_and_event_stream():
    signal = {
        "type": "path_of_legend_promotion",
        "tag": "#RJ9RRQPVU",
    }
    expected = "path_of_legend_promotion||#RJ9RRQPVU"

    assert signal_source_key(signal) == expected

    conn = db.get_connection(":memory:")
    try:
        record_signal_events(
            [dict(signal)],
            source_system="player_intel",
            source_detector="profile_refresh",
            conn=conn,
        )
        row = conn.execute(
            "SELECT source_signal_key FROM game_event_stream ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        assert row["source_signal_key"] == expected
    finally:
        conn.close()
