"""The brain's live delivery layer (runtime/awareness/deliver.py) + the two
correctness linchpins it depends on: record_awareness_post feeding channel_memory
(dedup) and last_tick_at excluding failed ticks (fail-hard, catch-up)."""

from __future__ import annotations

from unittest.mock import patch

from runtime.awareness import deliver as deliver_mod
from runtime.awareness import read as read_mod
from runtime.awareness import store

_LANES = {
    "announcements": {
        "channel_id": 111,
        "channel_name": "announcements",
        "leadership": False,
    },
    "elixir": {"channel_id": 222, "channel_name": "elixir", "leadership": False},
}


def _recorder():
    calls = []

    def record_fn(**kwargs):
        calls.append(kwargs)

    return record_fn, calls


# --------------------------------------------------------------- deliver_posts


def test_delivers_both_channels_and_records_each():
    sent = []

    def post_fn(channel_id, copy):
        sent.append((channel_id, copy))
        return 900 + len(sent)

    record_fn, recorded = _recorder()
    plan = {
        "posts": [
            {
                "channel": "announcements",
                "content": "Welcome Zed!",
                "covers_signal_keys": ["s1"],
            },
            {
                "channel": "elixir",
                "content": ["part a", "part b"],
                "covers_signal_keys": ["s2"],
            },
        ]
    }
    read = {"hard_post_signals": [{"signal_key": "s1"}]}

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(read, plan, post_fn=post_fn, record_fn=record_fn)

    assert result == {
        "delivered": 2,
        "failed": False,
        "reason": None,
        "uncovered_hard": [],
    }
    assert sent == [(111, "Welcome Zed!"), (222, "part a\n\npart b")]
    assert [r["lane"] for r in recorded] == ["announcements", "elixir"]
    assert recorded[0]["message_id"] == 901


