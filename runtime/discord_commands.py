"""Slash command registration for Elixir."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

import db
from runtime.activities import manual_activity_choices
from runtime.admin import admin_command_requires_leader, dispatch_admin_command, render_admin_help


def register_elixir_app_commands(bot) -> None:
    import runtime.app as app

    elixir_commands = app_commands.Group(name="elixir", description="Elixir clanops commands")
    profile_commands = app_commands.Group(name="profile", description="Member profile and metadata commands")
    memory_commands = app_commands.Group(name="memory", description="Inspect Elixir memory")
    job_commands = app_commands.Group(name="jobs", description="Operational job commands")

    async def send_interaction_text(interaction: discord.Interaction, content: str, *, ephemeral: bool = True, use_followup: bool = False):
        chunks = app._chunk_discord_text(content)
        if not chunks:
            chunks = ["_No content._"]
        if use_followup:
            await interaction.edit_original_response(content=chunks[0])
            start = 1
        elif not interaction.response.is_done():
            await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
            start = 1
        else:
            await interaction.followup.send(chunks[0], ephemeral=ephemeral)
            start = 1
        for chunk in chunks[start:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

    async def validate_admin_interaction(
        interaction: discord.Interaction,
        *,
        command_name: str,
        write: bool = False,
    ) -> bool:
        if not app._is_clanops_channel(interaction.channel):
            await send_interaction_text(interaction, "Use `/elixir ...` in `#clanops`.", ephemeral=True)
            return False
        if admin_command_requires_leader(command_name) and not app._has_leader_role(interaction.user):
            await send_interaction_text(interaction, "Leader role required for this command.", ephemeral=True)
            return False
        app.log.info(
            "slash_command command=%s channel_id=%s author_id=%s write=%s",
            command_name,
            getattr(interaction.channel, "id", None),
            getattr(interaction.user, "id", None),
            write,
        )
        return True

    async def run_admin_interaction(
        interaction: discord.Interaction,
        *,
        command_name: str,
        preview: bool = False,
        short: bool = False,
        args: dict | None = None,
        event_type: str | None = None,
        write: bool = False,
    ):
        if not await validate_admin_interaction(interaction, command_name=command_name, write=write):
            return
        use_followup = False
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
            use_followup = True
        content = await dispatch_admin_command(
            command_name,
            preview=preview,
            short=short,
            args=args or {},
        )
        await send_interaction_text(interaction, content, ephemeral=True, use_followup=use_followup)

    async def member_autocomplete(
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

    @elixir_commands.command(name="help", description="Show Elixir clanops help.")
    async def slash_help(interaction: discord.Interaction):
        if not await validate_admin_interaction(interaction, command_name="help", write=False):
            return
        content = render_admin_help()
        await send_interaction_text(interaction, content, ephemeral=True)

    @elixir_commands.command(name="status", description="Show Elixir runtime health and telemetry.")
    async def slash_status(interaction: discord.Interaction):
        await run_admin_interaction(interaction, command_name="status", event_type="status_report")

    @elixir_commands.command(name="db-status", description="Show database storage status and grouped table summaries.")
    @app_commands.describe(view="Optional focused view.")
    @app_commands.choices(view=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Clan", value="clan"),
        app_commands.Choice(name="War", value="war"),
        app_commands.Choice(name="Memory", value="memory"),
    ])
    async def slash_db_status(interaction: discord.Interaction, view: str | None = None):
        group = None if not view or view == "all" else view
        await run_admin_interaction(
            interaction,
            command_name="db-status",
            args={} if group is None else {"group": group},
            event_type="db_status_report" if group is None else f"db_status_{group}_report",
        )

    @elixir_commands.command(name="schedule", description="Show scheduled jobs and next runs.")
    async def slash_schedule(interaction: discord.Interaction):
        await run_admin_interaction(interaction, command_name="schedule", event_type="schedule_report")

    @elixir_commands.command(name="signals", description="Show signal routing and recent routed signals.")
    async def slash_signals(interaction: discord.Interaction):
        await run_admin_interaction(interaction, command_name="signals", event_type="signals_report")

    @elixir_commands.command(name="clan-status", description="Show the operational clan status report.")
    @app_commands.describe(short="Return the compact clan status variant.")
    async def slash_clan_status(interaction: discord.Interaction, short: bool = False):
        await run_admin_interaction(
            interaction,
            command_name="clan-status",
            short=short,
            event_type="clan_status_short_report" if short else "clan_status_report",
        )

    @elixir_commands.command(name="war-status", description="Show the live war-awareness status report.")
    async def slash_war_status(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="war-status",
            event_type="war_status_report",
        )

    @elixir_commands.command(name="clan-list", description="List active clan members.")
    @app_commands.describe(full="Return the expanded metadata variant.")
    async def slash_clan_list(interaction: discord.Interaction, full: bool = False):
        await run_admin_interaction(
            interaction,
            command_name="clan-list",
            args={"full": "true" if full else "false"},
            event_type="clan_list_full_report" if full else "clan_list_report",
        )

    @profile_commands.command(name="show", description="Show the stored member profile and metadata.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_profile_show(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="profile",
            args={"member": member},
            event_type="member_profile_report",
        )

    @memory_commands.command(name="show", description="Inspect stored conversation and contextual memory.")
    @app_commands.describe(
        member="Optional member name or tag filter.",
        query="Optional contextual-memory search text.",
        limit="Maximum items to show per section.",
        system_internal="Include system-internal contextual memories.",
    )
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_memory_show(
        interaction: discord.Interaction,
        member: str | None = None,
        query: str | None = None,
        limit: app_commands.Range[int, 1, 10] = 5,
        system_internal: bool = False,
    ):
        await run_admin_interaction(
            interaction,
            command_name="memory",
            args={
                "member": member,
                "query": query,
                "limit": str(limit),
                "include_system_internal": "true" if system_internal else "false",
            },
            event_type="memory_report",
        )

    @profile_commands.command(name="verify-discord", description="Verify a member's Discord link and Member role.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_verify_discord(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="verify-discord",
            args={"member": member},
            event_type="clanops_admin_verify_discord",
            write=True,
        )

    @profile_commands.command(name="set-discord", description="Manually assign a Discord identity to a member.")
    @app_commands.describe(member="Member name or tag.", discord_name="Discord username or display name.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_discord(interaction: discord.Interaction, member: str, discord_name: str):
        await run_admin_interaction(
            interaction,
            command_name="set-discord",
            args={"member": member, "discord_name": discord_name},
            event_type="clanops_admin_set_discord",
            write=True,
        )

    @profile_commands.command(name="set-join-date", description="Set a member join date.")
    @app_commands.describe(member="Member name or tag.", date="Join date in YYYY-MM-DD format.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_join_date(interaction: discord.Interaction, member: str, date: str):
        await run_admin_interaction(
            interaction,
            command_name="set-join-date",
            args={"member": member, "date": date},
            event_type="clanops_admin_set_join_date",
            write=True,
        )

    @profile_commands.command(name="clear-join-date", description="Clear a member join date.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_clear_join_date(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="clear-join-date",
            args={"member": member},
            event_type="clanops_admin_clear_join_date",
            write=True,
        )

    @profile_commands.command(name="set-birthday", description="Set a member birthday.")
    @app_commands.describe(member="Member name or tag.", month="Birthday month.", day="Birthday day.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_birthday(interaction: discord.Interaction, member: str, month: int, day: int):
        await run_admin_interaction(
            interaction,
            command_name="set-birthday",
            args={"member": member, "month": str(month), "day": str(day)},
            event_type="clanops_admin_set_birthday",
            write=True,
        )

    @profile_commands.command(name="clear-birthday", description="Clear a member birthday.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_clear_birthday(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="clear-birthday",
            args={"member": member},
            event_type="clanops_admin_clear_birthday",
            write=True,
        )

    @profile_commands.command(name="set-profile-url", description="Set a member profile URL.")
    @app_commands.describe(member="Member name or tag.", url="Profile URL.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_profile_url(interaction: discord.Interaction, member: str, url: str):
        await run_admin_interaction(
            interaction,
            command_name="set-profile-url",
            args={"member": member, "url": url},
            event_type="clanops_admin_set_profile_url",
            write=True,
        )

    @profile_commands.command(name="clear-profile-url", description="Clear a member profile URL.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_clear_profile_url(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="clear-profile-url",
            args={"member": member},
            event_type="clanops_admin_clear_profile_url",
            write=True,
        )

    @profile_commands.command(name="set-poap-address", description="Set a member POAP address.")
    @app_commands.describe(member="Member name or tag.", poap_address="Wallet or POAP address.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_poap_address(interaction: discord.Interaction, member: str, poap_address: str):
        await run_admin_interaction(
            interaction,
            command_name="set-poap-address",
            args={"member": member, "poap_address": poap_address},
            event_type="clanops_admin_set_poap_address",
            write=True,
        )

    @profile_commands.command(name="clear-poap-address", description="Clear a member POAP address.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_clear_poap_address(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="clear-poap-address",
            args={"member": member},
            event_type="clanops_admin_clear_poap_address",
            write=True,
        )

    @profile_commands.command(name="set-note", description="Set a member note.")
    @app_commands.describe(member="Member name or tag.", note="Leader note text.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_set_note(interaction: discord.Interaction, member: str, note: str):
        await run_admin_interaction(
            interaction,
            command_name="set-note",
            args={"member": member, "note": note},
            event_type="clanops_admin_set_note",
            write=True,
        )

    @profile_commands.command(name="clear-note", description="Clear a member note.")
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_clear_note(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="clear-note",
            args={"member": member},
            event_type="clanops_admin_clear_note",
            write=True,
        )

    JOB_CHOICES = [
        app_commands.Choice(name=label, value=value)
        for label, value in (
            [("poap-kings-sync", "poap-kings-sync")]
            + manual_activity_choices()
        )
    ]

    @job_commands.command(name="run", description="Run one operational job now.")
    @app_commands.describe(job="Job to run.", preview="Suppress Discord sends and site pushes when supported.")
    @app_commands.choices(job=JOB_CHOICES)
    async def slash_run_job(interaction: discord.Interaction, job: str, preview: bool = False):
        await run_admin_interaction(
            interaction,
            command_name=job,
            preview=preview,
            event_type=f"clanops_admin_{job.replace('-', '_')}_preview" if preview else f"clanops_admin_{job.replace('-', '_')}",
            write=True,
        )

    elixir_commands.add_command(profile_commands)
    elixir_commands.add_command(memory_commands)
    elixir_commands.add_command(job_commands)

    try:
        if app.APP_GUILD is not None:
            bot.tree.add_command(elixir_commands, guild=app.APP_GUILD)
        else:
            bot.tree.add_command(elixir_commands)
    except app_commands.CommandAlreadyRegistered:
        app.log.info("/elixir slash commands already registered")
    except Exception as exc:
        app.log.error("Slash command registration failed: %s", exc)
        raise
