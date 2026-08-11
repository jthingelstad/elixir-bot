"""Interactive Discord UI for actions leader actions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

import db
from runtime.leader_action_feedback import queue_leader_action_feedback_refresh
from runtime.leader_note_interpreter import queue_leader_note_interpretation

log = logging.getLogger("elixir.leader_action_ui")

LEADER_ACTION_UI_VERSION = "leader-action-ui-v1"
CLASH_COPY_MAX_LENGTH = 200


@dataclass(frozen=True)
class LeaderActionTypeSpec:
    action_type: str
    label: str
    emoji: str
    color: int
    done_label: str
    decline_label: str = "Decline"
    allow_copy_edit: bool = False
    copy_field_label: str = "Clash Copy"
    # When set, the card is a CLASSIFICATION (not binary done/decline): each
    # tuple is (button_kind, label, emoji). Every choice resolves the card as
    # done-with-a-classification. Used by departure_verification.
    classify_choices: tuple[tuple[str, str, str], ...] = ()


ACTION_SPECS: dict[str, LeaderActionTypeSpec] = {
    "welcome_relay": LeaderActionTypeSpec(
        "welcome_relay",
        "Welcome Relay",
        "👋",
        0x36A3FF,
        "Posted",
        "Skip",
        allow_copy_edit=True,
    ),
    "discord_invite_relay": LeaderActionTypeSpec(
        "discord_invite_relay",
        "Discord Invite Relay",
        "💬",
        0x5865F2,
        "Posted All",
        "Skip",
        allow_copy_edit=True,
        copy_field_label="Clash Copy Sequence",
    ),
    "in_game_relay": LeaderActionTypeSpec(
        "in_game_relay",
        "In-Game Relay",
        "📣",
        0xF1C40F,
        "Posted",
        "Skip",
        allow_copy_edit=True,
    ),
    "member_outreach": LeaderActionTypeSpec(
        "member_outreach",
        "Profile Outreach",
        "📧",
        0x9B59B6,
        "Send DM",
        "Skip",
        allow_copy_edit=True,
        copy_field_label="DM to member",
    ),
    "promotion_recommendation": LeaderActionTypeSpec(
        "promotion_recommendation",
        "Promotion Recommendation",
        "⬆️",
        0x2ECC71,
        "Promoted",
        allow_copy_edit=True,
    ),
    "demotion_recommendation": LeaderActionTypeSpec(
        "demotion_recommendation",
        "Demotion Recommendation",
        "⬇️",
        0xF39C12,
        "Demoted",
        allow_copy_edit=True,
    ),
    "kick_recommendation": LeaderActionTypeSpec(
        "kick_recommendation",
        "Kick Recommendation",
        "🚪",
        0xE74C3C,
        "Kicked",
        allow_copy_edit=True,
    ),
    "celebration_relay": LeaderActionTypeSpec(
        "celebration_relay",
        "Celebration Relay",
        "🎉",
        0x9B59B6,
        "Posted",
        "Skip",
        allow_copy_edit=True,
    ),
    "memory_review": LeaderActionTypeSpec(
        "memory_review",
        "Memory Review",
        "🧠",
        0x3498DB,
        "Fixed",
        "Dismiss",
    ),
    "departure_verification": LeaderActionTypeSpec(
        "departure_verification",
        "Departure — Kicked, Left, or Ignore?",
        "🔍",
        0x7F8C8D,
        "Verified",
        classify_choices=(
            ("classify_kick", "Kicked", "🚪"),
            ("classify_leave", "Left", "🚶"),
            ("classify_ignore", "Ignore", "🔕"),
        ),
    ),
}

# Button kind → the classification value classify_departure records.
_CLASSIFY_KIND_VALUE = {
    "classify_leave": "leave",
    "classify_kick": "kick",
    "classify_ignore": "ignore",
}


def leader_action_spec(action_type: str | None) -> LeaderActionTypeSpec:
    clean = (action_type or "").strip()
    return ACTION_SPECS.get(
        clean,
        LeaderActionTypeSpec(clean or "leader_action", "Leader Action", "⚡", 0x95A5A6, "Done"),
    )


def leader_action_type_choices() -> list[str]:
    return list(ACTION_SPECS)


def _clip(text: str | None, limit: int = 900) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean or "Not provided."
    return clean[: limit - 3].rstrip() + "..."


def _split_copy_messages(copy_text: str | None) -> list[str]:
    lines = [line.strip() for line in str(copy_text or "").splitlines() if line.strip()]
    if not lines and copy_text:
        lines = [" ".join(str(copy_text).split())]
    return lines[:5]


def action_copy_messages(action: dict, copy_messages: list[str] | None = None) -> list[str]:
    if copy_messages is not None:
        return [str(item).strip() for item in copy_messages if str(item).strip()]
    return _split_copy_messages(action.get("copy_current_text") or action.get("copy_original_text"))


def _status_label(action: dict) -> str:
    status = action.get("status") or db.ACTION_PROPOSED
    if status == db.ACTION_DONE:
        return "Done"
    if status == db.ACTION_REJECTED:
        return "Declined"
    return "Open"


def action_is_open(action: dict) -> bool:
    return (action.get("status") or db.ACTION_PROPOSED) == db.ACTION_PROPOSED


def _format_target(action: dict) -> str:
    name = action.get("target_player_name")
    tag = action.get("target_player_tag")
    if name and tag:
        return f"{name} (`{tag}`)"
    return name or (f"`{tag}`" if tag else "Clan")


def _note_interpretation(action: dict) -> dict | None:
    """The applied interpretation blob for this card, if one exists."""
    interp = action.get("note_interpret")
    return interp if isinstance(interp, dict) else None


def _has_undoable_interpretation(action: dict) -> bool:
    if (action.get("note_interpret_status") or "") != "interpreted":
        return False
    interp = _note_interpretation(action)
    return bool(interp and interp.get("applied"))


def _add_note_interpret_field(embed: discord.Embed, action: dict) -> None:
    status = action.get("note_interpret_status") or ""
    if status == "interpreted":
        interp = _note_interpretation(action) or {}
        reading = str(interp.get("reading") or "").strip()
        if reading:
            embed.add_field(name="Read as", value=_clip(reading, 300), inline=False)
    elif status == "failed":
        embed.add_field(name="Read as", value="Not interpreted", inline=False)
    elif status == "undone":
        embed.add_field(name="Read as", value="(undone)", inline=False)


def build_leader_action_embed(
    action: dict, *, copy_messages: list[str] | None = None
) -> discord.Embed:
    spec = leader_action_spec(action.get("action_type"))
    prefix = "TEST " if action.get("is_test") else ""
    title = f"{prefix}R{action.get('action_id')} {spec.emoji} {spec.label}"
    embed = discord.Embed(
        title=title,
        description=f"**Target:** {_format_target(action)}\n**Status:** {_status_label(action)}",
        color=spec.color,
    )
    embed.add_field(name="Decision", value=_clip(action.get("prompt_text"), 900), inline=False)
    if action.get("rationale"):
        embed.add_field(name="Why", value=_clip(action.get("rationale"), 900), inline=False)
    copies = action_copy_messages(action, copy_messages)
    if copies:
        if len(copies) == 1:
            value = f"```text\n{_clip(copies[0], CLASH_COPY_MAX_LENGTH)}\n```"
        else:
            value = "\n".join(
                f"`{idx}.` {_clip(item, CLASH_COPY_MAX_LENGTH)}"
                for idx, item in enumerate(copies, 1)
            )
        embed.add_field(name=spec.copy_field_label, value=value[:1024], inline=False)
    note = action.get("decision_note")
    if note:
        embed.add_field(name="Leader Note", value=_clip(note, 700), inline=False)
    _add_note_interpret_field(embed, action)
    footer = f"{action.get('action_type') or 'leader_action'} | {LEADER_ACTION_UI_VERSION}"
    if action.get("is_test"):
        footer += " | test card"
    embed.set_footer(text=footer)
    return embed


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def _runtime_app():
    from runtime import app as runtime_app

    return runtime_app


async def _ensure_leader(interaction: discord.Interaction) -> bool:
    app = _runtime_app()
    if app._has_leader_role(interaction.user):
        return True
    await _send_ephemeral(interaction, "Leader role required.")
    return False


_SYSTEM_DECIDERS = {
    "system": "auto-settled by Elixir (no verification in time)",
    "system:auto-withdraw": "withdrawn by Elixir — the evidence behind it no longer holds",
}


async def _report_already_decided(
    interaction: discord.Interaction,
    action: dict | None,
    *,
    action_id: int | None = None,
) -> None:
    """Tell a leader their click landed on a card that was already resolved.

    This is the visible half of the open-card guard in `decide_leader_action`.
    The card in front of them is stale — most often because the engine withdrew
    it and nothing refreshed the message, or another leader decided it first —
    so re-render it from the current row rather than leaving a card on screen
    that still says Open.
    """
    if action is None and action_id is not None:
        action = await asyncio.to_thread(db.get_leader_action_by_id, action_id)
    if not action:
        await _send_ephemeral(interaction, "Action not found.")
        return
    decider = str(action.get("decided_by_discord_user_id") or "")
    if decider in _SYSTEM_DECIDERS:
        who = _SYSTEM_DECIDERS[decider]
    elif decider.isdigit():
        who = f"decided by <@{decider}>"
    else:
        who = "already decided"
    await _send_ephemeral(
        interaction,
        f"R{action.get('action_id')} is no longer open — {who}. "
        "Nothing was changed. The card above has been refreshed.",
    )
    await _refresh_card_message(interaction, action)


async def _refresh_card_message(interaction: discord.Interaction, action: dict) -> None:
    message = getattr(interaction, "message", None)
    if message is None:
        source_id = action.get("source_message_id")
        if source_id and interaction.channel:
            try:
                message = await interaction.channel.fetch_message(int(source_id))
            except Exception:
                log.exception(
                    "leader_action_ui.fetch_card failed: action_id=%s source_message_id=%s",
                    action.get("action_id"),
                    source_id,
                )
                message = None
    if message is None:
        return
    try:
        await message.edit(
            embed=build_leader_action_embed(action),
            view=leader_action_view_for(action),
        )
    except Exception:
        log.warning(
            "leader action card refresh failed action_id=%s",
            action.get("action_id"),
            exc_info=True,
        )


async def _dispatch_member_outreach_decision(action: dict, status: str) -> None:
    """A member_outreach card delivers (or skips) the member DM only when the
    outreach flow is told of the leader's decision. The emoji-reaction path does
    this in runtime.prompt_feedback; the button/modal UI must do the same or an
    approved card is marked done while the member never gets a DM. Lazy app
    import avoids the app -> leader_action_ui load cycle."""
    if (action or {}).get("action_type") != "member_outreach":
        return
    import runtime.app as app

    try:
        await app._member_outreach_decision(action, status)
    except Exception:
        log.exception(
            "member outreach decision handling failed action_id=%s", (action or {}).get("action_id")
        )


async def _apply_card_update(interaction: discord.Interaction, action: dict) -> None:
    embed = build_leader_action_embed(action)
    view = leader_action_view_for(action)
    if not interaction.response.is_done():
        try:
            await interaction.response.edit_message(embed=embed, view=view)
            return
        except Exception:
            log.warning(
                "leader action interaction edit failed action_id=%s",
                action.get("action_id"),
                exc_info=True,
            )
    await _refresh_card_message(interaction, action)
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except Exception:
            log.warning(
                "leader action silent ack failed action_id=%s",
                action.get("action_id"),
                exc_info=True,
            )


async def _edit_copy_messages(
    interaction: discord.Interaction, action: dict, copies: list[str]
) -> None:
    ids = [str(item) for item in (action.get("copy_message_ids") or []) if item]
    if not ids and action.get("copy_message_id"):
        ids = [str(action["copy_message_id"])]
    sent_ids = list(ids)
    for index, copy in enumerate(copies):
        message_id = ids[index] if index < len(ids) else None
        if message_id and interaction.channel:
            try:
                message = await interaction.channel.fetch_message(int(message_id))
                await message.edit(content=copy)
                continue
            except Exception:
                log.warning(
                    "leader action copy edit failed action_id=%s copy_message_id=%s",
                    action.get("action_id"),
                    message_id,
                    exc_info=True,
                )
        if interaction.channel:
            sent = await interaction.channel.send(copy)
            sent_ids.append(str(sent.id))
    if sent_ids:
        await asyncio.to_thread(
            db.update_leader_action_copy_messages,
            action["action_id"],
            copy_message_ids=sent_ids[: len(copies)],
        )


class CopyEditModal(discord.ui.Modal):
    def __init__(self, action: dict, copies: list[str]):
        super().__init__(title=f"Edit R{action.get('action_id')} copy")
        self.action_id = int(action["action_id"])
        self.inputs: list[discord.ui.TextInput] = []
        for index, copy in enumerate((copies or [""])[:5], 1):
            item = discord.ui.TextInput(
                label=f"Clash message {index}",
                style=discord.TextStyle.paragraph,
                default=copy[:CLASH_COPY_MAX_LENGTH],
                max_length=CLASH_COPY_MAX_LENGTH,
                required=True,
                custom_id=f"copy_{index}",
            )
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        copies = [
            str(item.value or "").strip() for item in self.inputs if str(item.value or "").strip()
        ]
        if not copies:
            await _send_ephemeral(interaction, "No copy text submitted.")
            return
        action = await asyncio.to_thread(
            db.update_leader_action_copy_text,
            self.action_id,
            copy_text="\n".join(copies),
            discord_user_id=interaction.user.id,
        )
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        await _edit_copy_messages(interaction, action, copies)
        action = await asyncio.to_thread(db.get_leader_action_by_id, self.action_id) or action
        queue_leader_action_feedback_refresh(action.get("action_type"))
        await _apply_card_update(interaction, action)


class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, action: dict):
        super().__init__(title=f"Decline R{action.get('action_id')}")
        self.action_id = int(action["action_id"])
        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            max_length=240,
            required=False,
            placeholder="Optional. 'revisit in a week/month' delays re-nomination; else reconsidered on new evidence.",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        note = str(self.reason.value or "").strip()
        action = await asyncio.to_thread(
            db.decide_leader_action,
            self.action_id,
            status=db.ACTION_REJECTED,
            discord_user_id=interaction.user.id,
            emoji="❌",
            decision_note=note or None,
            decided_via=db.DECIDED_VIA_BUTTON,
        )
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_action_feedback_refresh(action.get("action_type"))
        queue_leader_note_interpretation(self.action_id, bot=interaction.client)
        await _apply_card_update(interaction, action)
        await _dispatch_member_outreach_decision(action, db.ACTION_REJECTED)


class NoteModal(discord.ui.Modal):
    def __init__(self, action: dict):
        super().__init__(title=f"Note for R{action.get('action_id')}")
        self.action_id = int(action["action_id"])
        self.note = discord.ui.TextInput(
            label="Leader note",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=True,
            placeholder="Example: boat defenses already full.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        action = await asyncio.to_thread(db.get_leader_action_by_id, self.action_id)
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        source_id = action.get("source_message_id") or getattr(
            getattr(interaction, "message", None), "id", None
        )
        if source_id is None:
            await _send_ephemeral(interaction, "Action message not available.")
            return
        updated = await asyncio.to_thread(
            db.record_leader_action_note_by_message,
            source_id,
            note=str(self.note.value or ""),
            discord_user_id=interaction.user.id,
        )
        if not updated:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_action_feedback_refresh(updated.get("action_type"))
        queue_leader_note_interpretation(self.action_id, bot=interaction.client)
        await _apply_card_update(interaction, updated)


class DepartureClassifyModal(discord.ui.Modal):
    """Leader confirms LEAVE vs KICK for a departed member, with an optional
    free-text comment on what happened (captured inline, not a separate note)."""

    def __init__(self, action: dict, classification: str):
        verb = "kick" if classification == "kick" else "leave"
        super().__init__(
            title=f"Confirm {verb}: {action.get('target_player_name') or 'member'}"[:45]
        )
        self.action_id = int(action["action_id"])
        self.classification = classification
        self.comment = discord.ui.TextInput(
            label="What happened? (optional)",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=False,
            placeholder="Optional. e.g. 'chat toxicity, warned twice' or 'left for another clan'.",
        )
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        comment = str(self.comment.value or "").strip()
        updated = await asyncio.to_thread(
            db.classify_departure,
            self.action_id,
            classification=self.classification,
            discord_user_id=interaction.user.id,
            comment=comment or None,
        )
        if not updated:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_action_feedback_refresh(updated.get("action_type"))
        await _apply_card_update(interaction, updated)


class FixReadingModal(discord.ui.Modal):
    """Correct a misread note: the leader re-types (or refines) the note, the
    current effect is reverted, and interpretation re-runs on the new text."""

    def __init__(self, action: dict):
        super().__init__(title=f"Fix reading R{action.get('action_id')}")
        self.action_id = int(action["action_id"])
        self.note = discord.ui.TextInput(
            label="Corrected note",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=True,
            default=action.get("decision_note") or None,
            placeholder="Reword so it reads correctly, e.g. 'on leave until August'.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        from runtime.leader_note_interpreter import revert_and_reset_note

        updated = await asyncio.to_thread(
            revert_and_reset_note,
            self.action_id,
            str(self.note.value or ""),
            interaction.user.id,
        )
        if not updated:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_note_interpretation(self.action_id, bot=interaction.client)
        await _apply_card_update(interaction, updated)


class LeaderActionButton(discord.ui.Button):
    def __init__(
        self,
        action: dict,
        *,
        kind: str,
        label: str,
        style: discord.ButtonStyle,
        row: int,
        emoji: str | None = None,
        disabled: bool = False,
    ):
        self.action_id = int(action["action_id"])
        self.kind = kind
        super().__init__(
            label=label,
            style=style,
            row=row,
            emoji=emoji,
            disabled=disabled,
            custom_id=f"leader_action:{self.action_id}:{kind}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        action = await asyncio.to_thread(db.get_leader_action_by_id, self.action_id)
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        if not action_is_open(action):
            await _report_already_decided(interaction, action)
            return
        if self.kind == "done":
            updated = await asyncio.to_thread(
                db.decide_leader_action,
                self.action_id,
                status=db.ACTION_DONE,
                discord_user_id=interaction.user.id,
                emoji="✅",
                decided_via=db.DECIDED_VIA_BUTTON,
            )
            if updated is None:
                # Lost the race, or the engine withdrew the card between the read
                # above and the write. Never fall back to the pre-write snapshot:
                # that used to fire the member DM for a decision the DB rejected.
                await _report_already_decided(interaction, None, action_id=self.action_id)
                return
            queue_leader_action_feedback_refresh(action.get("action_type"))
            await _apply_card_update(interaction, updated)
            await _dispatch_member_outreach_decision(updated, db.ACTION_DONE)
            return
        if self.kind == "decline":
            await interaction.response.send_modal(DecisionReasonModal(action))
            return
        if self.kind == "edit_copy":
            copies = action_copy_messages(action)
            if not copies:
                copies = _split_copy_messages(action.get("prompt_text"))
            await interaction.response.send_modal(CopyEditModal(action, copies))
            return
        if self.kind == "note":
            await interaction.response.send_modal(NoteModal(action))
            return
        if self.kind == "classify_ignore":
            updated = await asyncio.to_thread(
                db.classify_departure,
                self.action_id,
                classification="ignore",
                discord_user_id=interaction.user.id,
            )
            if updated is None:
                await _report_already_decided(interaction, None, action_id=self.action_id)
                return
            queue_leader_action_feedback_refresh(action.get("action_type"))
            await _apply_card_update(interaction, updated)
            return
        if self.kind in _CLASSIFY_KIND_VALUE:
            await interaction.response.send_modal(
                DepartureClassifyModal(action, _CLASSIFY_KIND_VALUE[self.kind])
            )
            return
        if self.kind == "note_undo":
            from runtime.leader_note_interpreter import undo_note_interpretation

            updated = await asyncio.to_thread(undo_note_interpretation, self.action_id)
            await _apply_card_update(interaction, updated or action)
            return
        if self.kind == "note_fix":
            await interaction.response.send_modal(FixReadingModal(action))
            return


class LeaderActionView(discord.ui.View):
    def __init__(self, action: dict):
        super().__init__(timeout=None)
        spec = leader_action_spec(action.get("action_type"))
        proposed = action_is_open(action)
        if proposed:
            self._add_primary_buttons(action, spec)
        # Interpretation controls survive past the decision: a note (and its
        # applied effect) can land on an already-decided card, so these render on
        # open AND closed cards whenever an interpretation exists.
        self._add_interpretation_buttons(action)

    def _add_primary_buttons(self, action: dict, spec) -> None:
        copies = action_copy_messages(action)
        if spec.classify_choices:
            # Classification card (e.g. departure_verification): render the
            # choice buttons instead of done/decline. Every choice resolves it.
            for kind, label, emoji in spec.classify_choices:
                self.add_item(
                    LeaderActionButton(
                        action,
                        kind=kind,
                        label=label,
                        emoji=emoji,
                        style=discord.ButtonStyle.primary,
                        row=0,
                    )
                )
            return

        self.add_item(
            LeaderActionButton(
                action,
                kind="done",
                label=spec.done_label,
                emoji="✅",
                style=discord.ButtonStyle.success,
                row=0,
            )
        )
        self.add_item(
            LeaderActionButton(
                action,
                kind="decline",
                label=spec.decline_label,
                emoji="❌",
                style=discord.ButtonStyle.danger,
                row=0,
            )
        )
        if spec.allow_copy_edit and copies:
            self.add_item(
                LeaderActionButton(
                    action,
                    kind="edit_copy",
                    label="Edit Copy",
                    emoji="✏️",
                    style=discord.ButtonStyle.primary,
                    row=1,
                )
            )
        self.add_item(
            LeaderActionButton(
                action,
                kind="note",
                label="Add Note",
                emoji="📝",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
        )

    def _add_interpretation_buttons(self, action: dict) -> None:
        if (action.get("note_interpret_status") or "") != "interpreted":
            return
        if _has_undoable_interpretation(action):
            self.add_item(
                LeaderActionButton(
                    action,
                    kind="note_undo",
                    label="Undo",
                    emoji="↩️",
                    style=discord.ButtonStyle.danger,
                    row=3,
                )
            )
        self.add_item(
            LeaderActionButton(
                action,
                kind="note_fix",
                label="Fix reading",
                emoji="🔧",
                style=discord.ButtonStyle.secondary,
                row=3,
            )
        )


def leader_action_view_for(action: dict) -> LeaderActionView | None:
    # A view is needed while the card is open, or once it carries an
    # interpretation (so the Undo / Fix-reading controls are live on a decided
    # card). Otherwise a resolved card is inert and needs no view.
    if action_is_open(action) or (action.get("note_interpret_status") or "") == "interpreted":
        return LeaderActionView(action)
    return None


async def post_leader_action_card(
    channel,
    action: dict,
    *,
    copy_messages: list[str] | None = None,
) -> list:
    copies = action_copy_messages(action, copy_messages)
    card = await channel.send(
        embed=build_leader_action_embed(action, copy_messages=copies),
        view=leader_action_view_for(action),
    )
    await asyncio.to_thread(
        db.update_leader_action_message,
        action["action_id"],
        source_message_id=getattr(card, "id", None),
    )
    sent = [card]
    copy_ids = []
    for copy in copies:
        message = await channel.send(copy)
        sent.append(message)
        if getattr(message, "id", None) is not None:
            copy_ids.append(message.id)
    if copy_ids:
        await asyncio.to_thread(
            db.update_leader_action_copy_messages,
            action["action_id"],
            copy_message_ids=copy_ids,
        )
    return sent


async def restore_leader_action_views(
    bot: discord.Client,
    *,
    limit: int = 50,
    terminal_cleanup_limit: int = 3,
) -> int:
    open_actions = await asyncio.to_thread(
        db.list_leader_actions, status=db.ACTION_PROPOSED, limit=limit
    )
    recent_actions = await asyncio.to_thread(
        db.list_leader_actions,
        limit=max(0, min(int(terminal_cleanup_limit or 0), int(limit or 0))),
    )
    # Decided-but-interpreted cards keep live Undo / Fix-reading buttons, so their
    # persistent views must be re-registered too (they're not in the open set).
    interpreted_actions = await asyncio.to_thread(db.list_interpreted_leader_actions, limit=limit)
    actions_by_id = {}
    for action in recent_actions + interpreted_actions + open_actions:
        action_id = action.get("action_id")
        if action_id is not None:
            actions_by_id[int(action_id)] = action

    restored = 0
    refreshed = 0
    for action in actions_by_id.values():
        message_id = action.get("source_message_id")
        # A card still carrying the POSTING_SENTINEL never actually posted (its
        # post was interrupted/403'd), so there is no Discord message to bind a
        # view to — skip it rather than throwing int('posting') on every boot.
        if not message_id or message_id == db.POSTING_SENTINEL:
            continue
        try:
            view = leader_action_view_for(action)
            if view is not None:
                bot.add_view(view, message_id=int(message_id))
                restored += 1
            if await refresh_leader_action_card(bot, action):
                refreshed += 1
        except Exception:
            log.warning(
                "leader action view restore failed action_id=%s",
                action.get("action_id"),
                exc_info=True,
            )
    if restored or refreshed:
        log.info(
            "Restored %s leader action view(s); refreshed %s card(s)",
            restored,
            refreshed,
        )
    return restored


async def refresh_leader_action_card(bot: discord.Client, action: dict) -> bool:
    message_id = action.get("source_message_id")
    channel_id = action.get("target_channel_id")
    if not message_id or not channel_id:
        return False
    try:
        channel_id_int = int(channel_id)
        message_id_int = int(message_id)
    except TypeError, ValueError:
        return False

    channel = None
    get_channel = getattr(bot, "get_channel", None)
    if get_channel is not None:
        channel = get_channel(channel_id_int)
    if channel is None:
        fetch_channel = getattr(bot, "fetch_channel", None)
        if fetch_channel is not None:
            try:
                channel = await fetch_channel(channel_id_int)
            except Exception:
                log.warning(
                    "leader action card channel fetch failed action_id=%s channel_id=%s",
                    action.get("action_id"),
                    channel_id,
                    exc_info=True,
                )
                return False
    if channel is None or not hasattr(channel, "fetch_message"):
        return False

    try:
        message = await channel.fetch_message(message_id_int)
        await message.edit(
            embed=build_leader_action_embed(action), view=leader_action_view_for(action)
        )
        return True
    except Exception:
        log.warning(
            "leader action card startup refresh failed action_id=%s message_id=%s",
            action.get("action_id"),
            message_id,
            exc_info=True,
        )
        return False


def default_test_copy(action_type: str) -> list[str]:
    spec = leader_action_spec(action_type)
    if action_type == "discord_invite_relay":
        return [
            "TEST ONLY: Discord invite relay interaction check.",
            "TEST ONLY: Do not paste this into Clash Royale.",
        ]
    return [f"TEST ONLY: {spec.label} interaction check. Do not paste this into Clash Royale."]


def default_test_prompt(action_type: str, target_name: str | None = None) -> str:
    name = target_name or "Test Player"
    if action_type == "promotion_recommendation":
        return f"Promote {name} to Elder."
    if action_type == "demotion_recommendation":
        return f"Review {name} for demotion."
    if action_type == "kick_recommendation":
        return f"Review {name} for removal from the clan."
    if action_type == "welcome_relay":
        return f"Welcome {name} with a short profile-specific clan-chat note."
    if action_type == "celebration_relay":
        return f"Share a milestone shoutout for {name}."
    if action_type == "discord_invite_relay":
        return "Share the weekly Discord invite copy in clan chat."
    return "Share this in-game relay with the clan."


async def post_test_leader_action_card(
    channel, *, action_type: str, target_name: str | None = None
) -> dict:
    clean_type = (action_type or "").strip()
    if clean_type not in ACTION_SPECS:
        raise ValueError(f"unsupported leader action type: {action_type}")
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    copies = default_test_copy(clean_type)
    copy_text = "\n".join(copies)
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type=clean_type,
        objective="test_render",
        prompt_text=default_test_prompt(clean_type, target_name),
        rationale="Test card generated by an operator to verify the leader-action interaction flow.",
        target_channel_key="actions",
        target_channel_id=getattr(channel, "id", None),
        target_player_tag="#TEST",
        target_player_name=target_name or "Test Player",
        copy_original_text=copy_text,
        copy_current_text=copy_text,
        action_key=f"test:{clean_type}:{now}",
        is_test=True,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline={
            "member": {
                "name": target_name or "Test Player",
                "role": "member",
                "status": "test",
            },
            "war_day": {"phase": "test", "untouched_count": 0},
        },
    )
    await post_leader_action_card(channel, action, copy_messages=copies)
    return await asyncio.to_thread(db.get_leader_action_by_id, action["action_id"]) or action


__all__ = [
    "ACTION_SPECS",
    "CLASH_COPY_MAX_LENGTH",
    "LEADER_ACTION_UI_VERSION",
    "LeaderActionTypeSpec",
    "LeaderActionView",
    "action_is_open",
    "action_copy_messages",
    "build_leader_action_embed",
    "default_test_copy",
    "default_test_prompt",
    "leader_action_spec",
    "leader_action_view_for",
    "leader_action_type_choices",
    "post_leader_action_card",
    "post_test_leader_action_card",
    "refresh_leader_action_card",
    "restore_leader_action_views",
]
