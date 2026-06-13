"""Tests for arena-relay leader action Discord UI composition."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    assert "Add Note" in labels
    assert "Preview Copy" not in labels
    assert "Profile" not in labels
    assert "War Detail" not in labels
    assert not any(isinstance(child, discord.ui.Select) for child in view.children)


def test_war_nudge_action_type_is_not_registered():
    assert "war_nudge_recommendation" not in leader_action_ui.leader_action_type_choices()


def test_role_action_uses_multi_row_decision_copy_defer_and_note_controls():
    view = LeaderActionView(_action("kick_recommendation"))
    rows = {getattr(child, "label", None): child.row for child in view.children}

    assert rows["Kicked"] == 0
    assert rows["Decline"] == 0
    assert rows["Edit Copy"] == 1
    assert rows["Add Note"] == 3
    assert "Preview Copy" not in rows
    assert "Profile" not in rows
    assert "War Detail" not in rows
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


def test_restore_refreshes_open_card_components():
    action = _action(
        "kick_recommendation",
        source_message_id="456",
        target_channel_id="123",
    )
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    bot = SimpleNamespace(get_channel=Mock(return_value=channel), add_view=Mock())

    with patch.object(leader_action_ui.db, "list_leader_actions", return_value=[action]):
        restored = asyncio.run(leader_action_ui.restore_leader_action_views(bot))

    assert restored == 1
    bot.add_view.assert_called_once()
    assert bot.add_view.call_args.kwargs["message_id"] == 456
    channel.fetch_message.assert_awaited_once_with(456)
    message.edit.assert_awaited_once()
    assert isinstance(message.edit.await_args.kwargs["view"], LeaderActionView)


def test_restore_refreshes_terminal_cards_without_components():
    action = _action(
        "kick_recommendation",
        status=db.ACTION_REJECTED,
        source_message_id="456",
        target_channel_id="123",
    )
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    bot = SimpleNamespace(get_channel=Mock(return_value=channel), add_view=Mock())

    def fake_list_leader_actions(*, status=None, limit=50):
        return [] if status == db.ACTION_PROPOSED else [action]

    with patch.object(leader_action_ui.db, "list_leader_actions", side_effect=fake_list_leader_actions):
        restored = asyncio.run(leader_action_ui.restore_leader_action_views(bot))

    assert restored == 0
    bot.add_view.assert_not_called()
    channel.fetch_message.assert_awaited_once_with(456)
    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["view"] is None
