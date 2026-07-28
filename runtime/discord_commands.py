"""Slash command registration for Elixir."""

from __future__ import annotations

import asyncio
import os

import discord
from discord import app_commands

import db
from runtime.activities import manual_activity_choices
from runtime.admin import (
    COMMAND_SPECS,
    admin_command_requires_leader,
    dispatch_admin_command,
    render_admin_help,
)


def _is_arena_relay_command_channel(app, channel, command_name: str) -> bool:
    if not str(command_name or "").startswith("relay."):
        return False
    channel_config = app._get_channel_behavior(getattr(channel, "id", 0))
    return bool(channel_config and channel_config.get("lane") == "arena-relay")


def register_elixir_app_commands(bot) -> None:
    import runtime.app as app

    # /elixir is the MEMBER surface (email + help). /clanops is the co-leader
    # surface (everything gated by validate_admin_interaction). memory/system/signal
    # were dropped from Discord — the Observatory web app owns that telemetry and
    # management (and co-leaders don't need them).
    elixir_commands = app_commands.Group(name="elixir", description="Your Elixir profile & email")
    clanops_commands = app_commands.Group(name="clanops", description="Elixir leader operations")
    clan_commands = app_commands.Group(
        name="clan", description="Clan-wide status and roster commands"
    )
    member_commands = app_commands.Group(
        name="member", description="Single-member inspection and metadata commands"
    )
    relay_commands = app_commands.Group(name="relay", description="Leader action relay board")
    activity_commands = app_commands.Group(
        name="activity",
        description="Recurring activity inspection and manual run commands",
    )
    email_commands = app_commands.Group(
        name="email", description="Add and verify your email on your clan profile"
    )

    async def send_interaction_text(
        interaction: discord.Interaction,
        content: str,
        *,
        ephemeral: bool = True,
        use_followup: bool = False,
    ):
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
        if not (
            app._is_clanops_channel(interaction.channel)
            or _is_arena_relay_command_channel(app, interaction.channel, command_name)
        ):
            await send_interaction_text(
                interaction, "Use `/clanops ...` in `#clanops`.", ephemeral=True
            )
            return False
        if admin_command_requires_leader(command_name) and not app._has_leader_role(
            interaction.user
        ):
            await send_interaction_text(
                interaction, "Leader role required for this command.", ephemeral=True
            )
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
        if not await validate_admin_interaction(
            interaction, command_name=command_name, write=write
        ):
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

    @elixir_commands.command(name="help", description="How to use Elixir as a clan member.")
    async def slash_elixir_help(interaction: discord.Interaction):
        # Member-facing help — no admin gate. Explains the self-service email flow
        # and points leaders at /clanops.
        await interaction.response.defer(ephemeral=True)
        content = (
            "**Elixir — for members** 👑\n\n"
            "Link an email to your clan profile to get your personalized weekly "
            "Clash Royale report:\n"
            "• `/elixir email set <address>` — I'll email you a 6-digit code\n"
            "• `/elixir email verify <code>` — enter the code to confirm\n"
            "• `/elixir email show` — see the email on your profile\n\n"
            "_Leaders:_ operator commands live under `/clanops`."
        )
        await send_interaction_text(interaction, content, ephemeral=True)

    @clanops_commands.command(name="help", description="Show Elixir leader operations help.")
    async def slash_clanops_help(interaction: discord.Interaction):
        if not await validate_admin_interaction(interaction, command_name="help", write=False):
            return
        content = render_admin_help()
        await send_interaction_text(interaction, content, ephemeral=True)

    async def _post_release_relay(text: str) -> str:
        """Consume cut_release.py's clan-chat marker line, post the #actions
        card, and strip the marker from the ephemeral reply."""
        marker = "::RELEASE_CLANCHAT::"
        kept: list[str] = []
        note = None
        for line in text.splitlines():
            if line.startswith(marker):
                parts = line.split(marker)  # ['', tag, clanchat]
                tag = parts[1] if len(parts) > 1 else ""
                clanchat = parts[2] if len(parts) > 2 else ""
                try:
                    from runtime.jobs._core import post_release_relay_card

                    ok = await post_release_relay_card(clanchat, tag=tag)
                    note = (
                        "📋 Clan-chat card posted to #actions."
                        if ok
                        else "Clan-chat card not posted (arena-relay unavailable)."
                    )
                except Exception:
                    app.log.exception("release relay card failed")
                    note = "Clan-chat card failed to post (see logs)."
                continue
            kept.append(line)
        result = "\n".join(kept).strip()
        return f"{result}\n{note}" if note else result

    @clanops_commands.command(
        name="release",
        description="Draft or publish Elixir release notes (leader).",
    )
    @app_commands.describe(
        name="Optional: use this release name instead of coining one",
        publish="Actually cut & announce (default: preview only)",
    )
    async def slash_release(
        interaction: discord.Interaction,
        name: str | None = None,
        publish: bool = False,
    ):
        # Preview by default; publish runs the full cut_release flow — RELEASES.md,
        # name-slug tag, GitHub release, email, #announcements, and the clan-chat
        # leader-action card. No version number: identity is name + date + hash.
        if not await validate_admin_interaction(interaction, command_name="release", write=publish):
            return
        if not app._has_leader_role(interaction.user):
            await send_interaction_text(
                interaction, "Leader role required for this command.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        import sys as _sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd = [_sys.executable, os.path.join("scripts", "cut_release.py")]
        if name:
            cmd += ["--name", name]
        if not publish:
            cmd.append("--dry-run")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=repo_root,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            text = out.decode("utf-8", errors="replace").strip() or "(no output)"
            # On a real cut, cut_release.py emits the clan-chat blurb on a marker
            # line; post it as a #actions card and hide the marker from the
            # ephemeral reply.
            if publish and proc.returncode == 0:
                text = await _post_release_relay(text)
            if proc.returncode != 0:
                text = f"release script exited {proc.returncode}:\n{text[-1500:]}"
        except asyncio.TimeoutError:
            # hygiene: answers the interaction, so a human is told directly
            text = "Release script timed out after 10 minutes — check the host."
        except Exception as exc:  # the command must always answer the interaction
            app.log.exception("slash release failed")
            text = f"Release command failed: {exc}"
        await send_interaction_text(interaction, text[-3800:], ephemeral=True)

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
    @app_commands.choices(
        detail=[
            app_commands.Choice(name="Summary", value="summary"),
            app_commands.Choice(name="Full", value="full"),
        ]
    )
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

    @relay_commands.command(name="status", description=COMMAND_SPECS["relay.status"].description)
    @app_commands.describe(view="Which leader actions to show.", limit="Maximum actions to list.")
    @app_commands.choices(
        view=[
            app_commands.Choice(name="All", value="all"),
            app_commands.Choice(name="Pending", value="pending"),
            app_commands.Choice(name="Decided", value="decided"),
        ]
    )
    async def slash_relay_status(
        interaction: discord.Interaction,
        view: str = "all",
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        await run_admin_interaction(
            interaction,
            command_name="relay.status",
            args={"view": view, "limit": str(limit)},
            event_type=COMMAND_SPECS["relay.status"].event_type,
        )

    from runtime.leader_action_ui import leader_action_spec, leader_action_type_choices

    LEADER_ACTION_TYPE_CHOICES = [
        app_commands.Choice(name=leader_action_spec(action_type).label, value=action_type)
        for action_type in leader_action_type_choices()
    ]

    @relay_commands.command(
        name="test-card", description=COMMAND_SPECS["relay.test-card"].description
    )
    @app_commands.describe(
        action_type="Real leader action type to render.",
        target_name="Optional display name for member-specific cards.",
    )
    @app_commands.choices(action_type=LEADER_ACTION_TYPE_CHOICES)
    async def slash_relay_test_card(
        interaction: discord.Interaction,
        action_type: str,
        target_name: str | None = None,
    ):
        if not await validate_admin_interaction(
            interaction, command_name="relay.test-card", write=True
        ):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            target_config = app.prompts.discord_singleton_lane("arena-relay")
            channel = app.bot.get_channel(target_config["id"])
        except Exception as exc:
            app.log.warning(
                "relay test card failed: arena-relay unavailable: %s",
                exc,
                exc_info=True,
            )
            await send_interaction_text(
                interaction, "Arena relay channel is not configured.", use_followup=True
            )
            return
        if channel is None:
            await send_interaction_text(
                interaction, "Arena relay channel was not found.", use_followup=True
            )
            return
        from runtime.leader_action_ui import post_test_leader_action_card

        try:
            action = await post_test_leader_action_card(
                channel, action_type=action_type, target_name=target_name
            )
        except Exception as exc:
            app.log.warning("relay test card post failed: %s", exc, exc_info=True)
            await send_interaction_text(
                interaction, f"Failed to post test card: {exc}", use_followup=True
            )
            return
        await send_interaction_text(
            interaction,
            f"Posted test {action_type} card as R{action.get('action_id')} in #actions.",
            use_followup=True,
        )

    @member_commands.command(
        name="verify-discord",
        description=COMMAND_SPECS["member.verify-discord"].description,
    )
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

    @member_commands.command(
        name="audit-discord",
        description=COMMAND_SPECS["member.audit-discord"].description,
    )
    async def slash_member_audit_discord(interaction: discord.Interaction):
        await run_admin_interaction(
            interaction,
            command_name="member.audit-discord",
            event_type=COMMAND_SPECS["member.audit-discord"].event_type,
        )

    @member_commands.command(name="set", description=COMMAND_SPECS["member.set"].description)
    @app_commands.describe(
        member="Member name or tag.", field="Field to set.", value="Field value."
    )
    @app_commands.choices(
        field=[
            app_commands.Choice(name="Discord", value="discord"),
            app_commands.Choice(name="Join Date", value="join-date"),
            app_commands.Choice(name="Birthday", value="birthday"),
            app_commands.Choice(name="Profile URL", value="profile-url"),
            app_commands.Choice(name="Note", value="note"),
        ]
    )
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_set(
        interaction: discord.Interaction, member: str, field: str, value: str
    ):
        await run_admin_interaction(
            interaction,
            command_name="member.set",
            args={"member": member, "field": field, "value": value},
            event_type=COMMAND_SPECS["member.set"].event_type,
            write=True,
        )

    @member_commands.command(name="clear", description=COMMAND_SPECS["member.clear"].description)
    @app_commands.describe(member="Member name or tag.", field="Field to clear.")
    @app_commands.choices(
        field=[
            app_commands.Choice(name="Discord", value="discord"),
            app_commands.Choice(name="Join Date", value="join-date"),
            app_commands.Choice(name="Birthday", value="birthday"),
            app_commands.Choice(name="Profile URL", value="profile-url"),
            app_commands.Choice(name="Note", value="note"),
        ]
    )
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_clear(interaction: discord.Interaction, member: str, field: str):
        await run_admin_interaction(
            interaction,
            command_name="member.clear",
            args={"member": member, "field": field},
            event_type=COMMAND_SPECS["member.clear"].event_type,
            write=True,
        )

    ACTIVITY_CHOICES = [
        app_commands.Choice(name=label, value=value) for label, value in manual_activity_choices()
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
    @app_commands.describe(
        activity="Activity to run.",
        preview="Suppress Discord sends and site pushes when supported.",
    )
    @app_commands.choices(activity=ACTIVITY_CHOICES)
    async def slash_activity_run(
        interaction: discord.Interaction, activity: str, preview: bool = False
    ):
        await run_admin_interaction(
            interaction,
            command_name="activity.run",
            args={"activity": activity},
            preview=preview,
            event_type=COMMAND_SPECS["activity.run"].event_type,
            write=True,
        )

    # (The `integration` command group was POAP KINGS website publishing only —
    # removed 2026-06-21 along with the rest of the site-update code.)

    tournament_commands = app_commands.Group(
        name="tournament", description="Clan tournament tracking commands"
    )

    @tournament_commands.command(name="watch", description="Start watching a tournament by tag.")
    @app_commands.describe(tag="Tournament tag (e.g. #2QJJRJPR)")
    async def slash_tournament_watch(interaction: discord.Interaction, tag: str):
        if not await validate_admin_interaction(
            interaction, command_name="tournament.watch", write=True
        ):
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
                f"Already watching **{active.get('name', active['tournament_tag'])}** (`{active['tournament_tag']}`). Use `/clanops tournament stop` first.",
                use_followup=True,
            )
            return

        # Validate tag against API
        api_data = await asyncio.to_thread(cr_api.get_tournament, clean_tag)
        if api_data is None:
            await send_interaction_text(
                interaction, f"Tournament `#{clean_tag}` not found.", use_followup=True
            )
            return

        api_status = api_data.get("status") or ""
        if api_status == "ended":
            await send_interaction_text(
                interaction,
                f"Tournament **{api_data.get('name')}** has already ended.",
                use_followup=True,
            )
            return

        # Register and start watching
        await asyncio.to_thread(db.register_tournament, clean_tag, api_data)
        jobs.start_tournament_watch()

        members = api_data.get("membersList") or []
        game_mode = api_data.get("gameMode") or {}
        status_label = {
            "inPreparation": "In Preparation",
            "inProgress": "In Progress",
        }.get(api_status, api_status)

        # Announce the watch to the clan via the v5 direct-post path
        # (replaces the retired v4 awareness pipeline; mirrors the scheduled
        # tournament-watch job's compose_and_post to #clan-events).
        try:
            from runtime.discord_posting import compose_and_post

            # #clan-events was retired (channel deleted 2026-07-11) — the brain's
            # #elixir is the consolidated public home for this now.
            target_config = app.prompts.discord_singleton_lane("elixir")
            channel = app.bot.get_channel(target_config["id"])
            if channel is None:
                app.log.warning("tournament_watching_started: #elixir channel unavailable")
            else:
                max_capacity = api_data.get("maxCapacity")
                context = (
                    "Elixir just started watching a new tournament for the clan. "
                    "Announce it for #elixir in your own voice — concise and "
                    "inviting; let members know they can jump in. Use only these facts.\n\n"
                    f"Tournament: {api_data.get('name')} (#{clean_tag})\n"
                    f"Status: {status_label}\n"
                    f"Participants: {len(members)}"
                    + (f" / {max_capacity} max" if max_capacity else "")
                    + "\n"
                    f"Game mode: {game_mode.get('name') or game_mode.get('id') or 'Unknown'}\n"
                    f"Type: {api_data.get('type') or 'unknown'}\n"
                    f"Elixir polls for updates every {jobs.TOURNAMENT_POLL_MINUTES} minutes."
                )
                await compose_and_post(channel, lane="elixir", context=context)
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

    @tournament_commands.command(
        name="status", description="Show active tournament tracking status."
    )
    async def slash_tournament_status(interaction: discord.Interaction):
        if not await validate_admin_interaction(
            interaction, command_name="tournament.status", write=False
        ):
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
                await send_interaction_text(
                    interaction, "No tournaments tracked yet.", ephemeral=True
                )
            return

        from db import get_connection

        def _participant_count(tid):
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tournament_participants WHERE tournament_id = ?",
                    (tid,),
                ).fetchone()
                return row["n"] if row else 0
            finally:
                conn.close()

        player_count = await asyncio.to_thread(_participant_count, tournament["tournament_id"])
        max_capacity = tournament.get("max_capacity")
        players_line = f"Players: {player_count}" + (f" / {max_capacity}" if max_capacity else "")

        lines = [
            f"**{tournament.get('name', tournament['tournament_tag'])}** (`{tournament['tournament_tag']}`)",
            f"Status: {tournament['status']}",
            players_line,
            f"Polls: {tournament.get('poll_count', 0)}",
            f"Battles captured: {tournament.get('battles_captured', 0)}",
            f"Last poll: {tournament.get('last_poll_at') or 'never'}",
            f"Watching since: {tournament.get('watching_started_at')}",
        ]
        await send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    @tournament_commands.command(name="stop", description="Stop watching the active tournament.")
    async def slash_tournament_stop(interaction: discord.Interaction):
        if not await validate_admin_interaction(
            interaction, command_name="tournament.stop", write=True
        ):
            return
        from db import _utcnow, get_connection
        from runtime import jobs

        tournament = await asyncio.to_thread(db.get_active_tournament)
        if not tournament:
            await send_interaction_text(
                interaction, "No active tournament to stop.", ephemeral=True
            )
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

    @tournament_commands.command(
        name="recap", description="Generate or regenerate a tournament recap."
    )
    @app_commands.describe(tag="Tournament tag (defaults to most recent)")
    async def slash_tournament_recap(interaction: discord.Interaction, tag: str | None = None):
        if not await validate_admin_interaction(
            interaction, command_name="tournament.recap", write=True
        ):
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
            posted = await _tournament_recap(tournament["tournament_tag"])
            if posted:
                text = f"Recap posted for **{name}**."
            else:
                text = f"Recap was not posted for **{name}**; see incidents/logs."
            await interaction.followup.send(text, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Recap generation failed: {e}", ephemeral=True)

    @tournament_commands.command(name="history", description="List past tournaments.")
    async def slash_tournament_history(interaction: discord.Interaction):
        if not await validate_admin_interaction(
            interaction, command_name="tournament.history", write=False
        ):
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
            status_icon = {
                "ended": "done",
                "in_progress": "live",
                "in_preparation": "prep",
                "cancelled": "cancelled",
                "watching": "watching",
            }.get(t["status"], t["status"])
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

    # ── Member email (self-service, ephemeral) — acts only on the caller's own
    # linked profile; the nickname step is what links a Discord user to a member.
    async def _resolve_caller_tag(interaction: discord.Interaction):
        link = await asyncio.to_thread(db.get_linked_member_for_discord_user, interaction.user.id)
        return (link or {}).get("player_tag")

    _NOT_LINKED = (
        "I can't tell who you are yet — set your Discord nickname to your in-game "
        "name first (that links your account), then try again."
    )

    @email_commands.command(
        name="set",
        description="Add an email to your profile; I'll send a code to verify it.",
    )
    @app_commands.describe(address="The email address to link to your clan profile.")
    async def slash_email_set(interaction: discord.Interaction, address: str):
        await interaction.response.defer(ephemeral=True)
        tag = await _resolve_caller_tag(interaction)
        if not tag:
            await send_interaction_text(interaction, _NOT_LINKED, ephemeral=True)
            return
        from runtime import email_verification

        result = await asyncio.to_thread(email_verification.start_verification, tag, address)
        if result["ok"]:
            await send_interaction_text(
                interaction,
                f"📧 I sent a 6-digit code to **{result['email']}**. Enter it with "
                f"`/elixir email verify <code>` within {email_verification.CODE_TTL_MINUTES} minutes.",
                ephemeral=True,
            )
        else:
            await send_interaction_text(
                interaction, f"Couldn't do that — {result['error']}", ephemeral=True
            )

    @email_commands.command(name="verify", description="Enter the 6-digit code I emailed you.")
    @app_commands.describe(code="The 6-digit code from the email.")
    async def slash_email_verify(interaction: discord.Interaction, code: str):
        await interaction.response.defer(ephemeral=True)
        tag = await _resolve_caller_tag(interaction)
        if not tag:
            await send_interaction_text(interaction, _NOT_LINKED, ephemeral=True)
            return
        from runtime import email_verification

        result = await asyncio.to_thread(email_verification.check_code, tag, code)
        if result["ok"]:
            await send_interaction_text(
                interaction,
                f"✅ Verified — **{result['email']}** is now on your profile.",
                ephemeral=True,
            )
        else:
            await send_interaction_text(
                interaction, f"Couldn't verify — {result['error']}", ephemeral=True
            )

    @email_commands.command(name="show", description="Show the email on your profile.")
    async def slash_email_show(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tag = await _resolve_caller_tag(interaction)
        if not tag:
            await send_interaction_text(interaction, _NOT_LINKED, ephemeral=True)
            return
        identity = await asyncio.to_thread(db.get_member_identity, tag)
        email = (identity or {}).get("email") or ""
        if not email:
            await send_interaction_text(
                interaction,
                "No email on your profile yet. Add one with `/elixir email set <address>`.",
                ephemeral=True,
            )
            return
        status = "✅ verified" if (identity or {}).get("email_verified_at") else "⚠️ not verified"
        await send_interaction_text(interaction, f"📧 **{email}** — {status}", ephemeral=True)

    # ── Admin: view/set a member's email (leader-only), in the identity family.
    @member_commands.command(name="email", description="Show or set a member's email (leader).")
    @app_commands.describe(
        member="Member name or tag.",
        address="Email to set; use 'clear' to remove; omit to just show.",
    )
    @app_commands.autocomplete(member=member_autocomplete)
    async def slash_member_email(
        interaction: discord.Interaction, member: str, address: str | None = None
    ):
        write = address is not None
        if not await validate_admin_interaction(
            interaction, command_name="member.email", write=write
        ):
            return
        if not app._has_leader_role(interaction.user):
            await send_interaction_text(
                interaction, "Leader role required for this command.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        from runtime.admin import _resolve_member_tag, _utcnow

        try:
            tag, label = await asyncio.to_thread(_resolve_member_tag, member)
        except ValueError as exc:
            await send_interaction_text(interaction, str(exc), ephemeral=True)
            return
        if address is not None:
            addr = address.strip()
            if addr.lower() in ("clear", "none", "-", ""):
                await asyncio.to_thread(db.clear_member_email, tag)
                await send_interaction_text(
                    interaction, f"Cleared {label}'s email.", ephemeral=True
                )
                return
            if not db.is_valid_email(addr):
                await send_interaction_text(
                    interaction,
                    f"`{addr}` doesn't look like a valid email.",
                    ephemeral=True,
                )
                return
            await asyncio.to_thread(
                db.set_member_email,
                tag,
                addr,
                source="admin_set",
                verified_at=_utcnow(),
            )
            await send_interaction_text(
                interaction,
                f"Set {label}'s email to **{addr}** (admin-set, trusted).",
                ephemeral=True,
            )
            return
        identity = await asyncio.to_thread(db.get_member_identity, tag)
        email = (identity or {}).get("email") or ""
        if not email:
            await send_interaction_text(
                interaction, f"{label} has no email on file.", ephemeral=True
            )
            return
        status = "verified" if (identity or {}).get("email_verified_at") else "not verified"
        src = (identity or {}).get("email_source") or "?"
        await send_interaction_text(
            interaction,
            f"{label}: **{email}** ({status}, source: {src})",
            ephemeral=True,
        )

    # /elixir = member surface (email + help).
    elixir_commands.add_command(email_commands)
    # /clanops = co-leader surface (all validate_admin_interaction-gated groups).
    clanops_commands.add_command(clan_commands)
    clanops_commands.add_command(member_commands)
    clanops_commands.add_command(relay_commands)
    clanops_commands.add_command(activity_commands)
    clanops_commands.add_command(tournament_commands)

    try:
        if app.APP_GUILD is not None:
            bot.tree.add_command(elixir_commands, guild=app.APP_GUILD)
            bot.tree.add_command(clanops_commands, guild=app.APP_GUILD)
        else:
            bot.tree.add_command(elixir_commands)
            bot.tree.add_command(clanops_commands)
    except app_commands.CommandAlreadyRegistered:
        app.log.info("/elixir + /clanops slash commands already registered")
    except Exception as exc:
        app.log.error("Slash command registration failed: %s", exc)
        raise
