"""Discord message routing for Elixir channel traffic."""

from __future__ import annotations

import asyncio

import cr_api
import db
import elixir_agent


async def route_message(message):
    import runtime.app as app

    if message.author.bot:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        message.author.id,
        username=message.author.name,
        global_name=getattr(message.author, "global_name", None),
        display_name=message.author.display_name,
    )
    channel_config = app._get_channel_behavior(message.channel.id)
    mentioned = app._is_bot_mentioned(message)

    if not channel_config:
        await app.bot.process_commands(message)
        return

    role = channel_config.get("role")
    workflow = channel_config.get("workflow", "interactive")
    scope = app._channel_scope(message.channel)
    conversation_scope = app._channel_conversation_scope(message.channel, message.author.id)
    raw_question = app._strip_bot_mentions(message.content).strip() if mentioned else message.content.strip()

    if role == "clanops" and app._is_legacy_clanops_command_text(raw_question):
        if not mentioned or not app.parse_admin_command(raw_question, require_prefix=True):
            hint_content = app._build_clanops_command_hint()
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
            await app._reply_text(message, hint_content)
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

    if role in {"clanops", "interactive"} and app._is_roster_join_dates_request(raw_question):
        app.log.info(
            "message_route route=roster_join_dates_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        roster_content = await asyncio.to_thread(app._build_roster_join_dates_report)
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
        await app._reply_text(message, roster_content)
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

    if role == "interactive" and app._is_help_request(raw_question):
        app.log.info(
            "message_route route=interactive_help channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        help_content = await asyncio.to_thread(app._build_help_report, "interactive")
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
            workflow="interactive",
            discord_message_id=message.id,
        )
        await app._reply_text(message, help_content)
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
            workflow="interactive",
            event_type="interactive_help",
        )
        return

    deck_target = None
    if role in {"clanops", "interactive"} and app._is_member_deck_request(raw_question):
        deck_target = await asyncio.to_thread(app._extract_member_deck_target, raw_question, message)
    if role in {"clanops", "interactive"} and deck_target:
        app.log.info(
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
        deck_content = await asyncio.to_thread(app._build_member_deck_report, deck_target)
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
        await app._reply_text(message, deck_content)
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

    if role == "clanops" and app._is_kick_risk_request(raw_question):
        app.log.info(
            "message_route route=kick_risk_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        kick_risk_content = await asyncio.to_thread(app._build_kick_risk_report)
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
        await app._reply_text(message, kick_risk_content)
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

    if role == "clanops" and app._is_top_war_contributors_request(raw_question):
        app.log.info(
            "message_route route=top_war_contributors_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        top_war_content = await asyncio.to_thread(app._build_top_war_contributors_report)
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
        await app._reply_text(message, top_war_content)
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

    profile_target = await asyncio.to_thread(app._extract_profile_target, raw_question) if role == "clanops" else None
    if role == "clanops" and (app._is_clan_list_request(raw_question) or profile_target):
        route = "clan_list_report" if app._is_clan_list_request(raw_question) else "member_profile_report"
        app.log.info(
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
        admin_content = await app.dispatch_admin_command(
            "clan-list" if app._is_clan_list_request(raw_question) else "profile",
            preview=False,
            short=False,
            args={} if app._is_clan_list_request(raw_question) else {"member": profile_target},
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
        await app._reply_text(message, admin_content)
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

    admin_command = app.parse_admin_command(raw_question, require_prefix=True) if role == "clanops" and mentioned else None
    if admin_command:
        if app.admin_command_requires_leader(admin_command["command"]) and not app._has_leader_role(message.author):
            denial = "Leader role required for this command."
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
            await app._reply_text(message, denial)
            await asyncio.to_thread(
                db.save_message,
                conversation_scope,
                "assistant",
                denial,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                channel_kind=str(message.channel.type),
                discord_user_id=message.author.id,
                username=message.author.name,
                display_name=message.author.display_name,
                workflow="clanops",
                event_type="clanops_admin_denied",
            )
            return
        route = f"clanops_admin_{admin_command['command'].replace('-', '_')}"
        if admin_command.get("preview"):
            route += "_preview"
        app.log.info(
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
        admin_content = await app.dispatch_admin_command(
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
        await app._reply_text(message, admin_content)
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

    clan_status_mode = app._clan_status_mode(raw_question) if role == "clanops" else None
    if role == "clanops" and (app._is_status_request(raw_question) or app._is_schedule_request(raw_question) or clan_status_mode):
        route = (
            "clan_status_report" if clan_status_mode == "full"
            else "clan_status_short_report" if clan_status_mode == "short"
            else "schedule_report" if app._is_schedule_request(raw_question)
            else "status_report"
        )
        app.log.info(
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
                clan, war = await app._load_live_clan_context()
            except Exception as exc:
                app.log.warning("Clan status refresh failed: %s", exc)
        if clan_status_mode == "full":
            report_builder = app._build_clan_status_report
            report_args = (clan, war)
            event_type = "clan_status_report"
        elif clan_status_mode == "short":
            report_builder = app._build_clan_status_short_report
            report_args = (clan, war)
            event_type = "clan_status_short_report"
        elif app._is_schedule_request(raw_question):
            report_builder = app._build_schedule_report
            report_args = ()
            event_type = "schedule_report"
        else:
            report_builder = app._build_status_report
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
        await app._reply_text(message, status_content)
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
    if proactive and not app._clanops_cooldown_elapsed(message.channel.id):
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
        await app.bot.process_commands(message)
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
                    app._log_prompt_failure(
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
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=app._preview_text(result),
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                content = result.get("content", result.get("summary", ""))
                if not content:
                    app._log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="empty_result",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=app._preview_text(result),
                        raw_json=result,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                await app._reply_text(message, content)
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
                app.log.error("reception error: %s", e)
                app._log_prompt_failure(
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
        app.log.info(
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
                clan, war = await app._load_live_clan_context()
                question = raw_question
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    conversation_scope,
                    app.CHANNEL_CONVERSATION_LIMIT,
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
                    channel_name=app._channel_reply_target_name(channel_config),
                    workflow=workflow,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                    proactive=proactive,
                )
                if result is None:
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="agent_none",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    if mentioned:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return
                if not isinstance(result, dict):
                    app.log.error("%s channel error: invalid result type %s", workflow, type(result).__name__)
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=app._preview_text(result),
                    )
                    if mentioned:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return

                content = result.get("content", result.get("summary", ""))
                if not content:
                    app.log.error("%s channel error: empty result payload %s", workflow, result)
                    app._log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="empty_result",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=app._preview_text(result),
                        raw_json=result,
                    )
                    if mentioned:
                        await message.reply(app._fallback_channel_response(raw_question, workflow))
                    return
                await app._share_channel_result(result, workflow)

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

                await app._reply_text(message, content)
            except Exception as e:
                app.log.error("%s channel error: %s", workflow, e)
                app._log_prompt_failure(
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

    await app.bot.process_commands(message)


__all__ = ["route_message"]
