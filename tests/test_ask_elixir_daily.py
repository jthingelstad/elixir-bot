"""The brain-powered #ask-elixir daily post (runtime/jobs/_core.py
_ask_elixir_daily_insight), plus a few rehearsal-driven tool fixes that used to
live alongside the retired ask_discovery rotation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ------------------------------------------------------------- brain-powered daily


def _run_daily_with(generated):
    """Run _ask_elixir_daily_insight with the brain composer stubbed to return
    ``generated`` (a {"post","topic"} dict or None). Returns (mock_post, saved)."""
    import runtime.jobs._core as core

    channel = SimpleNamespace(id=1482368505058955467, name="ask-elixir", type="text")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    saved = {}

    def fake_save_message(*args, **kwargs):
        saved.update(kwargs)

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=channel.id),
        patch(
            "runtime.jobs._core._bot",
            return_value=SimpleNamespace(get_channel=lambda _id: channel),
        ),
        patch("runtime.awareness.read.build_read", return_value={"time": None}),
        patch(
            "runtime.jobs._core.elixir_agent.generate_ask_elixir_daily",
            return_value=generated,
        ),
        patch("runtime.jobs._core._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("runtime.jobs._core.db.save_message", side_effect=fake_save_message),
        patch("runtime.jobs._core._channel_msg_kwargs", return_value={}),
        patch("runtime.jobs._core._channel_scope", return_value="chan"),
    ):
        asyncio.run(core._ask_elixir_daily_insight())
    return mock_post, saved


def test_daily_posts_brain_composed_hook():
    """The daily posts exactly the brain's composed text — no editor gate,
    no template rotation."""
    post_text = (
        "🔥 dez42 is on an 11-win Path of Legends run. Try asking:\n> Show dez42's ranked decks"
    )
    mock_post, saved = _run_daily_with({"post": post_text, "topic": "ranked-run"})

    assert mock_post.await_count == 1
    posted = mock_post.await_args.args[1]
    assert posted["content"] == post_text
    assert saved.get("event_type") == "daily_clan_insight"
    assert saved.get("workflow") == "ask-elixir"


def test_daily_skips_when_no_hook():
    """No worthwhile hook (composer returns None) → nothing is posted; fail-open
    to silence, never filler."""
    mock_post, saved = _run_daily_with(None)

    assert mock_post.await_count == 0
    assert saved == {}


# --- rehearsal-driven tool fixes (2026-07-04) -------------------------------


def test_list_card_owners_display_level_math():
    import db as _db
    from storage.cards import list_card_owners

    conn = _db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)"
        )
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#OWN1', 'Owner', '2026-06-01', '2026-07-04')"
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES ('#OWN1', '2026-06-01', 'test')"
        )
        conn.execute(
            "INSERT INTO card_catalog (card_id, name, max_level, rarity, card_type, synced_at) "
            "VALUES (99001, 'Testloon', 14, 'legendary', 'troop', '2026-07-04')"
        )
        # level 14 of maxLevel 14 => display 16 (maxed)
        conn.execute(
            "INSERT INTO player_card_collection (player_tag, card_id, level, observed_at) "
            "VALUES ('#OWN1', 99001, 14, '2026-07-04')"
        )
        conn.commit()
        result = list_card_owners("Testloon", conn=conn)
        assert result["count"] == 1
        assert result["owners"][0]["member"] == "Owner"
        assert result["owners"][0]["display_level"] == 16
        # case-insensitive lookup
        assert list_card_owners("testloon", conn=conn)["count"] == 1
    finally:
        conn.close()


def test_donations_aspect_compact_and_labeled():
    import db as _db
    from agent.tool_exec import _execute_get_clan_roster

    conn = _db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)"
        )
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#DON1', 'Giver', '2026-06-01', '2026-07-04')"
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES ('#DON1', '2026-06-01', 'test')"
        )
        conn.execute(
            "INSERT INTO player_current_state (player_tag, observed_at, donations_week) "
            "VALUES ('#DON1', '2026-07-04', 456)"
        )
        conn.commit()
    finally:
        conn.close()
    result = _execute_get_clan_roster({"aspect": "donations"})
    assert result["top_donors_this_week"][0] == {
        "name": "Giver",
        "donated": 456,
        "received": 0,
    }
    assert "THIS WEEK" in result["note"]


def test_respond_in_channel_author_identity_line():
    from unittest.mock import patch

    import elixir_agent

    captured = {}

    def fake_chat(system_prompt, user_msg, **kwargs):
        captured["user_msg"] = user_msg
        return {"content": "ok"}

    with patch("elixir_agent._chat_with_tools", side_effect=fake_chat):
        elixir_agent.respond_in_channel(
            question="When did I join?",
            author_name="Vijay",
            channel_name="#ask-elixir",
            workflow="interactive",
            clan_data={},
            war_data={},
            author_identity={"member_name": "Vijay", "player_tag": "#C920YGLC2"},
        )
    msg = captured["user_msg"]
    text = msg if isinstance(msg, str) else str(msg)
    assert "#C920YGLC2" in text and "do not ask who they are" in text