def test_unknown_channel_fails_tick():
    read, plan = {}, {"posts": [{"channel": "leader-lounge", "content": "x"}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: 1, record_fn=lambda **_: None
        )
    assert result["failed"] is True
    assert "leader-lounge" in result["reason"]


def test_send_returning_none_fails_tick():
    read, plan = {}, {"posts": [{"channel": "elixir", "content": "x"}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: None, record_fn=lambda **_: None
        )
    assert result["failed"] is True


def test_uncovered_hard_post_floor_fails_tick():
    # The post covers s1 but the floor also demands s2: the whole side-effect
    # plan fails validation before any partial Discord delivery can escape.
    record_fn, recorded = _recorder()
    sent = []
    read = {"hard_post_signals": [{"signal_key": "s1"}, {"signal_key": "s2"}]}
    plan = {"posts": [{"channel": "elixir", "content": "hi", "covers_signal_keys": ["s1"]}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda *args: sent.append(args) or 5,
            record_fn=record_fn,
        )
    assert result["failed"] is True
    assert result["uncovered_hard"] == ["s2"]
    assert sent == []
    assert recorded == []


def test_partial_delivery_retry_sends_only_unfulfilled_intent(engine_conn):
    class IntentStore:
        @staticmethod
        def prepare_delivery_intents(posts, required_signal_keys=None):
            return store.prepare_delivery_intents(
                posts,
                required_signal_keys=required_signal_keys,
                conn=engine_conn,
            )

        @staticmethod
        def mark_delivery_sending(intent_key):
            return store.mark_delivery_sending(intent_key, conn=engine_conn)

        @staticmethod
        def mark_delivery_pending(intent_key, error):
            return store.mark_delivery_pending(intent_key, error, conn=engine_conn)

        @staticmethod
        def mark_delivery_fulfilled(intent_key, message_id, loop_number=None):
            return store.mark_delivery_fulfilled(
                intent_key,
                message_id,
                loop_number=loop_number,
                conn=engine_conn,
            )

    plan = {
        "posts": [
            {
                "channel": "announcements",
                "content": "Welcome Alpha",
                "covers_signal_keys": ["s1"],
            },
            {
                "channel": "elixir",
                "content": "Bravo reached a new best",
                "covers_signal_keys": ["s2"],
            },
        ]
    }
    read = {"hard_post_signals": [{"signal_key": "s1"}, {"signal_key": "s2"}]}
    first_sends = []

    def fail_second(channel_id, copy):
        first_sends.append((channel_id, copy))
        if len(first_sends) == 2:
            raise RuntimeError("discord unavailable")
        return 901

    def record(**kwargs):
        store.record_awareness_post(conn=engine_conn, **kwargs)

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        first = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=fail_second,
            record_fn=record,
            intent_store=IntentStore,
        )
        retry_sends = []
        second = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda channel_id, copy: retry_sends.append((channel_id, copy)) or 902,
            record_fn=record,
            intent_store=IntentStore,
        )

    assert first["failed"] is True
    assert first["delivered"] == 1
    assert second["failed"] is False
    assert second["delivered"] == 1
    assert retry_sends == [(222, "Bravo reached a new best")]
    rows = engine_conn.execute(
        "SELECT lane, status, attempts FROM awareness_delivery_intents ORDER BY lane"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("announcements", "fulfilled", 1),
        ("elixir", "fulfilled", 2),
    ]


def test_pending_outbox_post_drains_even_when_fresh_plan_chooses_silence(engine_conn):
    class IntentStore:
        @staticmethod
        def prepare_delivery_intents(posts, required_signal_keys=None):
            return store.prepare_delivery_intents(
                posts,
                required_signal_keys=required_signal_keys,
                conn=engine_conn,
            )

        @staticmethod
        def mark_delivery_sending(intent_key):
            return store.mark_delivery_sending(intent_key, conn=engine_conn)

        @staticmethod
        def mark_delivery_pending(intent_key, error):
            return store.mark_delivery_pending(intent_key, error, conn=engine_conn)

        @staticmethod
        def mark_delivery_fulfilled(intent_key, message_id, loop_number=None):
            return store.mark_delivery_fulfilled(
                intent_key, message_id, loop_number=loop_number, conn=engine_conn
            )

    pending_post = {
        "channel": "elixir",
        "content": "Alpha reached a new best",
        "covers_signal_keys": ["s1"],
        "_delivery_content": "Alpha reached a new best",
    }
    store.prepare_delivery_intents([pending_post], conn=engine_conn)
    plan = {"posts": [], "skipped_reason": "fresh read was quiet"}
    sent = []

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {"hard_post_signals": [{"signal_key": "s1"}]},
            plan,
            post_fn=lambda channel_id, copy: sent.append((channel_id, copy)) or 903,
            record_fn=lambda **kwargs: store.record_awareness_post(conn=engine_conn, **kwargs),
            intent_store=IntentStore,
        )

    assert result["failed"] is False
    assert result["replayed"] == 1
    assert sent == [(222, "Alpha reached a new best")]
    assert plan["posts"][0]["covers_signal_keys"] == ["s1"]


def test_invalid_hard_post_plan_is_rejected_before_outbox_persistence(engine_conn):
    class IntentStore:
        @staticmethod
        def prepare_delivery_intents(posts, required_signal_keys=None):
            return store.prepare_delivery_intents(
                posts,
                required_signal_keys=required_signal_keys,
                conn=engine_conn,
            )

    read = {"hard_post_signals": [{"signal_key": "s1"}, {"signal_key": "s2"}]}
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "content": "Only half the mandatory plan",
                "covers_signal_keys": ["s1"],
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda *_: 904,
            record_fn=lambda **_: None,
            intent_store=IntentStore,
        )

    assert result["failed"] is True
    assert result["uncovered_hard"] == ["s2"]
    count = engine_conn.execute("SELECT COUNT(*) FROM awareness_delivery_intents").fetchone()[0]
    assert count == 0


