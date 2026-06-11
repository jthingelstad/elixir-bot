"""Interactive Discord UI for arena-relay leader actions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

import db
from runtime.leader_action_feedback import queue_leader_action_feedback_refresh

log = logging.getLogger("elixir.leader_action_ui")

LEADER_ACTION_UI_VERSION = "leader-action-ui-v1"
CLASH_COPY_MAX_LENGTH = 180
DEFER_OPTIONS = (
    ("1 day", 1, "Hold this recommendation for one day."),
    ("3 days", 3, "Give the situation a few days to change."),
    ("7 days", 7, "Do not revisit this for a week."),
)


@dataclass(frozen=True)
class LeaderActionTypeSpec:
    action_type: str
    label: str
    emoji: str
    color: int
    done_label: str
    decline_label: str = "Decline"
    allow_copy_edit: bool = False
    allow_defer: bool = False
    show_profile: bool = False
    show_war: bool = False
    copy_field_label: str = "Clash Copy"


ACTION_SPECS: dict[str, LeaderActionTypeSpec] = {
    "welcome_relay": LeaderActionTypeSpec(
        "welcome_relay",
        "Welcome Relay",
        "👋",
        0x36A3FF,
        "Posted",
        "Skip",
        allow_copy_edit=True,
        show_profile=True,
    ),
    "discord_invite_relay": LeaderActionTypeSpec(
        "discord_invite_relay",
        "Discord Invite Relay",
        "💬",
        0x5865F2,
        "Posted All",
        "Skip",
        allow_copy_edit=True,
        allow_defer=True,
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
        allow_defer=True,
        show_war=True,
    ),
    "war_nudge_recommendation": LeaderActionTypeSpec(
        "war_nudge_recommendation",
        "War Nudge",
        "⚔️",
        0xE67E22,
        "Nudged",
        allow_defer=True,
        show_profile=True,
        show_war=True,
    ),
    "promotion_recommendation": LeaderActionTypeSpec(
        "promotion_recommendation",
        "Promotion Recommendation",
        "⬆️",
        0x2ECC71,
        "Promoted",
        allow_copy_edit=True,
        allow_defer=True,
        show_profile=True,
        show_war=True,
    ),
    "demotion_recommendation": LeaderActionTypeSpec(
        "demotion_recommendation",
        "Demotion Recommendation",
        "⬇️",
        0xF39C12,
        "Demoted",
        allow_copy_edit=True,
        allow_defer=True,
        show_profile=True,
        show_war=True,
    ),
    "kick_recommendation": LeaderActionTypeSpec(
        "kick_recommendation",
        "Kick Recommendation",
        "🚪",
        0xE74C3C,
        "Kicked",
        allow_copy_edit=True,
        allow_defer=True,
        show_profile=True,
        show_war=True,
    ),
    "celebration_relay": LeaderActionTypeSpec(
        "celebration_relay",
        "Celebration Relay",
        "🎉",
        0x9B59B6,
        "Posted",
        "Skip",
        allow_copy_edit=True,
        show_profile=True,
    ),
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
    if status == db.ACTION_DEFERRED:
        days = action.get("defer_days")
        suffix = f" {days}d" if days else ""
        return f"Deferred{suffix}"
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


def _baseline_lines(value, *, limit: int = 8) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = []
    for key, raw in value.items():
        if raw in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ")
        lines.append(f"**{label}:** {raw}")
        if len(lines) >= limit:
            break
    return lines


def build_leader_action_embed(action: dict, *, copy_messages: list[str] | None = None) -> discord.Embed:
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
            value = "\n".join(f"`{idx}.` {_clip(item, CLASH_COPY_MAX_LENGTH)}" for idx, item in enumerate(copies, 1))
        embed.add_field(name=spec.copy_field_label, value=value[:1024], inline=False)
    note = action.get("decision_note")
    if note:
        embed.add_field(name="Leader Note", value=_clip(note, 700), inline=False)
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


async def _refresh_card_message(interaction: discord.Interaction, action: dict) -> None:
    message = getattr(interaction, "message", None)
    if message is None:
        source_id = action.get("source_message_id")
        if source_id and interaction.channel:
            try:
                message = await interaction.channel.fetch_message(int(source_id))
            except Exception:
                message = None
    if message is None:
        return
    try:
        await message.edit(
            embed=build_leader_action_embed(action),
            view=leader_action_view_for(action),
        )
    except Exception:
        log.warning("leader action card refresh failed action_id=%s", action.get("action_id"), exc_info=True)


async def _edit_copy_messages(interaction: discord.Interaction, action: dict, copies: list[str]) -> None:
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
                style=discord.TextStyle.short,
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
        copies = [str(item.value or "").strip() for item in self.inputs if str(item.value or "").strip()]
        if not copies:
            await _send_ephemeral(interaction, "No copy text submitted.")
            return
        await interaction.response.defer(ephemeral=True)
        action = await asyncio.to_thread(
            db.update_leader_action_copy_text,
            self.action_id,
            copy_text="\n".join(copies),
            discord_user_id=interaction.user.id,
        )
        if not action:
            await interaction.followup.send("Action not found.", ephemeral=True)
            return
        await _edit_copy_messages(interaction, action, copies)
        action = await asyncio.to_thread(db.get_leader_action_by_id, self.action_id) or action
        await _refresh_card_message(interaction, action)
        queue_leader_action_feedback_refresh(action.get("action_type"))
        await interaction.followup.send(f"Updated copy for R{self.action_id}.", ephemeral=True)


class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, action: dict):
        super().__init__(title=f"Decline R{action.get('action_id')}")
        self.action_id = int(action["action_id"])
        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.short,
            max_length=240,
            required=False,
            placeholder="Optional. Example: already handled, not the right call, wait for war reset.",
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
        )
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_action_feedback_refresh(action.get("action_type"))
        await _refresh_card_message(interaction, action)
        await _send_ephemeral(interaction, f"R{self.action_id} declined.")


class NoteModal(discord.ui.Modal):
    def __init__(self, action: dict):
        super().__init__(title=f"Note for R{action.get('action_id')}")
        self.action_id = int(action["action_id"])
        self.note = discord.ui.TextInput(
            label="Leader note",
            style=discord.TextStyle.short,
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
        source_id = action.get("source_message_id") or getattr(getattr(interaction, "message", None), "id", None)
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
        await _refresh_card_message(interaction, updated)
        await _send_ephemeral(interaction, f"Noted on R{self.action_id}.")


class LeaderActionButton(discord.ui.Button):
    def __init__(self, action: dict, *, kind: str, label: str, style: discord.ButtonStyle, row: int, emoji: str | None = None, disabled: bool = False):
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
        if self.kind == "done":
            updated = await asyncio.to_thread(
                db.decide_leader_action,
                self.action_id,
                status=db.ACTION_DONE,
                discord_user_id=interaction.user.id,
                emoji="✅",
            )
            queue_leader_action_feedback_refresh(action.get("action_type"))
            await _refresh_card_message(interaction, updated or action)
            await _send_ephemeral(interaction, f"R{self.action_id} marked done.")
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
        if self.kind == "preview_copy":
            copies = action_copy_messages(action)
            if not copies:
                await _send_ephemeral(interaction, "No Clash copy is attached to this action.")
                return
            text = "\n\n".join(f"```text\n{copy}\n```" for copy in copies)
            await _send_ephemeral(interaction, text[:1900])
            return
        if self.kind == "note":
            await interaction.response.send_modal(NoteModal(action))
            return
        if self.kind == "profile":
            baseline = action.get("baseline") or {}
            lines = _baseline_lines(baseline.get("member"), limit=10)
            content = "\n".join(lines) if lines else "No stored player snapshot for this action."
            await _send_ephemeral(interaction, content[:1900])
            return
        if self.kind == "war":
            baseline = action.get("baseline") or {}
            lines = _baseline_lines(baseline.get("war_day"), limit=10) + _baseline_lines(baseline.get("war_status"), limit=6)
            content = "\n".join(lines) if lines else "No stored war snapshot for this action."
            await _send_ephemeral(interaction, content[:1900])
            return


class DeferSelect(discord.ui.Select):
    def __init__(self, action: dict):
        self.action_id = int(action["action_id"])
        options = [
            discord.SelectOption(label=label, value=str(days), description=description, emoji="⏳")
            for label, days, description in DEFER_OPTIONS
        ]
        super().__init__(
            placeholder="Defer recommendation...",
            options=options,
            min_values=1,
            max_values=1,
            row=2,
            custom_id=f"leader_action:{self.action_id}:defer",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ensure_leader(interaction):
            return
        try:
            days = int((self.values or ["1"])[0])
        except (TypeError, ValueError):
            days = 1
        action = await asyncio.to_thread(
            db.decide_leader_action,
            self.action_id,
            status=db.ACTION_DEFERRED,
            discord_user_id=interaction.user.id,
            emoji="⏳",
            defer_days=days,
            decision_note=f"Deferred for {days} day{'s' if days != 1 else ''}.",
        )
        if not action:
            await _send_ephemeral(interaction, "Action not found.")
            return
        queue_leader_action_feedback_refresh(action.get("action_type"))
        await _refresh_card_message(interaction, action)
        await _send_ephemeral(interaction, f"R{self.action_id} deferred for {days} day{'s' if days != 1 else ''}.")


class LeaderActionView(discord.ui.View):
    def __init__(self, action: dict):
        super().__init__(timeout=None)
        spec = leader_action_spec(action.get("action_type"))
        proposed = action_is_open(action)
        if not proposed:
            return
        copies = action_copy_messages(action)

        self.add_item(LeaderActionButton(
            action,
            kind="done",
            label=spec.done_label,
            emoji="✅",
            style=discord.ButtonStyle.success,
            row=0,
            disabled=not proposed,
        ))
        self.add_item(LeaderActionButton(
            action,
            kind="decline",
            label=spec.decline_label,
            emoji="❌",
            style=discord.ButtonStyle.danger,
            row=0,
            disabled=not proposed,
        ))
        if spec.allow_copy_edit and copies:
            self.add_item(LeaderActionButton(
                action,
                kind="edit_copy",
                label="Edit Copy",
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                row=1,
                disabled=not proposed,
            ))
            self.add_item(LeaderActionButton(
                action,
                kind="preview_copy",
                label="Preview Copy",
                emoji="📋",
                style=discord.ButtonStyle.secondary,
                row=1,
            ))
        if spec.allow_defer and proposed:
            self.add_item(DeferSelect(action))
        detail_row = 3 if spec.allow_defer else 2
        if spec.show_profile and action.get("target_player_tag"):
            self.add_item(LeaderActionButton(
                action,
                kind="profile",
                label="Profile",
                emoji="👤",
                style=discord.ButtonStyle.secondary,
                row=detail_row,
            ))
        if spec.show_war:
            self.add_item(LeaderActionButton(
                action,
                kind="war",
                label="War Detail",
                emoji="⚔️",
                style=discord.ButtonStyle.secondary,
                row=detail_row,
            ))
        self.add_item(LeaderActionButton(
            action,
            kind="note",
            label="Add Note",
            emoji="📝",
            style=discord.ButtonStyle.secondary,
            row=4,
        ))


def leader_action_view_for(action: dict) -> LeaderActionView | None:
    if not action_is_open(action):
        return None
    return LeaderActionView(action)


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
            copy_ids.append(getattr(message, "id"))
    if copy_ids:
        await asyncio.to_thread(
            db.update_leader_action_copy_messages,
            action["action_id"],
            copy_message_ids=copy_ids,
        )
    return sent


async def restore_leader_action_views(bot: discord.Client, *, limit: int = 50) -> int:
    actions = await asyncio.to_thread(db.list_leader_actions, status=db.ACTION_PROPOSED, limit=limit)
    restored = 0
    for action in actions:
        message_id = action.get("source_message_id")
        if not message_id:
            continue
        try:
            view = leader_action_view_for(action)
            if view is None:
                continue
            bot.add_view(view, message_id=int(message_id))
            restored += 1
        except Exception:
            log.warning("leader action view restore failed action_id=%s", action.get("action_id"), exc_info=True)
    if restored:
        log.info("Restored %s leader action view(s)", restored)
    return restored


def default_test_copy(action_type: str) -> list[str]:
    spec = leader_action_spec(action_type)
    if action_type == "discord_invite_relay":
        return [
            "TEST ONLY: Discord invite relay interaction check.",
            "TEST ONLY: Do not paste this into Clash Royale.",
        ]
    if action_type in {"war_nudge_recommendation"}:
        return []
    return [f"TEST ONLY: {spec.label} interaction check. Do not paste this into Clash Royale."]


def default_test_prompt(action_type: str, target_name: str | None = None) -> str:
    name = target_name or "Test Player"
    if action_type == "promotion_recommendation":
        return f"Promote {name} to Elder."
    if action_type == "demotion_recommendation":
        return f"Review {name} for demotion."
    if action_type == "kick_recommendation":
        return f"Review {name} for removal from the clan."
    if action_type == "war_nudge_recommendation":
        return f"Nudge {name} to use war decks."
    if action_type == "welcome_relay":
        return f"Welcome {name} with a short profile-specific clan-chat note."
    if action_type == "celebration_relay":
        return f"Share a milestone shoutout for {name}."
    if action_type == "discord_invite_relay":
        return "Share the weekly Discord invite copy in clan chat."
    return "Share this in-game relay with the clan."


async def post_test_leader_action_card(channel, *, action_type: str, target_name: str | None = None) -> dict:
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
        rationale="Test card generated by an operator to verify the arena-relay interaction flow.",
        target_channel_key="arena-relay",
        target_channel_id=getattr(channel, "id", None),
        target_player_tag="#TEST",
        target_player_name=target_name or "Test Player",
        copy_original_text=copy_text,
        copy_current_text=copy_text,
        action_key=f"test:{clean_type}:{now}",
        is_test=True,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline={
            "member": {"name": target_name or "Test Player", "role": "member", "status": "test"},
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
    "restore_leader_action_views",
]
