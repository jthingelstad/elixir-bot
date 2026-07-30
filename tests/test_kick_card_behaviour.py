"""The card that actually removes a person from the clan.

Kick *detection* is the best-tested path in the repo — 48 real-database tests
over `engine/management.py` asserting real `kick_state` transitions. The
*card* had none. `LeaderActionButton.callback` never executed in any test,
`_ensure_leader` was uncovered, and `post_leader_action_card` appeared in the
suite only as a `patch()` target.

So nothing proved that pressing "Kicked" records the decision, and — the part
that matters — nothing proved a non-leader **cannot** press it.

These tests drive the real callback against a real database. Discord's
Interaction is faked because it is a network object, but every assertion is
about our state: what got written, what did not, and who was allowed to write
it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

import db
from runtime import leader_action_ui
from runtime.leader_action_ui import LeaderActionButton

LEADER_ID = 704062105258557511
MEMBER_ID = 999000111222333444
SECOND_LEADER_ID = 704062105258557512


@pytest.fixture
def action_db(tmp_path, monkeypatch):
    """One temp DB for the whole flow; `managed_connection` opens per call."""
    path = str(tmp_path / "cards.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def kick_card(action_db):
    """A real proposed kick recommendation, written through the real API."""
    action = db.create_leader_action_recommendation(
        action_type="kick_recommendation",
        objective="Remove an inactive member",
        prompt_text="Kick #GONE — 12 days idle.",
        rationale="No battles in 12 days on a full roster.",
        target_player_tag="#GONE",
        source_signal_key="test:kick:#GONE",
        source_signal_type="test",
        action_key="test:kick:#GONE",
        conn=action_db,
    )
    assert action and action["status"] == db.ACTION_PROPOSED
    # `managed_connection` never commits a BORROWED connection, and the callback
    # opens its own. Without this commit the card is invisible to the code under
    # test, and every assertion below passes or fails for the wrong reason.
    action_db.commit()
    return action


def _interaction(user_id: int):
    """A Discord Interaction stand-in. Only the identity matters to us."""
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="tester", roles=[]),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            send_modal=AsyncMock(),
            is_done=lambda: False,
            defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock(), id=12345),
    )


def _button(action, kind="done", label="Kicked"):
    return LeaderActionButton(
        action,
        kind=kind,
        label=label,
        style=discord.ButtonStyle.success,
        row=0,
    )


def _as_leader(is_leader: bool):
    """Patch the runtime's role check — the single gate `_ensure_leader` uses."""
    app = SimpleNamespace(_has_leader_role=lambda user: is_leader)
    return patch.object(leader_action_ui, "_runtime_app", lambda: app)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_non_leader_cannot_decide_a_kick(action_db, kick_card):
    """The assertion that did not exist: a member pressing "Kicked" is refused
    AND the action stays proposed."""
    interaction = _interaction(MEMBER_ID)
    button = _button(kick_card)

    with _as_leader(False), patch.object(leader_action_ui, "_send_ephemeral", AsyncMock()) as told:
        asyncio.run(button.callback(interaction))

    told.assert_awaited_once()
    assert "Leader role required" in told.await_args.args[1]

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_PROPOSED, "a non-leader decided a kick"
    assert not after.get("decided_at")


def test_a_leader_can_decide_a_kick(action_db, kick_card):
    interaction = _interaction(LEADER_ID)
    button = _button(kick_card)

    with (
        _as_leader(True),
        patch.object(leader_action_ui, "_apply_card_update", AsyncMock()),
        patch.object(leader_action_ui, "_dispatch_member_outreach_decision", AsyncMock()),
        patch.object(leader_action_ui, "queue_leader_action_feedback_refresh"),
    ):
        asyncio.run(button.callback(interaction))

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_DONE
    assert after["decided_at"], "a decided card must record when"
    assert str(after.get("decided_by_discord_user_id") or "") == str(LEADER_ID)


# ---------------------------------------------------------------------------
# What the decision records
# ---------------------------------------------------------------------------


