"""Tests for arena-relay leader action Discord UI composition."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import db
from runtime import leader_action_ui
from runtime.leader_action_ui import (
    CopyEditModal,
    DecisionReasonModal,
    LeaderActionView,
    NoteModal,
    build_leader_action_embed,
    leader_action_view_for,
)


def _action(action_type: str, **overrides):
    base = {
        "action_id": 12,
        "action_type": action_type,
        "objective": "test",
        "status": db.ACTION_PROPOSED,
        "target_player_tag": "#ABC123",
        "target_player_name": "King Levy",
        "prompt_text": "Take the recommended action.",
        "rationale": "The data supports it.",
        "copy_current_text": "Short Clash copy.",
        "baseline": {
            "member": {"name": "King Levy", "role": "member"},
            "war_day": {"phase": "battle", "untouched_count": 3},
        },
    }
    base.update(overrides)
    return base


def _labels(view: LeaderActionView) -> list[str | None]:
    return [getattr(child, "label", None) for child in view.children]


def test_welcome_relay_has_copy_controls_but_no_defer():
    view = LeaderActionView(_action("welcome_relay"))
    labels = _labels(view)

    assert "Posted" in labels
    assert "Skip" in labels
    assert "Edit Copy" in labels
    assert "Profile" in labels
    assert "Add Note" in labels
    assert not any(isinstance(child, discord.ui.Select) for child in view.children)


def test_war_nudge_has_defer_and_detail_controls_without_copy_editor():
    view = LeaderActionView(_action("war_nudge_recommendation", copy_current_text=None))
    labels = _labels(view)

    assert "Nudged" in labels
    assert "Decline" in labels
    assert "Edit Copy" not in labels
    assert "Profile" in labels
    assert "War Detail" in labels
    assert any(isinstance(child, discord.ui.Select) for child in view.children)


def test_role_action_uses_multi_row_decision_copy_defer_and_context_controls():
    view = LeaderActionView(_action("kick_recommendation"))
    rows = {getattr(child, "label", None): child.row for child in view.children}

    assert rows["Kicked"] == 0
    assert rows["Decline"] == 0
    assert rows["Edit Copy"] == 1
    assert rows["Preview Copy"] == 1
    assert rows["Profile"] == 3
    assert rows["War Detail"] == 3
    assert rows["Add Note"] == 4
    assert any(isinstance(child, discord.ui.Select) and child.row == 2 for child in view.children)


def test_terminal_action_has_no_controls():
    view = LeaderActionView(_action("promotion_recommendation", status=db.ACTION_DONE))

    assert view.children == []
    assert leader_action_view_for(_action("promotion_recommendation", status=db.ACTION_DONE)) is None


def test_embed_marks_test_cards_explicitly():
    embed = build_leader_action_embed(_action("celebration_relay", is_test=True))

    assert embed.title.startswith("TEST R12")
    assert "test card" in embed.footer.text


def test_text_inputs_use_paragraph_style():
    copy_modal = CopyEditModal(_action("welcome_relay"), ["Short Clash copy."])
    decline_modal = DecisionReasonModal(_action("kick_recommendation"))
    note_modal = NoteModal(_action("in_game_relay"))

    assert copy_modal.inputs[0].style == discord.TextStyle.paragraph
    assert decline_modal.reason.style == discord.TextStyle.paragraph
    assert note_modal.note.style == discord.TextStyle.paragraph


def test_card_update_removes_view_without_sending_confirmation():
    interaction = SimpleNamespace(
        response=SimpleNamespace(
            is_done=lambda: False,
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
    )
    action = _action("promotion_recommendation", status=db.ACTION_DONE)

    asyncio.run(leader_action_ui._apply_card_update(interaction, action))

    interaction.response.edit_message.assert_awaited_once()
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["view"] is None
    interaction.response.defer.assert_not_awaited()