def test_fulfilled_sibling_counts_while_pending_sibling_retries(engine_conn):
    class IntentStore:
        @staticmethod
        def prepare_delivery_intents(posts, required_signal_keys=None):
            return store.prepare_delivery_intents(
                posts,
                required_signal_keys=required_signal_keys,
                conn=engine_conn,
            )

        @staticmethod
        def mark_delivery_sending(intent_key):
            return store.mark_delivery_sending(intent_key, conn=engine_conn)

        @staticmethod
        def mark_delivery_pending(intent_key, error):
            return store.mark_delivery_pending(intent_key, error, conn=engine_conn)

        @staticmethod
        def mark_delivery_fulfilled(intent_key, message_id, loop_number=None):
            return store.mark_delivery_fulfilled(
                intent_key, message_id, loop_number=loop_number, conn=engine_conn
            )

    posts = [
        {
            "channel": "announcements",
            "content": "Alpha joined",
            "covers_signal_keys": ["s1"],
            "_delivery_content": "Alpha joined",
        },
        {
            "channel": "elixir",
            "content": "Bravo reached a new best",
            "covers_signal_keys": ["s2"],
            "_delivery_content": "Bravo reached a new best",
        },
    ]
    work = store.prepare_delivery_intents(
        posts, required_signal_keys={"s1", "s2"}, conn=engine_conn
    )
    first_key = next(
        item["intent_key"] for item in work if item["post"]["channel"] == "announcements"
    )
    assert store.mark_delivery_sending(first_key, conn=engine_conn) is True
    store.mark_delivery_fulfilled(first_key, 904, conn=engine_conn)
    sent = []
    plan = {"posts": [], "skipped_reason": "fresh plan was quiet"}

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {"hard_post_signals": [{"signal_key": "s1"}, {"signal_key": "s2"}]},
            plan,
            post_fn=lambda channel_id, copy: sent.append((channel_id, copy)) or 905,
            record_fn=lambda **kwargs: store.record_awareness_post(conn=engine_conn, **kwargs),
            intent_store=IntentStore,
        )

    assert result["failed"] is False
    assert result["delivered"] == 1
    assert sent == [(222, "Bravo reached a new best")]


def test_expired_sending_lease_returns_to_pending_for_at_least_once_retry(engine_conn):
    post = {
        "channel": "elixir",
        "content": "Alpha reached a new best",
        "covers_signal_keys": ["s1"],
        "_delivery_content": "Alpha reached a new best",
    }
    work = store.prepare_delivery_intents([post], conn=engine_conn)
    intent_key = work[0]["intent_key"]
    assert store.mark_delivery_sending(intent_key, conn=engine_conn) is True

    # A fresh in-flight lease remains ambiguous and fails closed.
    fresh = store.prepare_delivery_intents([], conn=engine_conn)
    assert fresh[0]["status"] == "sending"

    engine_conn.execute(
        "UPDATE awareness_delivery_intents SET updated_at = '2000-01-01T00:00:00Z' "
        "WHERE intent_key = ?",
        (intent_key,),
    )
    expired = store.prepare_delivery_intents([], conn=engine_conn)

    assert expired[0]["status"] == "pending"
    row = engine_conn.execute(
        "SELECT attempts, last_error FROM awareness_delivery_intents WHERE intent_key = ?",
        (intent_key,),
    ).fetchone()
    assert tuple(row) == (1, "delivery lease expired; retrying at least once")


def test_clan_chat_voicing_failure_does_not_fail_the_post():
    def bad_relay(post, channel_name):
        raise RuntimeError("voicing boom")

    plan = {
        "posts": [
            {
                "channel": "elixir",
                "content": "big news",
                "clan_chat": ["Big news for the whole clan today."],
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 7,
            record_fn=lambda **_: None,
            relay_fn=bad_relay,
        )
    assert result["failed"] is False
    assert result["delivered"] == 1


def test_presence_of_clan_chat_is_the_routing_decision():
    """The in-game voicing fires because `clan_chat` is present — no separate flag.
    A sibling post with no `clan_chat` is not voiced in-game."""
    voiced = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "content": "voiced one",
                "clan_chat": ["Nice climb from Andy today."],
            },
            {"channel": "elixir", "content": "silent one"},
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 5,
            record_fn=lambda **_: None,
            relay_fn=lambda p, c: voiced.append(p.get("content")),
        )
    assert voiced == ["voiced one"], "only the post with a clan_chat voicing goes in-game"