def test_deciding_is_idempotent_on_a_double_click(action_db, kick_card):
    """Discord double-fires. The second press must not rewrite the record.

    The first version of this test compared only `decided_at` across two presses
    by the SAME leader. `_utcnow()` has second granularity, so both presses
    produced an identical stamp and the test passed without a guard existing at
    all — it proved the clock was slow, not that the write was safe. It now
    asserts every decision field, and the real damage case lives in the test
    below.
    """
    interaction = _interaction(LEADER_ID)
    button = _button(kick_card)
    patches = (
        _as_leader(True),
        patch.object(leader_action_ui, "_apply_card_update", AsyncMock()),
        patch.object(leader_action_ui, "_dispatch_member_outreach_decision", AsyncMock()),
        patch.object(leader_action_ui, "queue_leader_action_feedback_refresh"),
    )
    fields = ("status", "decided_at", "decided_by_discord_user_id", "decision_emoji", "outcome")
    with patches[0], patches[1], patches[2], patches[3]:
        asyncio.run(button.callback(interaction))
        first = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
        asyncio.run(button.callback(interaction))
        second = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)

    assert first["status"] == db.ACTION_DONE
    for field in fields:
        assert first[field] == second[field], f"a second press rewrote {field}"


def test_a_second_leader_cannot_overwrite_the_first_leaders_decision(action_db, kick_card):
    """The case the idempotency test above could never catch.

    Leader A marks the kick done. Leader B, looking at a card Discord has not
    refreshed yet, presses Decline. Before the open-card guard this was a blind
    last-writer-wins: A's decision vanished — status, decider and emoji all
    became B's — and because `_reconcile_management_state` clears promote/demote
    state on DONE and cannot restore it on the way back out, the card read
    "Declined" while member_management read "enacted".
    """
    with (
        _as_leader(True),
        patch.object(leader_action_ui, "_apply_card_update", AsyncMock()),
        patch.object(leader_action_ui, "_dispatch_member_outreach_decision", AsyncMock()),
        patch.object(leader_action_ui, "queue_leader_action_feedback_refresh"),
    ):
        asyncio.run(_button(kick_card).callback(_interaction(LEADER_ID)))
        after_a = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
        assert after_a["status"] == db.ACTION_DONE

        other = _interaction(SECOND_LEADER_ID)
        with patch.object(leader_action_ui, "_refresh_card_message", AsyncMock()):
            asyncio.run(_button(kick_card, kind="decline", label="Didn't").callback(other))

    after_b = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after_b["status"] == db.ACTION_DONE, "leader B overwrote leader A's decision"
    assert str(after_b["decided_by_discord_user_id"]) == str(LEADER_ID)
    assert after_b["decision_emoji"] == "✅"
    # B must be told, not silently ignored — and never shown a reason modal.
    other.response.send_modal.assert_not_awaited()
    other.response.send_message.assert_awaited()
    assert "no longer open" in other.response.send_message.await_args.args[0]


def test_a_withdrawn_card_cannot_be_marked_done(action_db, kick_card):
    """The engine withdraws stale cards by writing the row only — it never
    refreshes the Discord message. So the card in front of a leader still says
    Open with live buttons. Pressing Done must refuse, not resurrect it."""
    db.decide_leader_action(
        kick_card["action_id"],
        status=db.ACTION_REJECTED,
        discord_user_id="system:auto-withdraw",
        emoji="auto-withdraw",
        conn=action_db,
    )
    action_db.commit()

    interaction = _interaction(LEADER_ID)
    with (
        _as_leader(True),
        patch.object(leader_action_ui, "_refresh_card_message", AsyncMock()) as refreshed,
    ):
        asyncio.run(_button(kick_card).callback(interaction))

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_REJECTED, "a withdrawn card was resurrected"
    assert str(after["decided_by_discord_user_id"]) == "system:auto-withdraw"
    said = interaction.response.send_message.await_args.args[0]
    assert "withdrawn by Elixir" in said, f"leader was not told why: {said}"
    refreshed.assert_awaited_once()


