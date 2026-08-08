"""The brain-powered #ask-elixir daily post (runtime/jobs/_core.py
_ask_elixir_daily_insight), plus a few rehearsal-driven tool fixes that used to
live alongside the retired ask_discovery rotation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ------------------------------------------------------------- brain-powered daily


def _run_daily_with(generated, status=None):
    """Run _ask_elixir_daily_insight with the brain composer stubbed to return
    ``generated`` (a {"post","topic"} dict, None, or an {"_error": ...} dict).

    Returns (mock_post, saved). Pass ``status`` — a dict — to collect the job
    status calls the run made under the keys "success" and "failure"."""
    import runtime.jobs._core as core

    channel = SimpleNamespace(id=1482368505058955467, name="ask-elixir", type="text")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    saved = {}
    recorded = status if status is not None else {}
    recorded.setdefault("success", [])
    recorded.setdefault("failure", [])

    def fake_save_message(*args, **kwargs):
        saved.update(kwargs)

    fake_status = SimpleNamespace(
        mark_job_start=lambda name: None,
        mark_job_success=lambda name, summary=None: recorded["success"].append((name, summary)),
        mark_job_failure=lambda name, error: recorded["failure"].append((name, error)),
    )

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=channel.id),
        patch(
            "runtime.jobs._core._bot",
            return_value=SimpleNamespace(get_channel=lambda _id: channel),
        ),
        patch("runtime.jobs._core.runtime_status", fake_status),
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
    to silence, never filler. A CHOSEN silence is still a successful run."""
    status = {}
    mock_post, saved = _run_daily_with(None, status=status)

    assert mock_post.await_count == 0
    assert saved == {}
    assert status["failure"] == []
    assert [s for _n, s in status["success"]] == ["no hook today — skipped"]


def test_daily_compose_failure_is_a_failure_not_a_skip():
    """A FORCED silence must not be reported as a successful quiet day.

    Until 2026-08-06 the composer returned a bare None for both "no hook" and
    "composition failed", so a truncating composer produced `mark_job_success(...,
    "no hook today — skipped")` every morning. #ask-elixir went silent from
    2026-07-26 to 2026-08-06 and the job's success_count kept climbing.
    """
    status = {}
    mock_post, saved = _run_daily_with(
        {"_error": {"kind": "truncation", "detail": "LLM response truncated by max_tokens=1400"}},
        status=status,
    )

    assert mock_post.await_count == 0, "a failed compose must not post"
    assert saved == {}
    assert status["success"] == [], "a failed compose must NOT be recorded as a success"
    assert len(status["failure"]) == 1
    _name, error = status["failure"][0]
    assert "truncation" in error


def test_daily_compose_exception_is_a_failure_not_a_skip():
    """Same rule when the compose thread raises rather than returning an error."""
    import runtime.jobs._core as core

    channel = SimpleNamespace(id=1482368505058955467, name="ask-elixir", type="text")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    recorded = {"success": [], "failure": []}
    fake_status = SimpleNamespace(
        mark_job_start=lambda name: None,
        mark_job_success=lambda name, summary=None: recorded["success"].append((name, summary)),
        mark_job_failure=lambda name, error: recorded["failure"].append((name, error)),
    )

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=channel.id),
        patch(
            "runtime.jobs._core._bot",
            return_value=SimpleNamespace(get_channel=lambda _id: channel),
        ),
        patch("runtime.jobs._core.runtime_status", fake_status),
        patch("runtime.awareness.read.build_read", side_effect=RuntimeError("read exploded")),
        patch("runtime.jobs._core._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        asyncio.run(core._ask_elixir_daily_insight())

    assert mock_post.await_count == 0
    assert recorded["success"] == []
    assert len(recorded["failure"]) == 1


def test_daily_composer_surfaces_error_instead_of_collapsing_to_none():
    """generate_ask_elixir_daily must distinguish its two silences at the source."""
    import agent.workflows as workflows

    truncated = {"_error": {"kind": "truncation", "detail": "truncated by max_tokens=1400"}}
    with patch.object(workflows, "_chat_with_tools", return_value=truncated):
        assert workflows.generate_ask_elixir_daily({}) == truncated

    # An empty post with no error is the genuine no-hook day, and stays None.
    with patch.object(workflows, "_chat_with_tools", return_value={"post": "  "}):
        assert workflows.generate_ask_elixir_daily({}) is None


def test_daily_ceiling_covers_thinking_not_just_the_visible_post():
    """The ceiling has been wrong twice, for two different reasons.

    At 1400 it died mid-tool-call — every truncation stopped at exactly 1400
    while emitting a `tool_use` block. Raised to 4096, it then truncated at
    exactly 4096 having emitted NO text and NO tool_use: the whole budget went
    to extended thinking before a single visible character. Sizing this against
    the ~574-token post is what keeps failing, because thinking is drawn from
    max_tokens and the prompt does not control how much of it happens.

    A floor, not an exact value: raising it further is fine, dropping back is
    the regression. Since 2026-08-08 the ceiling lives in
    agent.core.MODEL_CALL_POLICY, so that is what this asserts.
    """
    import agent.core as core

    policy = core.policy_for("ask_elixir_daily")
    assert policy.max_tokens >= 16384
    # The ceiling alone was never the fix — effort is what bounds the thinking
    # that exhausted it. Both halves have to hold.
    assert policy.effort in ("low", "medium")


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
