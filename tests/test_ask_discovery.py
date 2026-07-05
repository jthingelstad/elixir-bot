"""Capability-discovery rotation for the #ask-elixir daily post
(runtime/jobs/ask_discovery.py + the rewired _ask_elixir_daily_insight)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
from runtime.jobs import ask_discovery


def _conn():
    return db.get_connection()


# ------------------------------------------------------------- catalog

def test_catalog_completeness():
    keys = [c["key"] for c in ask_discovery.CATALOG]
    assert len(keys) == len(set(keys))
    assert len(keys) >= 8
    for cat in ask_discovery.CATALOG:
        assert cat["blurb"]
        assert len(cat["questions"]) >= 2
        assert callable(cat["nugget"])


def test_nuggets_empty_db_safe():
    conn = _conn()
    try:
        for cat in ask_discovery.CATALOG:
            result = cat["nugget"](conn)  # must not raise on the fixture DB
            assert result is None or isinstance(result, dict)
    finally:
        conn.close()


def test_screenshots_nugget_always_available():
    conn = _conn()
    try:
        assert ask_discovery._screenshots(conn)
    finally:
        conn.close()


# ------------------------------------------------------------- rotation

def test_last_category_key_parses_marker():
    assert ask_discovery.last_category_key("posted category=ranked") == "ranked"
    assert ask_discovery.last_category_key("no fresh insight") is None
    assert ask_discovery.last_category_key(None) is None


def test_rotation_skips_empty_and_cycles():
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)"
        )
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#ROT1', 'Rotator', '2026-06-01', '2026-07-04')"
        )
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES ('#ROT1', '2026-06-01', 'test')"
        )
        conn.commit()

        # From a cold start: war_now/cards are empty on the fixture, so the
        # first pick must land on a category with data (my_stats via roster).
        cat, facts = ask_discovery.pick_category(conn, None)
        assert cat["key"] == "my_stats"
        assert facts["member_count"] == 1

        # Continue the cycle: next pick starts AFTER my_stats and lands on
        # the next non-empty category — never my_stats again this cycle.
        cat2, _ = ask_discovery.pick_category(conn, "my_stats")
        assert cat2["key"] != "my_stats"

        # From the last catalog entry it wraps around deterministically.
        cat3, _ = ask_discovery.pick_category(conn, ask_discovery.CATALOG[-1]["key"])
        assert cat3["key"] == "my_stats"
    finally:
        conn.close()


# ------------------------------------------------------------- rendering

def test_fallback_post_shape():
    cat = ask_discovery.CATALOG[0]
    text = ask_discovery.fallback_post(cat, {"current_fame": 27850})
    assert cat["blurb"] in text
    assert "27850" in text
    assert "Try asking:" in text
    for q in cat["questions"]:
        assert f"> {q}" in text


def test_compose_context_bans_invention_and_pins_questions():
    cat = ask_discovery.CATALOG[6]  # ranked
    ctx = ask_discovery.compose_context(cat, {"top_ranked_player": "Atternam"})
    assert "Do not invent" in ctx
    assert "Atternam" in ctx
    for q in cat["questions"]:
        assert q in ctx


# ------------------------------------------------------------- gate wiring

def test_job_routes_copy_through_editor_gate():
    """The composed post is judged; a 'revise' verdict triggers exactly one
    recompose, and the verdict is recorded with the intent_id=0 marker."""
    import runtime.jobs._core as core

    channel = SimpleNamespace(id=1482368505058955467, name="ask-elixir", type="text")
    generated = {"event_type": "channel_update", "summary": "s",
                 "content": "Elixir watches the war. Try asking: > How's the war going?"}
    revised = {"event_type": "channel_update", "summary": "s",
               "content": "REVISED COPY. Try asking: > How's the war going?"}
    verdicts = [
        {"verdict": "revise", "dimensions": {}, "critique": "too flat"},
        {"verdict": "pass", "dimensions": {}, "critique": ""},
    ]
    gen_calls = []

    def fake_generate(name, lane, ctx, **kwargs):
        gen_calls.append(ctx)
        return revised if len(gen_calls) > 1 else generated

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    saved = {}

    def fake_save_message(*args, **kwargs):
        saved.update(kwargs)

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=channel.id),
        patch("runtime.jobs._core._bot", return_value=SimpleNamespace(get_channel=lambda _id: channel)),
        patch("runtime.jobs._core._channel_config_by_key", return_value={
            "name": "#ask-elixir", "lane_key": "ask-elixir",
        }),
        patch("runtime.jobs._core.build_lane_memory_context", return_value={}),
        patch("runtime.jobs._core.db.list_channel_messages", return_value=[]),
        patch("runtime.jobs._core.elixir_agent.generate_channel_update", side_effect=fake_generate),
        patch("runtime.jobs._core._runtime_app", return_value=SimpleNamespace(
            _entry_posts=lambda r: [r["content"]] if r and r.get("content") else [],
        )),
        patch("engine.editor.enabled", return_value=True),
        patch("engine.editor.judge", side_effect=verdicts) as mock_judge,
        patch("runtime.jobs._core._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("runtime.jobs._core.db.save_message", side_effect=fake_save_message),
        patch("runtime.jobs._core._channel_msg_kwargs", return_value={}),
        patch("runtime.jobs._core._channel_scope", return_value="chan"),
    ):
        asyncio.run(core._ask_elixir_daily_insight())

    assert len(gen_calls) == 2, "revise verdict must trigger exactly one recompose"
    assert "EDITOR CRITIQUE" in gen_calls[1]
    assert mock_judge.call_count == 2
    posted = mock_post.await_args.args[1]
    assert posted["content"] == revised["content"]
    assert saved.get("event_type") == "daily_clan_insight"

    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT intent_id, verdict, final_copy FROM editor_verdicts"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["intent_id"] == 0  # the no-intent marker
        assert rows[0]["verdict"] == "revise"
        assert rows[0]["final_copy"] == revised["content"]
    finally:
        conn.close()


# --- rehearsal-driven tool fixes (2026-07-04) -------------------------------

def test_list_card_owners_display_level_math():
    import db as _db
    from storage.cards import list_card_owners

    conn = _db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
                     "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)")
        conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                     "VALUES ('#OWN1', 'Owner', '2026-06-01', '2026-07-04')")
        conn.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
                     "VALUES ('#OWN1', '2026-06-01', 'test')")
        conn.execute("INSERT INTO card_catalog (card_id, name, max_level, rarity, card_type, synced_at) "
                     "VALUES (99001, 'Testloon', 14, 'legendary', 'troop', '2026-07-04')")
        # level 14 of maxLevel 14 => display 16 (maxed)
        conn.execute("INSERT INTO player_card_collection (player_tag, card_id, level, observed_at) "
                     "VALUES ('#OWN1', 99001, 14, '2026-07-04')")
        conn.commit()
        result = list_card_owners('Testloon', conn=conn)
        assert result["count"] == 1
        assert result["owners"][0]["member"] == "Owner"
        assert result["owners"][0]["display_level"] == 16
        # case-insensitive lookup
        assert list_card_owners('testloon', conn=conn)["count"] == 1
    finally:
        conn.close()


def test_donations_aspect_compact_and_labeled():
    import db as _db
    from agent.tool_exec import _execute_get_clan_roster

    conn = _db.get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
                     "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)")
        conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                     "VALUES ('#DON1', 'Giver', '2026-06-01', '2026-07-04')")
        conn.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
                     "VALUES ('#DON1', '2026-06-01', 'test')")
        conn.execute("INSERT INTO player_current_state (player_tag, observed_at, donations_week) "
                     "VALUES ('#DON1', '2026-07-04', 456)")
        conn.commit()
    finally:
        conn.close()
    result = _execute_get_clan_roster({"aspect": "donations"})
    assert result["top_donors_this_week"][0] == {"name": "Giver", "donated": 456, "received": 0}
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
            question="When did I join?", author_name="Vijay",
            channel_name="#ask-elixir", workflow="interactive",
            clan_data={}, war_data={},
            author_identity={"member_name": "Vijay", "player_tag": "#C920YGLC2"},
        )
    msg = captured["user_msg"]
    text = msg if isinstance(msg, str) else str(msg)
    assert "#C920YGLC2" in text and "do not ask who they are" in text
