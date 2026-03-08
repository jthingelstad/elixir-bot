"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import atexit
import json
import os
import re
import signal
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api
import db
import site_content
import elixir_agent
import heartbeat
import prompts
from runtime.admin import dispatch_admin_command, parse_admin_command, render_admin_help
from runtime import status as runtime_status

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
LEADER_ROLE_ID = _dc.get("leader_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
GUILD_ID = int(_dc.get("guild_id", 0) or 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))
CLANOPS_PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("CLANOPS_PROACTIVE_COOLDOWN_SECONDS", "900"))
CHANNEL_CONVERSATION_LIMIT = 20

# Active hours for the heartbeat (Chicago time). Outside this window, heartbeat is skipped.
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "7"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "22"))
HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "47"))
HEARTBEAT_JITTER_SECONDS = int(os.getenv("HEARTBEAT_JITTER_SECONDS", "300"))
PROMOTION_CONTENT_DAY = os.getenv("PROMOTION_CONTENT_DAY", "fri")
PROMOTION_CONTENT_HOUR = int(os.getenv("PROMOTION_CONTENT_HOUR", "9"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)
APP_GUILD = discord.Object(id=GUILD_ID) if GUILD_ID else None
SLASH_COMMANDS_SYNCED = False


def _format_hour_label(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}{suffix}"


def _member_role_grant_status() -> dict:
    status = {
        "configured": bool(MEMBER_ROLE_ID),
        "guild_found": False,
        "member_role_found": False,
        "bot_role_found": False,
        "manage_roles": None,
        "member_role_position": None,
        "bot_top_role_position": None,
        "ok": False,
        "reason": "member role not configured",
    }
    if not MEMBER_ROLE_ID:
        return status
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild is None:
        status["reason"] = "guild not cached"
        return status
    status["guild_found"] = True
    member_role = guild.get_role(MEMBER_ROLE_ID)
    bot_role = guild.get_role(BOT_ROLE_ID) if BOT_ROLE_ID else None
    me = guild.me
    if member_role is None:
        status["reason"] = "member role not found"
        return status
    status["member_role_found"] = True
    status["member_role_position"] = member_role.position
    if bot_role is not None:
        status["bot_role_found"] = True
    if me is None:
        status["reason"] = "bot member not cached"
        return status
    status["manage_roles"] = me.guild_permissions.manage_roles
    status["bot_top_role_position"] = getattr(me.top_role, "position", None)
    if not status["manage_roles"]:
        status["reason"] = "Manage Roles permission missing"
        return status
    if getattr(me.top_role, "position", -1) <= member_role.position:
        status["reason"] = "bot role must be above member role"
        return status
    status["ok"] = True
    status["reason"] = "ok"
    return status


def _has_leader_role(member) -> bool:
    if not LEADER_ROLE_ID:
        return True
    return any(getattr(role, "id", None) == LEADER_ROLE_ID for role in getattr(member, "roles", []))


def _is_clanops_channel(channel) -> bool:
    channel_config = _get_channel_behavior(getattr(channel, "id", 0))
    return bool(channel_config and channel_config.get("role") == "clanops")


def _chunk_discord_text(text: str, limit: int = 2000) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    return [text[i:i + (limit - 10)] for i in range(0, len(text), limit - 10)]


async def _send_interaction_text(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    chunks = _chunk_discord_text(content)
    if not chunks:
        chunks = ["_No content._"]
    if not interaction.response.is_done():
        await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
        start = 1
    else:
        await interaction.followup.send(chunks[0], ephemeral=ephemeral)
        start = 1
    for chunk in chunks[start:]:
        await interaction.followup.send(chunk, ephemeral=ephemeral)


async def _save_interaction_exchange(
    interaction: discord.Interaction,
    *,
    command_text: str,
    response_text: str,
    workflow: str = "clanops",
    event_type: str | None = None,
):
    if not interaction.channel or not interaction.user:
        return
    scope = _channel_conversation_scope(interaction.channel, interaction.user.id)
    await asyncio.to_thread(
        db.save_message,
        scope,
        "user",
        command_text,
        channel_id=interaction.channel.id,
        channel_name=getattr(interaction.channel, "name", None),
        channel_kind=str(getattr(interaction.channel, "type", "unknown")),
        discord_user_id=interaction.user.id,
        username=getattr(interaction.user, "name", None),
        display_name=getattr(interaction.user, "display_name", None),
        workflow=workflow,
    )
    await asyncio.to_thread(
        db.save_message,
        scope,
        "assistant",
        response_text,
        channel_id=interaction.channel.id,
        channel_name=getattr(interaction.channel, "name", None),
        channel_kind=str(getattr(interaction.channel, "type", "unknown")),
        discord_user_id=interaction.user.id,
        username=getattr(interaction.user, "name", None),
        display_name=getattr(interaction.user, "display_name", None),
        workflow=workflow,
        event_type=event_type,
    )


async def _validate_admin_interaction(
    interaction: discord.Interaction,
    *,
    command_name: str,
    write: bool = False,
) -> bool:
    if not _is_clanops_channel(interaction.channel):
        await _send_interaction_text(
            interaction,
            "Use `/elixir ...` in `#clanops`.",
            ephemeral=True,
        )
        return False
    if write and not _has_leader_role(interaction.user):
        await _send_interaction_text(
            interaction,
            "Leader role required for write commands.",
            ephemeral=True,
        )
        return False
    log.info(
        "slash_command command=%s channel_id=%s author_id=%s write=%s",
        command_name,
        getattr(interaction.channel, "id", None),
        getattr(interaction.user, "id", None),
        write,
    )
    return True


async def _run_admin_interaction(
    interaction: discord.Interaction,
    *,
    command_name: str,
    preview: bool = False,
    short: bool = False,
    args: dict | None = None,
    event_type: str | None = None,
    write: bool = False,
):
    if not await _validate_admin_interaction(interaction, command_name=command_name, write=write):
        return
    content = await dispatch_admin_command(
        command_name,
        preview=preview,
        short=short,
        args=args or {},
    )
    await _save_interaction_exchange(
        interaction,
        command_text=f"/elixir {command_name}",
        response_text=content,
        event_type=event_type or f"slash_{command_name.replace('-', '_')}",
    )
    await _send_interaction_text(interaction, content, ephemeral=True)


async def _member_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    del interaction
    query = (current or "").strip()
    if query:
        members = await asyncio.to_thread(db.resolve_member, query, "active", 25)
    else:
        members = await asyncio.to_thread(db.list_members, "active")
        members = members[:25]
    choices: list[app_commands.Choice[str]] = []
    seen: set[str] = set()
    for member in members:
        tag = member.get("player_tag")
        if not tag or tag in seen:
            continue
        seen.add(tag)
        name = member.get("current_name") or member.get("member_name") or tag
        label = f"{name} ({tag})"
        choices.append(app_commands.Choice(name=label[:100], value=tag))
        if len(choices) >= 25:
            break
    return choices


ELIXIR_COMMANDS = app_commands.Group(name="elixir", description="Elixir clanops commands")
ELIXIR_MEMBER_COMMANDS = app_commands.Group(name="member", description="Member lookup and metadata commands")
ELIXIR_JOB_COMMANDS = app_commands.Group(name="jobs", description="Operational job commands")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _post_to_elixir(channel, entry: dict):
    """Post an entry's content to a configured Discord channel."""
    for post in _entry_posts(entry):
        if len(post) > 2000:
            for chunk in [post[i:i+1990] for i in range(0, len(post), 1990)]:
                await channel.send(chunk)
        else:
            await channel.send(post)


def _entry_posts(entry: dict, field="content"):
    content = entry.get(field, entry.get("summary", ""))
    if not content:
        return []
    if isinstance(content, list):
        return [item.strip() for item in content if isinstance(item, str) and item.strip()]
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else []
    return [str(content)]


def _preview_text(value, limit=500):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = repr(value)
    return text[:limit]


def _normalize_prompt_failure_question(question):
    text = (question or "").strip()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    return " ".join(text.split())


def _log_prompt_failure(*, question, workflow, failure_type, failure_stage, channel, author,
                        discord_message_id=None, detail=None, result_preview=None, raw_json=None):
    openai = runtime_status.snapshot().get("openai") or {}
    clean_question = _normalize_prompt_failure_question(question)
    try:
        failure_id = db.record_prompt_failure(
            clean_question,
            failure_type,
            failure_stage,
            workflow=workflow,
            channel_id=getattr(channel, "id", None),
            channel_name=getattr(channel, "name", None),
            discord_user_id=getattr(author, "id", None),
            discord_message_id=discord_message_id,
            detail=detail,
            result_preview=result_preview,
            openai_last_error=openai.get("last_error"),
            openai_last_model=openai.get("last_model"),
            openai_last_call_at=openai.get("last_call_at"),
            raw_json=raw_json,
        )
        log.warning(
            "prompt_failure id=%s workflow=%s type=%s stage=%s channel_id=%s author_id=%s question=%r detail=%r openai_model=%s openai_error=%r",
            failure_id,
            workflow,
            failure_type,
            failure_stage,
            getattr(channel, "id", None),
            getattr(author, "id", None),
            _preview_text(clean_question, limit=180),
            _preview_text(detail, limit=240),
            openai.get("last_model"),
            _preview_text(openai.get("last_error"), limit=240),
        )
    except Exception as exc:
        log.error("prompt failure logging error: %s", exc)


def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    for name in names:
        globals()[name] = getattr(module, name)
    return names


from runtime import helpers as _helpers_module
from runtime import jobs as _jobs_module

__all__ = [name for name in globals() if not name.startswith("__")]
for _module in (_helpers_module, _jobs_module):
    __export_public(_module)

__all__ = [name for name in globals() if not name.startswith("__")]


@ELIXIR_COMMANDS.command(name="help", description="Show Elixir clanops help.")
async def slash_help(interaction: discord.Interaction):
    if not await _validate_admin_interaction(interaction, command_name="help", write=False):
        return
    content = render_admin_help()
    await _save_interaction_exchange(
        interaction,
        command_text="/elixir help",
        response_text=content,
        event_type="slash_help",
    )
    await _send_interaction_text(interaction, content, ephemeral=True)


@ELIXIR_COMMANDS.command(name="status", description="Show Elixir runtime health and telemetry.")
async def slash_status(interaction: discord.Interaction):
    await _run_admin_interaction(interaction, command_name="status", event_type="status_report")


@ELIXIR_COMMANDS.command(name="schedule", description="Show scheduled jobs and next runs.")
async def slash_schedule(interaction: discord.Interaction):
    await _run_admin_interaction(interaction, command_name="schedule", event_type="schedule_report")


@ELIXIR_COMMANDS.command(name="clan-status", description="Show the operational clan status report.")
@app_commands.describe(short="Return the compact clan status variant.")
async def slash_clan_status(interaction: discord.Interaction, short: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="clan-status",
        short=short,
        event_type="clan_status_short_report" if short else "clan_status_report",
    )


@ELIXIR_COMMANDS.command(name="clan-list", description="List active clan members with exact names and tags.")
async def slash_clan_list(interaction: discord.Interaction):
    await _run_admin_interaction(interaction, command_name="clan-list", event_type="clan_list_report")


@ELIXIR_COMMANDS.command(name="profile", description="Show the stored member profile and metadata.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_profile(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="profile",
        args={"member": member},
        event_type="member_profile_report",
    )


@ELIXIR_MEMBER_COMMANDS.command(name="set-join-date", description="Set a member join date.")
@app_commands.describe(member="Member name or tag.", date="Join date in YYYY-MM-DD format.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_set_join_date(interaction: discord.Interaction, member: str, date: str):
    await _run_admin_interaction(
        interaction,
        command_name="set-join-date",
        args={"member": member, "date": date},
        event_type="clanops_admin_set_join_date",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="clear-join-date", description="Clear a member join date.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_clear_join_date(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="clear-join-date",
        args={"member": member},
        event_type="clanops_admin_clear_join_date",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="set-birthday", description="Set a member birthday.")
@app_commands.describe(member="Member name or tag.", month="Birthday month.", day="Birthday day.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_set_birthday(interaction: discord.Interaction, member: str, month: int, day: int):
    await _run_admin_interaction(
        interaction,
        command_name="set-birthday",
        args={"member": member, "month": str(month), "day": str(day)},
        event_type="clanops_admin_set_birthday",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="clear-birthday", description="Clear a member birthday.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_clear_birthday(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="clear-birthday",
        args={"member": member},
        event_type="clanops_admin_clear_birthday",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="set-profile-url", description="Set a member profile URL.")
@app_commands.describe(member="Member name or tag.", url="Profile URL.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_set_profile_url(interaction: discord.Interaction, member: str, url: str):
    await _run_admin_interaction(
        interaction,
        command_name="set-profile-url",
        args={"member": member, "url": url},
        event_type="clanops_admin_set_profile_url",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="clear-profile-url", description="Clear a member profile URL.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_clear_profile_url(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="clear-profile-url",
        args={"member": member},
        event_type="clanops_admin_clear_profile_url",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="set-poap-address", description="Set a member POAP address.")
@app_commands.describe(member="Member name or tag.", poap_address="Wallet or POAP address.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_set_poap_address(interaction: discord.Interaction, member: str, poap_address: str):
    await _run_admin_interaction(
        interaction,
        command_name="set-poap-address",
        args={"member": member, "poap_address": poap_address},
        event_type="clanops_admin_set_poap_address",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="clear-poap-address", description="Clear a member POAP address.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_clear_poap_address(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="clear-poap-address",
        args={"member": member},
        event_type="clanops_admin_clear_poap_address",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="set-note", description="Set a member note.")
@app_commands.describe(member="Member name or tag.", note="Leader note text.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_set_note(interaction: discord.Interaction, member: str, note: str):
    await _run_admin_interaction(
        interaction,
        command_name="set-note",
        args={"member": member, "note": note},
        event_type="clanops_admin_set_note",
        write=True,
    )


@ELIXIR_MEMBER_COMMANDS.command(name="clear-note", description="Clear a member note.")
@app_commands.describe(member="Member name or tag.")
@app_commands.autocomplete(member=_member_autocomplete)
async def slash_clear_note(interaction: discord.Interaction, member: str):
    await _run_admin_interaction(
        interaction,
        command_name="clear-note",
        args={"member": member},
        event_type="clanops_admin_clear_note",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="heartbeat", description="Force one heartbeat cycle now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_heartbeat(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="heartbeat",
        preview=preview,
        event_type="clanops_admin_heartbeat_preview" if preview else "clanops_admin_heartbeat",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="site-data", description="Force the site data refresh job now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_site_data(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="site-data",
        preview=preview,
        event_type="clanops_admin_site_data_preview" if preview else "clanops_admin_site_data",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="site-content", description="Force the full site content cycle now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_site_content(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="site-content",
        preview=preview,
        event_type="clanops_admin_site_content_preview" if preview else "clanops_admin_site_content",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="site-publish", description="Commit and push current Elixir-owned site files.")
@app_commands.describe(preview="Suppress site push.")
async def slash_site_publish(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="site-publish",
        preview=preview,
        event_type="clanops_admin_site_publish_preview" if preview else "clanops_admin_site_publish",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="home-message", description="Regenerate only the website home message.")
@app_commands.describe(preview="Suppress site push.")
async def slash_home_message(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="home-message",
        preview=preview,
        event_type="clanops_admin_home_message_preview" if preview else "clanops_admin_home_message",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="members-message", description="Regenerate only the website members message.")
@app_commands.describe(preview="Suppress site push.")
async def slash_members_message(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="members-message",
        preview=preview,
        event_type="clanops_admin_members_message_preview" if preview else "clanops_admin_members_message",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="roster-bios", description="Regenerate only roster intro and member bios.")
@app_commands.describe(preview="Suppress site push.")
async def slash_roster_bios(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="roster-bios",
        preview=preview,
        event_type="clanops_admin_roster_bios_preview" if preview else "clanops_admin_roster_bios",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="promote-content", description="Regenerate only the promotion payload locally.")
@app_commands.describe(preview="Suppress site push.")
async def slash_promote_content(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="promote-content",
        preview=preview,
        event_type="clanops_admin_promote_content_preview" if preview else "clanops_admin_promote_content",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="promotion", description="Force the promotion sync now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_promotion(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="promotion",
        preview=preview,
        event_type="clanops_admin_promotion_preview" if preview else "clanops_admin_promotion",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="player-intel", description="Force the player intel refresh job now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_player_intel(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="player-intel",
        preview=preview,
        event_type="clanops_admin_player_intel_preview" if preview else "clanops_admin_player_intel",
        write=True,
    )


@ELIXIR_JOB_COMMANDS.command(name="clanops-review", description="Force the weekly clanops review post now.")
@app_commands.describe(preview="Suppress Discord sends and site pushes.")
async def slash_clanops_review(interaction: discord.Interaction, preview: bool = False):
    await _run_admin_interaction(
        interaction,
        command_name="clanops-review",
        preview=preview,
        event_type="clanops_admin_clanops_review_preview" if preview else "clanops_admin_clanops_review",
        write=True,
    )


ELIXIR_COMMANDS.add_command(ELIXIR_MEMBER_COMMANDS)
ELIXIR_COMMANDS.add_command(ELIXIR_JOB_COMMANDS)


try:
    if APP_GUILD is not None:
        bot.tree.add_command(ELIXIR_COMMANDS, guild=APP_GUILD)
    else:
        bot.tree.add_command(ELIXIR_COMMANDS)
except Exception:
    pass

@bot.event
async def on_ready():
    global SLASH_COMMANDS_SYNCED
    log.info("Elixir online as %s", bot.user)
    prompts.ensure_valid_discord_channel_config()
    role_status = _member_role_grant_status()
    if role_status["configured"] and not role_status["ok"]:
        log.warning(
            "Member role auto-grant unavailable: %s (manage_roles=%s, bot_top_role_position=%s, member_role_position=%s)",
            role_status["reason"],
            role_status["manage_roles"],
            role_status["bot_top_role_position"],
            role_status["member_role_position"],
        )
    if not SLASH_COMMANDS_SYNCED:
        try:
            if APP_GUILD is not None:
                await bot.tree.sync(guild=APP_GUILD)
                log.info("Synced /elixir commands to guild %s", GUILD_ID)
            else:
                await bot.tree.sync()
                log.info("Synced global /elixir commands")
            SLASH_COMMANDS_SYNCED = True
        except Exception as exc:
            log.error("Slash command sync failed: %s", exc)
    if not scheduler.running:
        # Single heartbeat job replaces both the 4x/day observations and hourly member check
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_heartbeat_tick())
            ),
            "interval",
            minutes=HEARTBEAT_INTERVAL_MINUTES,
            jitter=HEARTBEAT_JITTER_SECONDS,
            id="heartbeat",
        )
        # Daily site publish for poapkings.com: refresh data, generate content, commit/push.
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_site_content_cycle())
            ),
            "cron",
            hour=SITE_CONTENT_HOUR,
            minute=0,
            id="site_content_cycle",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_player_intel_refresh())
            ),
            "interval",
            hours=PLAYER_INTEL_REFRESH_HOURS,
            id="player_intel_refresh",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_clanops_weekly_review())
            ),
            "cron",
            day_of_week=CLANOPS_WEEKLY_REVIEW_DAY,
            hour=CLANOPS_WEEKLY_REVIEW_HOUR,
            minute=0,
            id="clanops_weekly_review",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_promotion_content_cycle())
            ),
            "cron",
            day_of_week=PROMOTION_CONTENT_DAY,
            hour=PROMOTION_CONTENT_HOUR,
            minute=0,
            id="promotion_content_cycle",
        )
        scheduler.start()
        log.info("Scheduler started — heartbeat every %d minutes with up to %ds jitter (active %dam-%dpm Chicago), "
                 "site publish at %s, player intel refresh every %dh, clanops review %s at %02d:00, "
                 "promotion sync %s at %02d:00",
                 HEARTBEAT_INTERVAL_MINUTES, HEARTBEAT_JITTER_SECONDS, HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 _format_hour_label(SITE_CONTENT_HOUR), PLAYER_INTEL_REFRESH_HOURS,
                 CLANOPS_WEEKLY_REVIEW_DAY, CLANOPS_WEEKLY_REVIEW_HOUR,
                 PROMOTION_CONTENT_DAY, PROMOTION_CONTENT_HOUR)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    await asyncio.to_thread(
        db.upsert_discord_user,
        member.id,
        username=member.name,
        global_name=getattr(member, "global_name", None),
        display_name=member.display_name,
    )
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))
    if not channel:
        return
    msg = await asyncio.to_thread(
        elixir_agent.generate_message,
        "discord_member_join",
        f"A new user '{member.display_name}' ({member.mention}) just joined the Discord server. "
        f"Welcome them in #reception and explain how to set their server nickname "
        f"to match their Clash Royale in-game name to get verified.",
    )
    if msg:
        await channel.send(msg)
    else:
        await channel.send(
            f"Welcome to the server, {member.mention}! Set your server nickname "
            f"to your Clash Royale name and I'll get you verified."
        )