def test_member_join_with_clan_chat_voices_in_game():
    """A join that carries its in-game welcome is voiced to clan chat."""
    voiced = []
    plan = {
        "posts": [
            {
                "channel": "announcements",
                "content": "Welcome BigNorton! 10,090 trophies.",
                "covers_signal_keys": ["member_joined:#CV20JCY0V:t"],
                "clan_chat": ["Welcome BigNorton to POAP KINGS! 10,090 trophies."],
            }
        ]
    }
    read = {
        "hard_post_signals": [
            {"signal_key": "member_joined:#CV20JCY0V:t", "event_type": "member_joined"}
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda *_: 5,
            record_fn=lambda **_: None,
            relay_fn=lambda p, c: voiced.append(p.get("covers_signal_keys")),
        )

    assert result["failed"] is False
    assert voiced == [["member_joined:#CV20JCY0V:t"]]


def test_member_join_without_clan_chat_fails_tick_as_missed_signal():
    """A join with no in-game voicing is a MISSED SIGNAL: the copy policy fails the
    tick (no template substituted) so the join re-surfaces next loop."""
    voiced = []
    plan = {
        "posts": [
            {
                "channel": "announcements",
                "content": "Welcome BigNorton! 10,090 trophies.",
                "covers_signal_keys": ["member_joined:#CV20JCY0V:t"],
            }
        ]
    }
    read = {
        "hard_post_signals": [
            {"signal_key": "member_joined:#CV20JCY0V:t", "event_type": "member_joined"}
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda *_: 5,
            record_fn=lambda **_: None,
            relay_fn=lambda p, c: voiced.append(p),
        )

    assert result["failed"] is True
    assert "join_missing_clan_chat" in result["reason"]
    assert voiced == [], "nothing is voiced in-game when the tick fails pre-send"


def test_copy_policy_blocks_gendered_member_pronoun_before_send():
    sent = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "content": "King Levy broke his old ceiling.",
                "covers_signal_keys": ["best_trophies_peak:#LEVY:13000"],
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *args: sent.append(args),
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "gendered_member_pronoun" in result["reason"]
    assert sent == []


def test_copy_policy_repairs_once_then_sends_corrected_plan():
    sent = []
    repairs = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "content": "King Levy broke his old ceiling.",
                "covers_signal_keys": ["best_trophies_peak:#LEVY:13000"],
            }
        ]
    }

    def repair_fn(read, rejected, violations):
        repairs.append((rejected, violations))
        return {
            "posts": [
                {
                    **rejected["posts"][0],
                    "content": "King Levy broke their old ceiling.",
                }
            ]
        }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda channel_id, copy: sent.append((channel_id, copy)) or 44,
            record_fn=lambda **_: None,
            repair_fn=repair_fn,
        )

    assert result["failed"] is False
    assert len(repairs) == 1
    assert sent == [(222, "King Levy broke their old ceiling.")]


def test_copy_policy_failed_repair_still_fails_closed():
    sent = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "content": "She reached a new best.",
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *args: sent.append(args),
            record_fn=lambda **_: None,
            repair_fn=lambda *_: plan,
        )

    assert result["failed"] is True
    assert sent == []


def test_copy_policy_repair_cannot_change_signal_coverage():
    sent = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "content": "She reached a new best.",
                "covers_signal_keys": ["peak:#A:1"],
            }
        ]
    }

    def unsafe_repair(*_):
        return {
            "posts": [
                {
                    **plan["posts"][0],
                    "content": "They reached a new best.",
                    "covers_signal_keys": [],
                }
            ]
        }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *args: sent.append(args),
            record_fn=lambda **_: None,
            repair_fn=unsafe_repair,
        )

    assert result["failed"] is True
    assert "changed_covers_signal_keys" in result["reason"]
    assert sent == []


