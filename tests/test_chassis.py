"""The chassis and the scoped responder — Agentic Loop v2, Phase 1.

These pin the properties that make one shared execution path safe to build on.
The expensive lesson behind most of them is v4: many independent composers, thin
context, and a delivery path per caller produced posts that contradicted each
other across surfaces. The chassis is allowed to be a single point of failure
only because these hold.

Nothing here calls a model. The tool loop is stubbed; what is under test is the
assembly, the validation boundary, the staging shape, and the floor.
"""

from __future__ import annotations

import json

import pytest

from agent import chassis
from agent.post_validation import PostRejected, validate_clan_chat_post, validate_discord_post


def _attention(**kw):
    base = dict(
        job="welcome",
        surfaces=frozenset({chassis.SURFACE_DISCORD_ANNOUNCEMENTS, chassis.SURFACE_CLAN_CHAT}),
    )
    base.update(kw)
    return chassis.Attention(**base)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_the_system_prompt_carries_game_knowledge():
    """GAME.md's absence is not hypothetical: in the 2026-08-04 experiment both
    scoped arms called a war-league bracket move a demotion, because they had
    the voice files but not the game's mechanics."""
    system = chassis.assemble_system(_attention())
    assert "Clash Royale" in system
    # The job file is the only per-purpose prose, and it must actually load.
    assert "welcome" in system.lower()


def test_surfaces_decide_which_posting_tools_exist():
    """'Can this turn speak here?' is data on the attention. The recap-email
    case that needed a 25th prompt builder to OMIT Discord blocks is the same
    question asked the other way."""
    both = chassis.surface_tools(_attention())
    assert {t["name"] for t in both} == {"post_to_discord", "post_to_clan_chat"}

    discord_only = chassis.surface_tools(
        _attention(surfaces=frozenset({chassis.SURFACE_DISCORD_ELIXIR}))
    )
    assert {t["name"] for t in discord_only} == {"post_to_discord"}

    silent = chassis.surface_tools(_attention(surfaces=frozenset()))
    assert silent == [], "a turn with no surface cannot post anywhere"


def test_lessons_are_injected_without_the_caller_asking(monkeypatch):
    """The whole argument for a chassis. Today editorial lessons reach the
    awareness read and nothing else, so a lesson learned from a bad post never
    reaches deck review or #ask-elixir."""
    monkeypatch.setattr(
        chassis,
        "_editorial_lessons",
        lambda limit=12: [{"title": "Don't open with trophies", "body": "..."}],
    )
    context = chassis.assemble_context(_attention(), {"events": []})
    assert context["lessons"], "every chassis turn gets the lessons for free"


def test_a_broken_lesson_store_does_not_stop_the_turn(monkeypatch):
    """A turn without lessons is worse. A turn that does not happen is far
    worse — the floor still owes the clan a post."""
    monkeypatch.setattr(
        "storage.contextual_memory.list_memories",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("memory down")),
    )
    assert chassis.assemble_context(_attention(), {})["lessons"] == []


# ---------------------------------------------------------------------------
# The validation boundary — every rule here was measured, not imagined
# ---------------------------------------------------------------------------


def test_literal_escape_sequences_are_rejected():
    """Haiku emitted literal backslash-n inside the tool argument in 4 of 7
    experiment cases. Discord would have rendered them mid-sentence."""
    with pytest.raises(PostRejected, match="escape sequences"):
        validate_discord_post("**Welcome.**\\n\\nGlad to have you.", lane="announcements")


def test_a_trailing_quote_is_rejected():
    with pytest.raises(PostRejected, match="quotation mark"):
        validate_discord_post(
            '**Welcome to POAP KINGS.** Glad you are here."', lane="announcements"
        )


def test_invented_custom_emoji_are_rejected_but_unicode_is_allowed():
    """The `elixir_` namespace is checkable; Unicode shortcodes are not, and the
    brain's real posts use :crossed_swords: and :wave: correctly."""
    known = {"elixir_trophy", "elixir_cheers"}
    with pytest.raises(PostRejected, match="do not exist"):
        validate_discord_post("Welcome :elixir_party:", lane="announcements", known_emoji=known)
    # Real content from a real post must survive.
    assert validate_discord_post(
        "**Season 134 closed.** :crossed_swords: :elixir_trophy:",
        lane="announcements",
        known_emoji=known,
    )


def test_clan_chat_rejects_what_the_game_cannot_render():
    with pytest.raises(PostRejected, match="shortcode"):
        validate_clan_chat_post("Welcome to POAP KINGS :elixir_cheers:")
    with pytest.raises(PostRejected, match="markup"):
        validate_clan_chat_post("Welcome **blackberry** to the clan")
    assert validate_clan_chat_post("Welcome blackberry — Elixir Golem beatdown, nice.")


# ---------------------------------------------------------------------------
# Staging — posting is a tool call
# ---------------------------------------------------------------------------


def _run_staged(fn):
    """Execute a tool call inside a staging context, as run_turn does."""
    staging = chassis._Staging(_attention())
    token = chassis._ACTIVE_STAGING.set(staging)
    try:
        return staging, fn()
    finally:
        chassis._ACTIVE_STAGING.reset(token)