def test_a_missing_action_is_reported_not_crashed(action_db, kick_card):
    """A card whose row is gone must answer, not raise inside Discord."""
    button = _button({**kick_card, "action_id": 999999})
    interaction = _interaction(LEADER_ID)

    with _as_leader(True), patch.object(leader_action_ui, "_send_ephemeral", AsyncMock()) as told:
        asyncio.run(button.callback(interaction))

    told.assert_awaited_once()
    assert "not found" in told.await_args.args[1].lower()


def test_decline_opens_a_reason_modal_and_leaves_the_action_open(action_db, kick_card):
    """Declining must collect a reason — it must not silently resolve."""
    interaction = _interaction(LEADER_ID)
    button = _button(kick_card, kind="decline", label="Didn't")

    with _as_leader(True):
        asyncio.run(button.callback(interaction))

    interaction.response.send_modal.assert_awaited_once()
    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_PROPOSED, "decline resolved the card before the reason"


def test_the_leader_gate_runs_before_anything_is_read(action_db, kick_card):
    """Order matters: refuse first, then work. If the gate ran after the DB
    read, a non-leader would still be able to probe card state."""
    interaction = _interaction(MEMBER_ID)
    button = _button(kick_card)

    with (
        _as_leader(False),
        patch.object(leader_action_ui, "_send_ephemeral", AsyncMock()),
        patch.object(db, "get_leader_action_by_id") as read,
    ):
        asyncio.run(button.callback(interaction))

    read.assert_not_called()


# ---------------------------------------------------------------------------
# The storage-layer guard
#
# The UI checks `action_is_open` before writing, so the tests above pass even
# with the storage guard removed — they cover the wrong layer. These drive
# `db.decide_leader_action` directly, which is where the compare-and-set lives
# and where a cross-connection race actually resolves.
# ---------------------------------------------------------------------------


def _decide(action_db, kick_card, *, status, who, emoji, **kw):
    return db.decide_leader_action(
        kick_card["action_id"],
        status=status,
        discord_user_id=who,
        emoji=emoji,
        conn=action_db,
        **kw,
    )


def test_storage_refuses_a_decision_on_an_already_decided_card(action_db, kick_card):
    first = _decide(action_db, kick_card, status=db.ACTION_DONE, who=LEADER_ID, emoji="✅")
    assert first and first["status"] == db.ACTION_DONE

    second = _decide(
        action_db, kick_card, status=db.ACTION_REJECTED, who=SECOND_LEADER_ID, emoji="❌"
    )
    assert second is None, "storage accepted a decision on a closed card"

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_DONE
    assert str(after["decided_by_discord_user_id"]) == str(LEADER_ID)
    assert after["decision_emoji"] == "✅"


def test_storage_refuses_to_resurrect_a_withdrawn_card(action_db, kick_card):
    _decide(
        action_db,
        kick_card,
        status=db.ACTION_REJECTED,
        who="system:auto-withdraw",
        emoji="auto-withdraw",
    )
    assert _decide(action_db, kick_card, status=db.ACTION_DONE, who=LEADER_ID, emoji="✅") is None
    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert str(after["decided_by_discord_user_id"]) == "system:auto-withdraw"


def test_removing_a_reaction_cannot_take_back_a_button_press(action_db, kick_card):
    """Both paths store "✅". Removing a ✅ reaction is meant to take back a ✅
    REACTION; before this guard it also took back a ✅ BUTTON press, so a leader
    who clicked Done and later added-then-removed a reaction silently reopened
    their own decided card. The reopen is only a partial inverse — it leaves
    decision_note, expires_at, premise_rejected and the cleared management
    state behind — so the card came back in a state nothing else agreed with.
    """
    from storage.leader_actions import clear_leader_action_decision_by_message

    db.update_leader_action_message(kick_card["action_id"], source_message_id=777, conn=action_db)
    _decide(
        action_db,
        kick_card,
        status=db.ACTION_DONE,
        who=LEADER_ID,
        emoji="✅",
        decided_via=db.DECIDED_VIA_BUTTON,
    )
    action_db.commit()

    clear_leader_action_decision_by_message(
        777, discord_user_id=LEADER_ID, emoji="✅", conn=action_db
    )

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_DONE, "a reaction removal reopened a button decision"