def test_copy_policy_blocks_current_rank_when_race_is_unranked():
    sent = []
    read = {"war_season": {"race_ranked": False}}
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "Don't read anything into today's rank 3 showing.",
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=lambda *args: sent.append(args),
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "current_rank_while_unranked" in result["reason"]
    assert sent == []


def test_copy_policy_blocks_plain_we_are_third_when_race_is_unranked():
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "We're 3rd and pushing for the top.",
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {"war_season": {"race_ranked": False}},
            plan,
            post_fn=lambda *_: 77,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "current_rank_while_unranked" in result["reason"]


def test_copy_policy_repair_cannot_change_or_drop_factual_numbers():
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "milestone",
                "content": "She crossed 13,000 trophies.",
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 77,
            record_fn=lambda **_: None,
            repair_fn=lambda *_: {
                "posts": [
                    {
                        **plan["posts"][0],
                        "content": "They crossed 12,000 trophies.",
                    }
                ]
            },
        )

    assert result["failed"] is True
    assert "introduced_number" in result["reason"]


def test_copy_policy_rank_repair_may_remove_only_bad_rank_number():
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "We're 3rd today after 22 straight weeks at #1.",
            }
        ]
    }

    def repair(*_):
        return {
            "posts": [
                {
                    **plan["posts"][0],
                    "content": "Scoring has not started after 22 straight weeks at #1.",
                }
            ]
        }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {"war_season": {"race_ranked": False}},
            plan,
            post_fn=lambda *_: 77,
            record_fn=lambda **_: None,
            repair_fn=repair,
        )

    assert result["failed"] is False


def test_positive_war_policy_blocks_nonparticipant_nag_before_send(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    sent = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": (
                    "Battle Day 4 resets in ~14 hours. 36 of 45 members haven't touched "
                    "a deck yet, with 4 more only partial. Aaqib Javed and Fullboat "
                    "already ran their full 4 today."
                ),
            }
        ]
    }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *args: sent.append(args),
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "negative_war_participation" in result["reason"]
    assert sent == []


def test_positive_war_policy_allows_participant_recognition(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    sent = []
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": (
                    "Nine members have played war decks today. "
                    "Aaqib Javed and Fullboat completed all 4."
                ),
            }
        ]
    }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda channel_id, copy: sent.append((channel_id, copy)) or 91,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is False
    assert sent == [
        (222, "Nine members have played war decks today. Aaqib Javed and Fullboat completed all 4.")
    ]


def test_positive_war_policy_blocks_participant_count_as_roster_ratio(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "9 of 45 members have played war decks today.",
            }
        ]
    }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 92,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "negative_war_participation" in result["reason"]


def test_positive_war_policy_allows_completed_member_with_no_decks_remaining(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "Aaqib has no decks remaining after completing all 4 today.",
            }
        ]
    }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 92,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is False


def test_positive_war_policy_checks_in_game_copy_too(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "Nine members have played war decks today.",
                "clan_chat": "Last chance to use your war decks before reset.",
            }
        ]
    }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 92,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is True
    assert "negative_war_participation" in result["reason"]


def test_positive_war_policy_repair_may_remove_only_negative_counts(monkeypatch):
    monkeypatch.setenv("ELIXIR_POSITIVE_WAR_MESSAGING", "1")
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": (
                    "36 of 45 members haven't touched a deck yet. "
                    "Aaqib Javed completed all 4 today."
                ),
            }
        ]
    }

    def repair(*_):
        return {
            "posts": [
                {
                    **plan["posts"][0],
                    "content": "Aaqib Javed completed all 4 today.",
                }
            ]
        }

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {},
            plan,
            post_fn=lambda *_: 92,
            record_fn=lambda **_: None,
            repair_fn=repair,
        )

    assert result["failed"] is False