def test_posting_stages_rather_than_sends():
    """Delivery stays in ONE path. A tool that sent directly would be the second
    delivery path v4 died of — and would post before the floor was checked."""
    from agent.tool_exec import _execute_tool

    staging, raw = _run_staged(
        lambda: _execute_tool(
            "post_to_discord",
            {
                "lane": "announcements",
                "content": "**Welcome to POAP KINGS.** Glad to have you.",
                "covers_signal_keys": ["member_joined:#AAA:2026-08-04T00:00:00Z"],
            },
        )
    )
    assert json.loads(raw)["accepted"] is True
    assert len(staging.posts) == 1
    assert staging.posts[0]["channel"] == "announcements"
    assert staging.posts[0]["covers_signal_keys"] == ["member_joined:#AAA:2026-08-04T00:00:00Z"]


def test_a_rejected_post_is_not_staged_and_says_why():
    """The bounce IS the feature: the model gets the reason and fixes it."""
    from agent.tool_exec import _execute_tool

    staging, raw = _run_staged(
        lambda: _execute_tool(
            "post_to_discord",
            {"lane": "announcements", "content": "Welcome.\\n\\nGlad.", "covers_signal_keys": []},
        )
    )
    result = json.loads(raw)
    assert result["error"] == "post_rejected"
    assert "escape sequences" in result["reason"]
    assert staging.posts == [], "a rejected post must never reach the outbox"
    assert staging.rejections, "the rejection is recorded on the episode"


def test_the_clan_chat_line_rides_on_its_discord_sibling():
    """One moment, one author, two surfaces — composed in the same turn. The
    2026-07-04 rule: the in-game welcome and the Discord welcome told different
    stories when each surface did its own lookup."""
    from agent.tool_exec import _execute_tool

    def _both():
        _execute_tool(
            "post_to_discord",
            {
                "lane": "announcements",
                "content": "**Welcome.** Glad to have you.",
                "covers_signal_keys": ["k1"],
            },
        )
        return _execute_tool("post_to_clan_chat", {"content": "Welcome to POAP KINGS, blackberry."})

    staging, raw = _run_staged(_both)
    assert json.loads(raw)["accepted"] is True
    assert staging.posts[0]["clan_chat"] == ["Welcome to POAP KINGS, blackberry."]


def test_clan_chat_without_a_discord_post_tells_the_model_what_to_do():
    from agent.tool_exec import _execute_tool

    _, raw = _run_staged(lambda: _execute_tool("post_to_clan_chat", {"content": "Hello there."}))
    result = json.loads(raw)
    assert result["error"] == "no_discord_post_yet"
    assert "post_to_discord first" in result["reason"]


def test_posting_tools_are_inert_outside_a_chassis_turn():
    """Defense in depth: even if a posting tool were somehow offered to another
    workflow, there is nothing to stage into."""
    from agent.tool_exec import _execute_tool

    result = json.loads(
        _execute_tool(
            "post_to_discord", {"lane": "elixir", "content": "hi", "covers_signal_keys": []}
        )
    )
    assert "only available inside a chassis turn" in result["error"]


# ---------------------------------------------------------------------------
# Write policy
# ---------------------------------------------------------------------------


def test_posting_is_a_write_and_needs_an_opted_in_workflow():
    """`write_tools_allowed` had drifted into a declarative-only field that
    granted nothing; the gate hardcoded two workflow names instead."""
    from agent.workflow_registry import TOOL_DEFINITIONS_BY_NAME, get_workflow_spec

    assert TOOL_DEFINITIONS_BY_NAME["post_to_discord"]["side_effect"] == "write"
    assert TOOL_DEFINITIONS_BY_NAME["post_to_clan_chat"]["side_effect"] == "write"
    assert get_workflow_spec("wake_response").write_tools_allowed is True
    assert get_workflow_spec("interactive").write_tools_allowed is False


def test_a_declared_round_cap_is_the_cap_actually_used():
    """MAX_ROUNDS_BY_WORKFLOW used to be built only from specs that declared a
    response_schema, so a spec without one silently ran at the default 3.

    It cost a real turn: a chassis welcome declaring 6 rounds got 3, spent them
    on a tool call plus a validator bounce, and the forced final answer came
    back as a weekly recap for a join. A round cap has nothing to do with
    whether a workflow returns JSON.
    """
    from agent.workflow_registry import _WORKFLOW_SPECS, MAX_ROUNDS_BY_WORKFLOW

    for spec in _WORKFLOW_SPECS:
        assert MAX_ROUNDS_BY_WORKFLOW[spec.name] == spec.max_tool_rounds, spec.name


def test_surface_tools_never_leak_into_a_shared_toolset():
    """ALL_TOOLS is clanops's surface. A posting tool offered there would let a
    leadership command post to the whole clan."""
    from agent.workflow_registry import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS

    for toolset in (ALL_TOOLS, READ_TOOLS, WRITE_TOOLS):
        names = {tool["name"] for tool in toolset}
        assert not names & {"post_to_discord", "post_to_clan_chat"}


def test_the_registry_still_grants_exactly_the_old_write_workflows():
    """The gate change must be behaviour-preserving for everything that existed
    before it."""
    from agent.workflow_registry import WORKFLOW_SPECS

    granted = {name for name, spec in WORKFLOW_SPECS.items() if spec.write_tools_allowed}
    assert granted == {"clanops", "awareness", "wake_response", "wake_response_chat"}
