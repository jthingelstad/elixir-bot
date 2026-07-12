"""The brain's live delivery layer (runtime/awareness/deliver.py) + the two
correctness linchpins it depends on: record_awareness_post feeding channel_memory
(dedup) and last_tick_at excluding failed ticks (fail-hard, catch-up)."""

from __future__ import annotations

from unittest.mock import patch

from runtime.awareness import deliver as deliver_mod
from runtime.awareness import read as read_mod
from runtime.awareness import store

_LANES = {
    "announcements": {"channel_id": 111, "channel_name": "announcements", "leadership": False},
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
    plan = {"posts": [
        {"channel": "announcements", "content": "Welcome Zed!", "covers_signal_keys": ["s1"]},
        {"channel": "elixir", "content": ["part a", "part b"], "covers_signal_keys": ["s2"]},
    ]}
    read = {"hard_post_signals": [{"signal_key": "s1"}]}

    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(read, plan, post_fn=post_fn, record_fn=record_fn)

    assert result == {"delivered": 2, "failed": False, "reason": None, "uncovered_hard": []}
    assert sent == [(111, "Welcome Zed!"), (222, "part a\n\npart b")]
    assert [r["lane"] for r in recorded] == ["announcements", "elixir"]
    assert recorded[0]["message_id"] == 901


def test_unknown_channel_fails_tick():
    read, plan = {}, {"posts": [{"channel": "leader-lounge", "content": "x"}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: 1, record_fn=lambda **_: None)
    assert result["failed"] is True
    assert "leader-lounge" in result["reason"]


def test_send_returning_none_fails_tick():
    read, plan = {}, {"posts": [{"channel": "elixir", "content": "x"}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: None, record_fn=lambda **_: None)
    assert result["failed"] is True


def test_uncovered_hard_post_floor_fails_tick():
    # The post covers s1 but the floor also demands s2 → tick fails, and the
    # (delivered) post is still recorded so it isn't re-sent next loop.
    record_fn, recorded = _recorder()
    read = {"hard_post_signals": [{"signal_key": "s1"}, {"signal_key": "s2"}]}
    plan = {"posts": [{"channel": "elixir", "content": "hi", "covers_signal_keys": ["s1"]}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: 5, record_fn=record_fn)
    assert result["failed"] is True
    assert result["uncovered_hard"] == ["s2"]
    assert len(recorded) == 1  # the delivered post was recorded before the failure


def test_relay_failure_does_not_fail_the_post():
    def bad_relay(post, channel_name):
        raise RuntimeError("relay boom")

    plan = {"posts": [{"channel": "elixir", "content": "big news",
                       "relay_to_clan_chat": True}]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            {}, plan, post_fn=lambda *_: 7, record_fn=lambda **_: None, relay_fn=bad_relay)
    assert result["failed"] is False
    assert result["delivered"] == 1


def test_member_join_always_relays_even_without_brain_flag():
    """A new-member join ALWAYS raises an in-game welcome relay, even if the brain
    forgot to set relay_to_clan_chat — the deterministic backstop."""
    relayed = []

    def relay_fn(post, channel_name):
        relayed.append(post.get("covers_signal_keys"))

    # Brain welcomed BigNorton in #announcements but did NOT flag the relay.
    plan = {"posts": [{
        "channel": "announcements", "content": "Welcome BigNorton! 10,090 trophies.",
        "covers_signal_keys": ["member_joined:#CV20JCY0V:t"],
        "relay_to_clan_chat": False,
    }]}
    read = {"hard_post_signals": [
        {"signal_key": "member_joined:#CV20JCY0V:t", "event_type": "member_joined"}
    ]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        result = deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: 5, record_fn=lambda **_: None, relay_fn=relay_fn)

    assert result["failed"] is False
    assert relayed == [["member_joined:#CV20JCY0V:t"]], "join must force a welcome relay"


def test_non_join_post_without_flag_does_not_relay():
    """A non-join post that the brain didn't flag must NOT be force-relayed — the
    backstop is join-specific, not a blanket relay-everything."""
    relayed = []
    plan = {"posts": [{
        "channel": "elixir", "content": "routine milestone",
        "covers_signal_keys": ["card_level_milestone:#X:1"],
        "relay_to_clan_chat": False,
    }]}
    read = {"hard_post_signals": [
        {"signal_key": "member_joined:#OTHER:t", "event_type": "member_joined"}
    ]}
    with patch.object(deliver_mod.engine_compose, "channels", return_value=_LANES):
        deliver_mod.deliver_posts(
            read, plan, post_fn=lambda *_: 5, record_fn=lambda **_: None,
            relay_fn=lambda p, c: relayed.append(p))
    assert relayed == [], "only the post covering the join relays, not unrelated posts"


# ------------------------------------- record_awareness_post → channel_memory

def test_recorded_post_shows_up_in_channel_memory(engine_conn):
    store.ensure_awareness_schema(engine_conn)
    store.record_awareness_post(
        lane="elixir", content="dez42 is on an 11-win run", covers=["s2"],
        message_id=42, loop_number=7, conn=engine_conn)

    mem = read_mod._channel_memory(engine_conn)
    elixir_intents = mem["elixir"]["recent_intents"]
    assert len(elixir_intents) == 1
    assert elixir_intents[0]["intent_type"] == "awareness:post"
    assert elixir_intents[0]["posted"] is True
    assert "11-win run" in elixir_intents[0]["preview"]
    # The other channel stays empty.
    assert mem["announcements"]["recent_intents"] == []


# --------------------------------------- last_tick_at excludes failed ticks

def test_failed_tick_does_not_advance_cursor(engine_conn):
    store.ensure_awareness_schema(engine_conn)

    # A successful silence advances the cursor.
    store.persist_thought({}, {"posts": [], "skipped_reason": "quiet"},
                          conn=engine_conn)
    after_silence = store.last_tick_at(conn=engine_conn)
    assert after_silence is not None

    # A FAILED tick (no posts key / _error) is persisted but must NOT move it.
    store.persist_thought({}, {"_error": "delivery boom"}, conn=engine_conn)
    after_failure = store.last_tick_at(conn=engine_conn)
    assert after_failure == after_silence

    # A real post advances it again.
    store.record_awareness_post(lane="elixir", content="hi", conn=engine_conn)
    store.persist_thought({}, {"posts": [{"channel": "elixir", "content": "hi"}]},
                          conn=engine_conn)
    assert store.last_tick_at(conn=engine_conn) >= after_silence