@bot.event
async def on_member_update(before, after):
    """Detect nickname changes and grant member role when name matches a clan member."""
    if before.nick == after.nick:
        return
    if not after.nick:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        after.id,
        username=after.name,
        global_name=getattr(after, "global_name", None),
        display_name=after.display_name,
    )

    # Only act if they don't already have the member role
    if not MEMBER_ROLE_ID:
        return
    member_role = after.guild.get_role(MEMBER_ROLE_ID)
    if not member_role or member_role in after.roles:
        return

    match = await asyncio.to_thread(_match_clan_member, after.nick)
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))

    if not match:
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "nickname_no_match",
                f"User {after.mention} set their nickname to '{after.nick}' but it doesn't "
                f"match anyone in the clan roster. Let them know and suggest they check "
                f"the spelling or join the clan first. Channel: #reception.",
            )
            await channel.send(msg or f"Hmm {after.mention}, I don't see **{after.nick}** in our roster.")
        return

    tag, cr_name = match
    await asyncio.to_thread(
        db.link_discord_user_to_member,
        after.id,
        tag,
        username=after.name,
        display_name=after.display_name,
        source="verified_nickname_match",
    )
    try:
        await after.add_roles(member_role, reason=f"Matched clan member: {cr_name} ({tag})")
    except discord.Forbidden:
        log.error("Cannot assign member role — check bot permissions and role hierarchy")
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "role_grant_failed",
                f"Matched user {after.mention} to clan member '{cr_name}' ({tag}) but "
                f"couldn't assign the member role due to permissions. Let them know "
                f"a leader will help. Channel: #reception.",
            )
            await channel.send(msg or f"I matched **{cr_name}** but couldn't assign the role.")
        return

    if channel:
        msg = await asyncio.to_thread(
            elixir_agent.generate_message,
            "nickname_matched",
            f"User {after.mention} set their nickname to '{cr_name}' which matches "
            f"clan member tag {tag}. They've been granted the member role. "
            f"Welcome them and let them know they have full access. Channel: #reception.",
        )
        await channel.send(msg or f"Welcome aboard, {cr_name}! You now have full access.")


