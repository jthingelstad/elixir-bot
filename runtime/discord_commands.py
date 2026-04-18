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

    @member_commands.command(name="audit-discord", description=COMMAND_SPECS["member.audit-discord"].description)
    async def slash_member_audit_discord(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="member.audit-discord",
            event_type=COMMAND_SPECS["member.audit-discord"].event_type,
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

    tournament_commands = app_commands.Group(name="tournament", description="Clan tournament tracking commands")

    @tournament_commands.command(name="watch", description="Start watching a tournament by tag.")
    @app_commands.describe(tag="Tournament tag (e.g. #2QJJRJPR)")
    async def slash_tournament_watch(interaction: discord.Interaction, tag: str):
        if not await validate_admin_interaction(interaction, command_name="tournament.watch", write=True):
            return
        await interaction.response.defer(ephemeral=True)
        import cr_api
        from runtime import jobs

        # Clean tag
        clean_tag = tag.strip().lstrip("#").upper()
        if not clean_tag:
            await send_interaction_text(interaction, "Invalid tag.", use_followup=True)
            return

        # Check for existing active tournament
        active = await asyncio.to_thread(db.get_active_tournament)
        if active:
            await send_interaction_text(
                interaction,
                f"Already watching **{active.get('name', active['tournament_tag'])}** (`{active['tournament_tag']}`). Use `/elixir tournament stop` first.",
                use_followup=True,
            )
            return

        # Validate tag against API
        api_data = await asyncio.to_thread(cr_api.get_tournament, clean_tag)
        if api_data is None:
            await send_interaction_text(interaction, f"Tournament `#{clean_tag}` not found.", use_followup=True)
            return

        api_status = api_data.get("status") or ""
        if api_status == "ended":
            await send_interaction_text(interaction, f"Tournament **{api_data.get('name')}** has already ended.", use_followup=True)
            return

        # Register and start watching
        tournament_id = await asyncio.to_thread(db.register_tournament, clean_tag, api_data)
        jobs.start_tournament_watch()

        members = api_data.get("membersList") or []
        game_mode = api_data.get("gameMode") or {}
        status_label = {"inPreparation": "In Preparation", "inProgress": "In Progress"}.get(api_status, api_status)

        # Announce the watch to the clan via the awareness pipeline.
        from runtime.jobs._signals import _deliver_signal_group
        watching_signal = {
            "type": "tournament_watching_started",
            "signal_key": f"tournament_watching_started|{clean_tag}",
            "tournament_tag": clean_tag,
            "tournament_name": api_data.get("name"),
            "tournament_description": api_data.get("description"),
            "tournament_type": api_data.get("type"),
            "api_status": api_status,
            "status_label": status_label,
            "participant_count": len(members),
            "max_capacity": api_data.get("maxCapacity"),
            "game_mode_id": game_mode.get("id"),
            "game_mode_name": game_mode.get("name"),
            "level_cap": api_data.get("levelCap"),
            "preparation_duration_seconds": api_data.get("preparationDuration"),
            "duration_seconds": api_data.get("duration"),
            "created_time": api_data.get("createdTime"),
            "started_time": api_data.get("startedTime"),
            "poll_interval_minutes": jobs.TOURNAMENT_POLL_MINUTES,
        }
        try:
            await _deliver_signal_group([watching_signal], {}, {})
        except Exception as exc:
            app.log.warning("tournament_watching_started delivery failed: %s", exc)

        lines = [
            f"Watching **{api_data.get('name')}** (`#{clean_tag}`)",
            f"Status: {status_label}",
            f"Participants: {len(members)}",
            f"Game Mode: {game_mode.get('name') or game_mode.get('id', 'Unknown')}",
            f"Polling every {jobs.TOURNAMENT_POLL_MINUTES} minutes.",
        ]
        await send_interaction_text(interaction, "\n".join(lines), use_followup=True)

    @tournament_commands.command(name="status", description="Show active tournament tracking status.")
    async def slash_tournament_status(interaction: discord.Interaction):
        if not await validate_admin_interaction(interaction, command_name="tournament.status", write=False):
            return
        tournament = await asyncio.to_thread(db.get_active_tournament)
        if not tournament:
            # Show most recent ended tournament
            from db import get_connection
            def _latest():
                conn = get_connection()
                try:
                    row = conn.execute(
                        "SELECT * FROM tournaments ORDER BY tournament_id DESC LIMIT 1"
                    ).fetchone()
                    return dict(row) if row else None
                finally:
                    conn.close()
            latest = await asyncio.to_thread(_latest)
            if latest:
                await send_interaction_text(
                    interaction,
                    f"No active tournament. Last tracked: **{latest.get('name')}** (`{latest['tournament_tag']}`) — {latest['status']}, {latest.get('battles_captured', 0)} battles captured.",
                    ephemeral=True,
                )
            else:
                await send_interaction_text(interaction, "No tournaments tracked yet.", ephemeral=True)
            return

        lines = [
            f"**{tournament.get('name', tournament['tournament_tag'])}** (`{tournament['tournament_tag']}`)",
            f"Status: {tournament['status']}",
            f"Polls: {tournament.get('poll_count', 0)}",
            f"Battles captured: {tournament.get('battles_captured', 0)}",
            f"Last poll: {tournament.get('last_poll_at') or 'never'}",
            f"Watching since: {tournament.get('watching_started_at')}",
        ]
        await send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    @tournament_commands.command(name="stop", description="Stop watching the active tournament.")
    async def slash_tournament_stop(interaction: discord.Interaction):
        if not await validate_admin_interaction(interaction, command_name="tournament.stop", write=True):
            return
        from runtime import jobs
        from db import get_connection, _utcnow

        tournament = await asyncio.to_thread(db.get_active_tournament)
        if not tournament:
            await send_interaction_text(interaction, "No active tournament to stop.", ephemeral=True)
            return

        jobs.stop_tournament_watch()

        def _cancel():
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE tournaments SET status = 'cancelled', watching_ended_at = ? WHERE tournament_id = ?",
                    (_utcnow(), tournament["tournament_id"]),
                )
                conn.commit()
            finally:
                conn.close()
        await asyncio.to_thread(_cancel)

        name = tournament.get("name") or tournament["tournament_tag"]
        await send_interaction_text(
            interaction,
            f"Stopped watching **{name}**. {tournament.get('battles_captured', 0)} battles captured.",
            ephemeral=True,
        )

    @tournament_commands.command(name="recap", description="Generate or regenerate a tournament recap.")
    @app_commands.describe(tag="Tournament tag (defaults to most recent)")
    async def slash_tournament_recap(interaction: discord.Interaction, tag: str | None = None):
        if not await validate_admin_interaction(interaction, command_name="tournament.recap", write=True):
            return
        await interaction.response.defer(ephemeral=True)
        from runtime.jobs import _tournament_recap

        # Find the tournament
        if tag:
            clean_tag = tag.strip().lstrip("#").upper()
            tournament = await asyncio.to_thread(db.get_tournament_by_tag, clean_tag)
        else:
            from db import get_connection
            def _latest_with_battles():
                conn = get_connection()
                try:
                    row = conn.execute(
                        "SELECT * FROM tournaments WHERE battles_captured > 0 ORDER BY tournament_id DESC LIMIT 1"
                    ).fetchone()
                    return dict(row) if row else None
                finally:
                    conn.close()
            tournament = await asyncio.to_thread(_latest_with_battles)

        if not tournament:
            await send_interaction_text(interaction, "No tournament found.", use_followup=True)
            return

        if tournament.get("battles_captured", 0) == 0:
            await send_interaction_text(
                interaction,
                f"**{tournament.get('name')}** has no captured battles — recap needs battle data.",
                use_followup=True,
            )
            return

        name = tournament.get("name") or tournament["tournament_tag"]
        await send_interaction_text(
            interaction,
            f"Generating recap for **{name}**...",
            use_followup=True,
        )

        try:
            await _tournament_recap(tournament["tournament_tag"])
            await interaction.followup.send(f"Recap posted for **{name}**.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Recap generation failed: {e}", ephemeral=True)

    @tournament_commands.command(name="history", description="List past tournaments.")
    async def slash_tournament_history(interaction: discord.Interaction):
        if not await validate_admin_interaction(interaction, command_name="tournament.history", write=False):
            return

        from db import get_connection
        def _list_tournaments():
            conn = get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM tournaments ORDER BY tournament_id DESC LIMIT 10"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        tournaments = await asyncio.to_thread(_list_tournaments)
        if not tournaments:
            await send_interaction_text(interaction, "No tournaments tracked yet.", ephemeral=True)
            return

        lines = ["**Tournament History**\n"]
        for t in tournaments:
            status_icon = {"ended": "done", "in_progress": "live", "in_preparation": "prep", "cancelled": "cancelled", "watching": "watching"}.get(t["status"], t["status"])
            winner = ""
            if t["status"] == "ended":
                from db import get_connection as _gc
                def _get_winner(tid):
                    conn = _gc()
                    try:
                        row = conn.execute(
                            "SELECT player_name FROM tournament_participants WHERE tournament_id = ? AND final_rank = 1",
                            (tid,),
                        ).fetchone()
                        return row["player_name"] if row else None
                    finally:
                        conn.close()
                w = await asyncio.to_thread(_get_winner, t["tournament_id"])
                if w:
                    winner = f" — Winner: **{w}**"

            date = (t.get("created_time") or "")[:10]
            battles = t.get("battles_captured", 0)
            lines.append(
                f"`{t['tournament_tag']}` **{t.get('name', '?')}** [{status_icon}]\n"
                f"  {date} · {battles} battles{winner}"
            )

        await send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    elixir_commands.add_command(tournament_commands)
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

    # -- /quiz: top-level, visible to all members ----------------------------
    _register_quiz_commands(bot, app)


