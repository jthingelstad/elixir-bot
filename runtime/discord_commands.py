"""Slash command registration for Elixir."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

import db
from runtime.activities import manual_activity_choices
from runtime.admin import COMMAND_SPECS, admin_command_requires_leader, dispatch_admin_command, render_admin_help


def register_elixir_app_commands(bot) -> None:
    import runtime.app as app

    elixir_commands = app_commands.Group(name="elixir", description="Elixir clanops commands")
    system_commands = app_commands.Group(name="system", description="Runtime health, storage, and schedule commands")
    clan_commands = app_commands.Group(name="clan", description="Clan-wide status and roster commands")
    member_commands = app_commands.Group(name="member", description="Single-member inspection and metadata commands")
    memory_commands = app_commands.Group(name="memory", description="Inspect Elixir memory")
    signal_commands = app_commands.Group(name="signal", description="Signal routing and system-signal commands")
    activity_commands = app_commands.Group(name="activity", description="Recurring activity inspection and manual run commands")
    integration_commands = app_commands.Group(name="integration", description="Integration modules and external publishing")

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

    @system_commands.command(name="status", description=COMMAND_SPECS["system.status"].description)
    async def slash_system_status(interaction: discord.Interaction):
        await run_admin_interaction(interaction, command_name="system.status", event_type=COMMAND_SPECS["system.status"].event_type)

    @system_commands.command(name="storage", description=COMMAND_SPECS["system.storage"].description)
    @app_commands.describe(view="Optional focused storage view.")
    @app_commands.choices(view=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Clan", value="clan"),
        app_commands.Choice(name="War", value="war"),
        app_commands.Choice(name="Memory", value="memory"),
    ])
    async def slash_system_storage(interaction: discord.Interaction, view: str | None = None):
        await run_admin_interaction(
            interaction,
            command_name="system.storage",
            args={"view": view or "all"},
            event_type=COMMAND_SPECS["system.storage"].event_type,
        )

    @system_commands.command(name="schedule", description=COMMAND_SPECS["system.schedule"].description)
    async def slash_system_schedule(interaction: discord.Interaction):
        await run_admin_interaction(interaction, command_name="system.schedule", event_type=COMMAND_SPECS["system.schedule"].event_type)

    @clan_commands.command(name="status", description=COMMAND_SPECS["clan.status"].description)
    @app_commands.describe(short="Return the compact clan status variant.")
    async def slash_clan_status(interaction: discord.Interaction, short: bool = False):
        await run_admin_interaction(
            interaction,
            command_name="clan.status",
            short=short,
            event_type=COMMAND_SPECS["clan.status"].event_type,
        )

    @clan_commands.command(name="war", description=COMMAND_SPECS["clan.war"].description)
    async def slash_war_status(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="clan.war",
            event_type=COMMAND_SPECS["clan.war"].event_type,
        )

    @clan_commands.command(name="members", description=COMMAND_SPECS["clan.members"].description)
    @app_commands.describe(detail="Choose summary or full member detail.")
    @app_commands.choices(detail=[
        app_commands.Choice(name="Summary", value="summary"),
        app_commands.Choice(name="Full", value="full"),
    ])
    async def slash_clan_members(interaction: discord.Interaction, detail: str = "summary"):
        await run_admin_interaction(
            interaction,
            command_name="clan.members",
            args={"detail": detail},
            event_type=COMMAND_SPECS["clan.members"].event_type,
        )

    @member_commands.command(name="show", description=COMMAND_SPECS["member.show"].description)
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_show(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="member.show",
            args={"member": member},
            event_type=COMMAND_SPECS["member.show"].event_type,
        )

    @memory_commands.command(name="show", description=COMMAND_SPECS["memory.show"].description)
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
            command_name="memory.show",
            args={
                "member": member,
                "query": query,
                "limit": str(limit),
                "include_system_internal": "true" if system_internal else "false",
            },
            event_type=COMMAND_SPECS["memory.show"].event_type,
        )

    @member_commands.command(name="verify-discord", description=COMMAND_SPECS["member.verify-discord"].description)
    @app_commands.describe(member="Member name or tag.")
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_verify_discord(interaction: discord.Interaction, member: str):
        await run_admin_interaction(
            interaction,
            command_name="member.verify-discord",
            args={"member": member},
            event_type=COMMAND_SPECS["member.verify-discord"].event_type,
            write=True,
        )

    @member_commands.command(name="set", description=COMMAND_SPECS["member.set"].description)
    @app_commands.describe(member="Member name or tag.", field="Field to set.", value="Field value.")
    @app_commands.choices(field=[
        app_commands.Choice(name="Discord", value="discord"),
        app_commands.Choice(name="Join Date", value="join-date"),
        app_commands.Choice(name="Birthday", value="birthday"),
        app_commands.Choice(name="Profile URL", value="profile-url"),
        app_commands.Choice(name="POAP Address", value="poap-address"),
        app_commands.Choice(name="Note", value="note"),
    ])
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_set(interaction: discord.Interaction, member: str, field: str, value: str):
        await run_admin_interaction(
            interaction,
            command_name="member.set",
            args={"member": member, "field": field, "value": value},
            event_type=COMMAND_SPECS["member.set"].event_type,
            write=True,
        )

    @member_commands.command(name="clear", description=COMMAND_SPECS["member.clear"].description)
    @app_commands.describe(member="Member name or tag.", field="Field to clear.")
    @app_commands.choices(field=[
        app_commands.Choice(name="Discord", value="discord"),
        app_commands.Choice(name="Join Date", value="join-date"),
        app_commands.Choice(name="Birthday", value="birthday"),
        app_commands.Choice(name="Profile URL", value="profile-url"),
        app_commands.Choice(name="POAP Address", value="poap-address"),
        app_commands.Choice(name="Note", value="note"),
    ])
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_clear(interaction: discord.Interaction, member: str, field: str):
        await run_admin_interaction(
            interaction,
            command_name="member.clear",
            args={"member": member, "field": field},
            event_type=COMMAND_SPECS["member.clear"].event_type,
            write=True,
        )

    @signal_commands.command(name="show", description=COMMAND_SPECS["signal.show"].description)
    @app_commands.describe(view="Choose which signal slice to inspect.", limit="Maximum recent signals to show.")
    @app_commands.choices(view=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Routes", value="routes"),
        app_commands.Choice(name="Recent", value="recent"),
        app_commands.Choice(name="Pending", value="pending"),
    ])
    async def slash_signal_show(interaction: discord.Interaction, view: str = "all", limit: app_commands.Range[int, 1, 20] = 10):
        await run_admin_interaction(
            interaction,
            command_name="signal.show",
            args={"view": view, "limit": str(limit)},
            event_type=COMMAND_SPECS["signal.show"].event_type,
        )

    @signal_commands.command(name="publish-pending", description=COMMAND_SPECS["signal.publish-pending"].description)
    @app_commands.describe(preview="Suppress Discord sends when supported.")
    async def slash_signal_publish_pending(interaction: discord.Interaction, preview: bool = False):
        await run_admin_interaction(
            interaction,
            command_name="signal.publish-pending",
            preview=preview,
            event_type=COMMAND_SPECS["signal.publish-pending"].event_type,
            write=True,
        )

    ACTIVITY_CHOICES = [
        app_commands.Choice(name=label, value=value)
        for label, value in manual_activity_choices()
    ]

    @activity_commands.command(name="list", description=COMMAND_SPECS["activity.list"].description)
    async def slash_activity_list(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="activity.list",
            event_type=COMMAND_SPECS["activity.list"].event_type,
        )

    @activity_commands.command(name="show", description=COMMAND_SPECS["activity.show"].description)
    @app_commands.describe(activity="Activity to inspect.")
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def slash_activity_show(interaction: discord.Interaction, activity: str):
        await run_admin_interaction(
            interaction,
            command_name="activity.show",
            args={"activity": activity},
            event_type=COMMAND_SPECS["activity.show"].event_type,
        )

    @activity_commands.command(name="run", description=COMMAND_SPECS["activity.run"].description)
    @app_commands.describe(activity="Activity to run.", preview="Suppress Discord sends and site pushes when supported.")
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def slash_activity_run(interaction: discord.Interaction, activity: str, preview: bool = False):
        await run_admin_interaction(
            interaction,
            command_name="activity.run",
            args={"activity": activity},
            preview=preview,
            event_type=COMMAND_SPECS["activity.run"].event_type,
            write=True,
        )

    @integration_commands.command(name="list", description=COMMAND_SPECS["integration.list"].description)
    async def slash_integration_list(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="integration.list",
            event_type=COMMAND_SPECS["integration.list"].event_type,
        )

    @integration_commands.command(name="status", description="Show status for an integration module.")
    @app_commands.describe(integration="Integration module to inspect.")
    @app_commands.choices(integration=[
        app_commands.Choice(name="POAP KINGS", value="poap-kings"),
    ])
    async def slash_integration_status(interaction: discord.Interaction, integration: str):
        if integration != "poap-kings":
            await send_interaction_text(interaction, f"Unsupported integration: {integration}", ephemeral=True)
            return
        await run_admin_interaction(
            interaction,
            command_name="integration.poap-kings.status",
            event_type=COMMAND_SPECS["integration.poap-kings.status"].event_type,
        )

    @integration_commands.command(name="publish", description="Publish content through an integration module.")
    @app_commands.describe(
        integration="Integration module to publish through.",
        target="POAP KINGS publish target.",
        preview="Suppress Discord sends and site pushes when supported.",
    )
    @app_commands.choices(integration=[
        app_commands.Choice(name="POAP KINGS", value="poap-kings"),
    ])
    @app_commands.choices(target=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Data", value="data"),
        app_commands.Choice(name="Home", value="home"),
        app_commands.Choice(name="Members", value="members"),
        app_commands.Choice(name="Roster Bios", value="roster-bios"),
        app_commands.Choice(name="Promote", value="promote"),
    ])
    async def slash_integration_publish(
        interaction: discord.Interaction,
        integration: str,
        target: str,
        preview: bool = False,
    ):
        if integration != "poap-kings":
            await send_interaction_text(interaction, f"Unsupported integration: {integration}", ephemeral=True)
            return
        await run_admin_interaction(
            interaction,
            command_name="integration.poap-kings.publish",
            args={"target": target},
            preview=preview,
            event_type=COMMAND_SPECS["integration.poap-kings.publish"].event_type,
            write=True,
        )

    elixir_commands.add_command(system_commands)
    elixir_commands.add_command(clan_commands)
    elixir_commands.add_command(member_commands)
    elixir_commands.add_command(memory_commands)
    elixir_commands.add_command(signal_commands)
    elixir_commands.add_command(activity_commands)
    elixir_commands.add_command(integration_commands)

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