def _is_legacy_clanops_command_text(text: str) -> bool:
    return bool(
        _is_help_request(text)
        or _is_status_request(text)
        or _is_schedule_request(text)
        or _clan_status_mode(text)
        or _is_clan_list_request(text)
        or _extract_profile_target(text)
        or parse_admin_command(text, require_prefix=True)
    )


def _build_clanops_command_hint() -> str:
    return (
        "Use `/elixir ...` for private clanops commands or mention me with `@Elixir do ...` "
        "for public room commands."
    )


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        message.author.id,
        username=message.author.name,
        global_name=getattr(message.author, "global_name", None),
        display_name=message.author.display_name,
    )
    channel_config = _get_channel_behavior(message.channel.id)
    mentioned = _is_bot_mentioned(message)
    if not channel_config:
        await bot.process_commands(message)
        return

    role = channel_config.get("role")
    workflow = channel_config.get("workflow")
    scope = _channel_scope(message.channel)
    conversation_scope = _channel_conversation_scope(message.channel, message.author.id)
    raw_question = _strip_bot_mentions(message.content) if mentioned else message.content.strip()

    # Non-responsive singleton channels are outbound only.
    if not channel_config.get("respond_allowed", True):
        return

    if role == "onboarding" and not mentioned:
        return

    clan_status_mode = _clan_status_mode(raw_question)

    if role == "interactive" and mentioned and _is_help_request(raw_question):
        log.info(
            "message_route route=help channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        help_content = await asyncio.to_thread(_build_help_report, role)
        workflow_name = "clanops" if role == "clanops" else workflow
        event_type = f"{role}_help"
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            discord_message_id=message.id,
        )
        await _reply_text(message, help_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            help_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            event_type=event_type,
        )
        return

    if role == "clanops" and _is_legacy_clanops_command_text(raw_question):
        if not mentioned or not parse_admin_command(raw_question, require_prefix=True):
            hint_content = _build_clanops_command_hint()
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "user",
                raw_question,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                discord_message_id=message.id,
            )
            await _reply_text(message, hint_content)
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "assistant",
                hint_content,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                event_type="clanops_command_hint",
            )
            return

    if role in {"clanops", "interactive"} and _is_roster_join_dates_request(raw_question):
        log.info(
            "message_route route=roster_join_dates_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        roster_content = await asyncio.to_thread(_build_roster_join_dates_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, roster_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            roster_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="roster_join_dates_report",
        )
        return

    deck_target = None
    if role in {"clanops", "interactive"} and _is_member_deck_request(raw_question):
        deck_target = await asyncio.to_thread(_extract_member_deck_target, raw_question, message)
    if role in {"clanops", "interactive"} and deck_target:
        log.info(
            "message_route route=member_deck_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s deck_target=%r raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            deck_target,
            raw_question,
            message.content,
        )
        deck_content = await asyncio.to_thread(_build_member_deck_report, deck_target)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, deck_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            deck_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="member_deck_report",
        )
        return

    if role == "clanops" and _is_kick_risk_request(raw_question):
        log.info(
            "message_route route=kick_risk_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        kick_risk_content = await asyncio.to_thread(_build_kick_risk_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, kick_risk_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            kick_risk_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="kick_risk_report",
        )
        return

    if role == "clanops" and _is_top_war_contributors_request(raw_question):
        log.info(
            "message_route route=top_war_contributors_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        top_war_content = await asyncio.to_thread(_build_top_war_contributors_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, top_war_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            top_war_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="top_war_contributors_report",
        )
        return

    profile_target = await asyncio.to_thread(_extract_profile_target, raw_question) if role == "clanops" else None
    if role == "clanops" and (_is_clan_list_request(raw_question) or profile_target):
        route = "clan_list_report" if _is_clan_list_request(raw_question) else "member_profile_report"
        log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        admin_content = await dispatch_admin_command(
            "clan-list" if _is_clan_list_request(raw_question) else "profile",
            preview=False,
            short=False,
            args={} if _is_clan_list_request(raw_question) else {"member": profile_target},
        )
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await _reply_text(message, admin_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            admin_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=route,
        )
        return

    admin_command = parse_admin_command(raw_question, require_prefix=True) if role == "clanops" and mentioned else None
    if admin_command:
        route = f"clanops_admin_{admin_command['command'].replace('-', '_')}"
        if admin_command.get("preview"):
            route += "_preview"
        log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        admin_content = await dispatch_admin_command(
            admin_command["command"],
            preview=admin_command.get("preview", False),
            short=admin_command.get("short", False),
            args=admin_command.get("args", {}),
        )
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await _reply_text(message, admin_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            admin_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=route,
        )
        return

    if role == "clanops" and (_is_status_request(raw_question) or _is_schedule_request(raw_question) or clan_status_mode):
        route = (
            "clan_status_report" if clan_status_mode == "full"
            else "clan_status_short_report" if clan_status_mode == "short"
            else "schedule_report" if _is_schedule_request(raw_question)
            else "status_report"
        )
        log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        clan = {}
        war = {}
        if clan_status_mode:
            try:
                clan, war = await _load_live_clan_context()
            except Exception as exc:
                log.warning("Clan status refresh failed: %s", exc)
        if clan_status_mode == "full":
            report_builder = _build_clan_status_report
            report_args = (clan, war)
            event_type = "clan_status_report"
        elif clan_status_mode == "short":
            report_builder = _build_clan_status_short_report
            report_args = (clan, war)
            event_type = "clan_status_short_report"
        elif _is_schedule_request(raw_question):
            report_builder = _build_schedule_report
            report_args = ()
            event_type = "schedule_report"
        else:
            report_builder = _build_status_report
            report_args = ()
            event_type = "status_report"
        status_content = await asyncio.to_thread(report_builder, *report_args)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await _reply_text(message, status_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            status_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=event_type,
        )
        return

    proactive = role == "clanops" and not mentioned
    if proactive and not _clanops_cooldown_elapsed(message.channel.id):
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            message.content.strip(),
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        return

    if not mentioned and not proactive:
        await bot.process_commands(message)
        return

    if role == "onboarding":
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                question = raw_question
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow="reception",
                    discord_message_id=message.id,
                )
                result = await asyncio.to_thread(
                    elixir_agent.respond_in_reception,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    memory_context=memory_context,
                )
                if result is None:
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="agent_none",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                if not isinstance(result, dict):
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=_preview_text(result),
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                content = result.get("content", result.get("summary", ""))
                if not content:
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="empty_result",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=_preview_text(result),
                        raw_json=result,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                await _reply_text(message, content)
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "assistant",
                    content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    workflow="reception",
                    event_type=result.get("event_type"),
                )
            except Exception as e:
                log.error("reception error: %s", e)
                _log_prompt_failure(
                    question=raw_question,
                    workflow="reception",
                    failure_type="exception",
                    failure_stage="on_message_reception",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                await message.reply("Hit an error. Try again in a moment.")
        return

    if workflow in {"interactive", "clanops"}:
        log.info(
            "message_route route=channel_llm channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s proactive=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            proactive,
            raw_question,
            message.content,
        )
        async with message.channel.typing():
            try:
                clan, war = await _load_live_clan_context()
                question = raw_question
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    conversation_scope,
                    CHANNEL_CONVERSATION_LIMIT,
                )
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )

                await asyncio.to_thread(
                    db.save_message,
                    conversation_scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                        workflow=workflow,
                        discord_message_id=message.id,
                    )

                result = await asyncio.to_thread(
                    elixir_agent.respond_in_channel,
                    question=question,
                    author_name=message.author.display_name,
                    channel_name=_channel_reply_target_name(channel_config),
                    workflow=workflow,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                    proactive=proactive,
                )
                if result is None:
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="agent_none",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return
                if not isinstance(result, dict):
                    log.error("%s channel error: invalid result type %s", workflow, type(result).__name__)
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=_preview_text(result),
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return

                content = result.get("content", result.get("summary", ""))
                if not content:
                    log.error("%s channel error: empty result payload %s", workflow, result)
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="empty_result",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=_preview_text(result),
                        raw_json=result,
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return
                await _share_channel_result(result, workflow)

                await asyncio.to_thread(
                        db.save_message,
                        conversation_scope,
                        "assistant",
                        content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow=workflow,
                    event_type=result.get("event_type"),
                )

                await _reply_text(message, content)
            except Exception as e:
                log.error("%s channel error: %s", workflow, e)
                _log_prompt_failure(
                    question=raw_question,
                    workflow=workflow,
                    failure_type="exception",
                    failure_stage="on_message_channel",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                if mentioned:
                    await message.reply("Hit an error. Try again in a moment.")
        return

    await bot.process_commands(message)


PID_FILE = os.path.join(os.path.dirname(__file__), "elixir.pid")


def _acquire_pid_file():
    """Write current PID to file, killing any stale process first."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            log.info("Killed stale process %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # PID invalid, process gone, or not ours
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _cleanup_pid_file():
    """Remove PID file on clean shutdown."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    _acquire_pid_file()
    atexit.register(_cleanup_pid_file)
    bot.run(TOKEN)