def _register_quiz_commands(bot, app) -> None:
    """Register /quiz as a standalone top-level command group (visible to everyone)."""
    from modules.card_training.views import CARD_TRAINING_CHANNEL_ID

    quiz_commands = app_commands.Group(name="quiz", description="Card knowledge quiz")

    def _is_card_training_channel(interaction: discord.Interaction) -> bool:
        return CARD_TRAINING_CHANNEL_ID != 0 and interaction.channel_id == CARD_TRAINING_CHANNEL_ID

    async def _require_quiz_channel(interaction: discord.Interaction) -> bool:
        if _is_card_training_channel(interaction):
            return True
        msg = (
            f"Use this command in <#{CARD_TRAINING_CHANNEL_ID}>."
            if CARD_TRAINING_CHANNEL_ID
            else "The #card-quiz channel is not configured yet."
        )
        await interaction.response.send_message(msg, ephemeral=True)
        return False

    @quiz_commands.command(name="start", description="Start a card knowledge quiz.")
    @app_commands.describe(questions="Number of questions (1-10, default 5)")
    async def slash_quiz_start(interaction: discord.Interaction, questions: app_commands.Range[int, 1, 10] = 5):
        if not await _require_quiz_channel(interaction):
            return
        from modules.card_training.views import start_interactive_quiz
        await start_interactive_quiz(interaction, questions)

    @quiz_commands.command(name="stats", description="View your quiz stats and streak.")
    async def slash_quiz_stats(interaction: discord.Interaction):
        if not await _require_quiz_channel(interaction):
            return
        from modules.card_training import storage as quiz_storage
        stats = await asyncio.to_thread(quiz_storage.get_member_quiz_stats, str(interaction.user.id))

        lines = []
        total_q = stats["total_questions"]
        total_c = stats["total_correct"]
        pct = round(100 * total_c / total_q) if total_q > 0 else 0
        lines.append(f"**Sessions completed:** {stats['total_sessions']}")
        lines.append(f"**Overall accuracy:** {total_c}/{total_q} ({pct}%)")

        streak = stats.get("daily_streak")
        if streak:
            lines.append(f"**Current daily streak:** {streak['current_streak']}")
            lines.append(f"**Longest daily streak:** {streak['longest_streak']}")
            daily_total = streak["total_daily_answered"]
            daily_correct = streak["total_daily_correct"]
            daily_pct = round(100 * daily_correct / daily_total) if daily_total > 0 else 0
            lines.append(f"**Daily accuracy:** {daily_correct}/{daily_total} ({daily_pct}%)")
        else:
            lines.append("No daily quiz activity yet.")

        embed = discord.Embed(
            title="Your Quiz Stats",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @quiz_commands.command(name="leaderboard", description="View the daily quiz streak leaderboard.")
    async def slash_quiz_leaderboard(interaction: discord.Interaction):
        if not await _require_quiz_channel(interaction):
            return
        from modules.card_training import storage as quiz_storage
        board = await asyncio.to_thread(quiz_storage.get_quiz_leaderboard, 10)

        if not board:
            await interaction.response.send_message(
                "No one has answered a daily question yet. Be the first!",
                ephemeral=True,
            )
            return

        lines = []
        for i, entry in enumerate(board, 1):
            user_id = entry["discord_user_id"]
            streak = entry["current_streak"]
            longest = entry["longest_streak"]
            lines.append(f"**{i}.** <@{user_id}> — {streak} day streak (best: {longest})")

        embed = discord.Embed(
            title="Daily Quiz Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    try:
        if app.APP_GUILD is not None:
            bot.tree.add_command(quiz_commands, guild=app.APP_GUILD)
        else:
            bot.tree.add_command(quiz_commands)
    except app_commands.CommandAlreadyRegistered:
        app.log.info("/quiz slash commands already registered")
    except Exception as exc:
        app.log.error("/quiz command registration failed: %s", exc)
        raise