def test_copy_policy_allows_historical_rank_streak_when_current_race_is_unranked():
    plan = {
        "posts": [
            {
                "channel": "elixir",
                "leads_with": "war",
                "content": "POAP KINGS has finished 22 straight war weeks at #1.",
            }
        ]
    }
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {"war_season": {"race_ranked": False}},
            plan,
            post_fn=lambda *_: 77,
            record_fn=lambda **_: None,
        )

    assert result["failed"] is False


# ------------------------------------- record_awareness_post → channel_memory


def test_recorded_post_shows_up_in_channel_memory(engine_conn):
    store.ensure_awareness_schema(engine_conn)
    store.record_awareness_post(
        lane="elixir",
        content="dez42 is on an 11-win run",
        covers=["s2"],
        message_id=42,
        loop_number=7,
        conn=engine_conn,
    )

    mem = read_mod._channel_memory(engine_conn)
    elixir_posts = mem["elixir"]["recent_posts"]
    assert len(elixir_posts) == 1
    assert elixir_posts[0]["posted"] is True
    assert "11-win run" in elixir_posts[0]["preview"]
    # The other channel stays empty.
    assert mem["announcements"]["recent_posts"] == []
    assert (
        engine_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='communication_intents'"
        ).fetchone()
        is None
    )


def test_recorded_post_is_idempotent_by_discord_message_id(engine_conn):
    store.record_awareness_post(
        lane="elixir", content="first receipt", message_id=42, conn=engine_conn
    )
    store.record_awareness_post(
        lane="announcements", content="retry receipt", message_id=42, conn=engine_conn
    )

    rows = engine_conn.execute("SELECT lane, content_preview FROM awareness_posts").fetchall()
    assert [tuple(row) for row in rows] == [("elixir", "first receipt")]


def test_post_receipt_links_to_persisted_loop(engine_conn):
    store.record_awareness_post(
        lane="elixir", content="linked", message_id="link-1", conn=engine_conn
    )
    linked = store.attach_awareness_posts_to_loop(
        77, since="2000-01-01T00:00:00Z", conn=engine_conn
    )

    row = engine_conn.execute(
        "SELECT loop_number FROM awareness_posts WHERE discord_message_id = 'link-1'"
    ).fetchone()
    assert linked == 1
    assert row["loop_number"] == 77


def test_post_receipt_failure_is_fail_soft_but_records_incident(engine_conn):
    store.record_awareness_post(
        lane="elixir",
        content="already sent",
        covers=[object()],
        message_id=99,
        conn=engine_conn,
    )

    incident = engine_conn.execute(
        "SELECT component, summary FROM runtime_incidents WHERE component = 'awareness.record_post'"
    ).fetchone()
    assert incident is not None
    assert incident[0] == "awareness.record_post"
    assert "TypeError" in incident[1]


# --------------------------------------- last_tick_at excludes failed ticks


def test_failed_tick_does_not_advance_cursor(engine_conn):
    store.ensure_awareness_schema(engine_conn)

    # A successful silence advances the cursor.
    store.persist_thought({}, {"posts": [], "skipped_reason": "quiet"}, conn=engine_conn)
    after_silence = store.last_tick_at(conn=engine_conn)
    assert after_silence is not None

    # A FAILED tick (no posts key / _error) is persisted but must NOT move it.
    store.persist_thought({}, {"_error": "delivery boom"}, conn=engine_conn)
    after_failure = store.last_tick_at(conn=engine_conn)
    assert after_failure == after_silence

    # A real post advances it again.
    store.record_awareness_post(lane="elixir", content="hi", conn=engine_conn)
    store.persist_thought({}, {"posts": [{"channel": "elixir", "content": "hi"}]}, conn=engine_conn)
    assert store.last_tick_at(conn=engine_conn) >= after_silence