def test_removing_a_reaction_still_takes_back_a_reaction_decision(action_db, kick_card):
    """The guard must not break the path it is protecting."""
    from storage.leader_actions import clear_leader_action_decision_by_message

    db.update_leader_action_message(kick_card["action_id"], source_message_id=778, conn=action_db)
    _decide(
        action_db,
        kick_card,
        status=db.ACTION_DONE,
        who=LEADER_ID,
        emoji="✅",
        decided_via=db.DECIDED_VIA_REACTION,
    )
    action_db.commit()

    clear_leader_action_decision_by_message(
        778, discord_user_id=LEADER_ID, emoji="✅", conn=action_db
    )

    after = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert after["status"] == db.ACTION_PROPOSED, "a leader could not take back their reaction"


def test_a_reopened_card_can_be_decided_again(action_db, kick_card):
    """The guard must not trap a card. Reopening is the supported escape hatch
    for a misclick, so decide → reopen → decide has to work end to end."""
    _decide(action_db, kick_card, status=db.ACTION_DONE, who=LEADER_ID, emoji="✅")
    action_db.execute(
        "UPDATE leader_action_recommendations SET status=?, decided_at=NULL, "
        "decided_by_discord_user_id=NULL, decision_emoji=NULL WHERE action_id=?",
        (db.ACTION_PROPOSED, kick_card["action_id"]),
    )
    action_db.commit()

    again = _decide(action_db, kick_card, status=db.ACTION_REJECTED, who=LEADER_ID, emoji="❌")
    assert again is not None, "a reopened card must be decidable"
    assert again["status"] == db.ACTION_REJECTED


# ---------------------------------------------------------------------------
# Posting the card
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id: int):
        self.id = message_id


class _FakeChannel:
    """Records what was sent. `send` is the only surface the poster touches."""

    def __init__(self):
        self.sent = []
        self._next_id = 500

    async def send(self, content=None, *, embed=None, view=None):
        self._next_id += 1
        self.sent.append({"content": content, "embed": embed, "view": view})
        return _FakeMessage(self._next_id)


def test_posting_a_kick_card_records_the_message_id(action_db, kick_card):
    """The card must be findable by the message a leader reacts to.

    `source_message_id` is the only link from a ✅ reaction back to the action.
    If the post succeeds but the write doesn't, the card looks fine on screen
    and every reaction on it silently does nothing.
    """
    channel = _FakeChannel()

    asyncio.run(leader_action_ui.post_leader_action_card(channel, kick_card))

    assert channel.sent and channel.sent[0]["view"] is not None, "card posted without buttons"
    stored = db.get_leader_action_by_id(kick_card["action_id"], conn=action_db)
    assert str(stored["source_message_id"]) == "501", "the posted message id was not recorded"

    # End-to-end: a ✅ on that message resolves back to this action. This is the
    # whole point of the write above, so assert it through the real reaction path.
    decided = db.decide_leader_action_by_message(
        501, status=db.ACTION_DONE, discord_user_id=LEADER_ID, emoji="✅", conn=action_db
    )
    assert decided and decided["action_id"] == kick_card["action_id"]


def test_the_posted_card_offers_both_decisions_wired_to_this_action(kick_card):
    """A kick card a leader can't decline is a kick card that only goes one way."""
    view = leader_action_ui.leader_action_view_for(kick_card)
    buttons = [c for c in view.children if isinstance(c, LeaderActionButton)]

    kinds = {b.kind for b in buttons}
    assert {"done", "decline"} <= kinds, f"kick card is missing a decision: {sorted(kinds)}"
    assert all(b.action_id == kick_card["action_id"] for b in buttons), (
        "a button is wired to a different action than the card it sits on"
    )
