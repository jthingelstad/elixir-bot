async def _heartbeat_tick():
    """Hourly heartbeat — fetch data, detect signals, post if interesting."""
    runtime_status.mark_job_start("heartbeat")
    # Check active hours
    now_chicago = datetime.now(CHICAGO)
    if not (HEARTBEAT_START_HOUR <= now_chicago.hour < HEARTBEAT_END_HOUR):
        log.info("Heartbeat: outside active hours (%d:%02d), skipping",
                 now_chicago.hour, now_chicago.minute)
        runtime_status.mark_job_success("heartbeat", "skipped outside active hours")
        return

    announcements_channel_id = _get_singleton_channel_id("announcements")
    channel = bot.get_channel(announcements_channel_id)
    if not channel:
        log.error("Announcements channel %s not found", announcements_channel_id)
        runtime_status.mark_job_failure("heartbeat", "announcements channel not found")
        return

    try:
        # Run the heartbeat tick — fetches data, snapshots, detects signals
        tick_result = heartbeat.tick()
        signals = tick_result.signals

        if not signals:
            log.info("Heartbeat: no signals, nothing to post")
            runtime_status.mark_job_success("heartbeat", "no signals")
            return

        log.info("Heartbeat: %d signals detected, consulting LLM", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        # Fetch recent announcements-channel post history to avoid repetition
        recent_posts = await asyncio.to_thread(
            db.list_channel_messages, announcements_channel_id, 20, "assistant",
        )
        channel_memory = await asyncio.to_thread(
            db.build_memory_context,
            channel_id=announcements_channel_id,
        )

        # Handle join/leave signals via LLM
        other_signals = []
        for sig in signals:
            if sig["type"] == "member_join":
                msg = await asyncio.to_thread(
                    elixir_agent.generate_message,
                    "member_join_broadcast",
                    f"New member '{sig['name']}' (tag: {sig['tag']}) just joined the clan. "
                    f"Write a welcome announcement for the broadcast channel.",
                    recent_posts,
                )
                if msg:
                    await channel.send(msg)
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_join_broadcast",
                    )
            elif sig["type"] == "member_leave":
                msg = await asyncio.to_thread(
                    elixir_agent.generate_message,
                    "member_leave_broadcast",
                    f"Member '{sig['name']}' (tag: {sig['tag']}) has left the clan. "
                    f"Write a brief farewell for the broadcast channel.",
                    recent_posts,
                )
                if msg:
                    await channel.send(msg)
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", msg,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type="member_leave_broadcast",
                    )
            else:
                other_signals.append(sig)

        # If there are non-join/leave signals, let the LLM craft a post
        if other_signals:
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post, clan, war,
                other_signals, recent_posts, channel_memory,
            )
            if result is None:
                log.info("Heartbeat: LLM decided signals not worth posting")
                runtime_status.mark_job_success("heartbeat", f"{len(other_signals)} signal(s), no post")
                return
            await _post_to_elixir(channel, result)
            content = result.get("content", result.get("summary", ""))
            if content:
                await asyncio.to_thread(
                    db.save_message,
                    _channel_scope(channel), "assistant", content,
                    channel_id=channel.id,
                    channel_name=getattr(channel, "name", None),
                    channel_kind=str(channel.type),
                    workflow="observation",
                    event_type=result.get("event_type"),
                )
            log.info("Posted observation: %s", result.get("summary"))

        runtime_status.mark_job_success("heartbeat", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Heartbeat error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("heartbeat", str(e))


# ── Site content for poapkings.com ────────────────────────────────────────────

SITE_DATA_HOUR = int(os.getenv("SITE_DATA_HOUR", "8"))       # 8am Chicago
SITE_CONTENT_HOUR = int(os.getenv("SITE_CONTENT_HOUR", "20"))  # 8pm Chicago
PLAYER_INTEL_REFRESH_HOURS = int(os.getenv("PLAYER_INTEL_REFRESH_HOURS", "6"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "12"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "6"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))


async def _site_data_refresh():
    """Morning job — refresh clan data and roster on poapkings.com."""
    runtime_status.mark_job_start("site_data_refresh")
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            log.error("Site data refresh: CR API failed")
            clan = {}

        if not clan.get("memberList"):
            log.info("Site data refresh: no member data, skipping")
            runtime_status.mark_job_success("site_data_refresh", "no member data")
            return

        roster_data = site_content.build_roster_data(clan)
        site_content.write_content("roster", roster_data)

        clan_stats = site_content.build_clan_data(clan)
        site_content.write_content("clan", clan_stats)

        site_content.commit_and_push("Elixir data refresh")
        log.info("Site data refresh complete: %d members", len(roster_data.get("members", [])))
        runtime_status.mark_job_success("site_data_refresh", f"{len(roster_data.get('members', []))} members")
    except Exception as e:
        log.error("Site data refresh error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_data_refresh", str(e))


async def _site_content_cycle():
    """Evening job — generate all site content and refresh data."""
    runtime_status.mark_job_start("site_content_cycle")
    try:
        try:
            clan = cr_api.get_clan()
        except Exception:
            clan = {}
        try:
            war = cr_api.get_current_war()
        except Exception:
            war = {}

        # Build and write data (second daily refresh)
        roster_data = None
        if clan.get("memberList"):
            roster_data = site_content.build_roster_data(clan, include_cards=True)
            clan_stats = site_content.build_clan_data(clan)

            # Generate roster bios and merge
            try:
                bios = elixir_agent.generate_roster_bios(clan, war, roster_data=roster_data)
                if bios:
                    roster_data["intro"] = bios.get("intro", "")
                    member_bios = bios.get("members", {})
                    for m in roster_data["members"]:
                        mc = member_bios.get(m["tag"], {}) or member_bios.get("#" + m["tag"], {})
                        if mc:
                            m["bio"] = mc.get("bio", "")
                            m["highlight"] = mc.get("highlight", "general")
            except Exception as e:
                log.error("Roster bio generation error: %s", e)

            site_content.write_content("roster", roster_data)
            site_content.write_content("clan", clan_stats)

        # Generate home message
        try:
            prev_home = site_content.load_current("home")
            prev_msg = prev_home.get("message", "") if prev_home else ""
            home_text = elixir_agent.generate_home_message(clan, war, prev_msg, roster_data=roster_data)
            if home_text:
                site_content.write_content("home", {
                    "message": home_text,
                    "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
        except Exception as e:
            log.error("Home message error: %s", e)

        # Generate members message
        try:
            prev_members = site_content.load_current("members")
            prev_msg = prev_members.get("message", "") if prev_members else ""
            members_text = elixir_agent.generate_members_message(clan, war, prev_msg, roster_data=roster_data)
            if members_text:
                site_content.write_content("members", {
                    "message": members_text,
                    "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
        except Exception as e:
            log.error("Members message error: %s", e)

        # Generate promote content on Sundays
        now_chicago = datetime.now(CHICAGO)
        if now_chicago.weekday() == 6:  # Sunday
            try:
                promote = elixir_agent.generate_promote_content(clan, roster_data=roster_data)
                if promote:
                    site_content.write_content("promote", promote)
            except Exception as e:
                log.error("Promote content error: %s", e)

        site_content.commit_and_push("Elixir content update")
        log.info("Site content cycle complete")
        runtime_status.mark_job_success("site_content_cycle", "content updated")
    except Exception as e:
        log.error("Site content cycle error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("site_content_cycle", str(e))


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        runtime_status.mark_job_failure("player_intel_refresh", f"clan fetch failed: {e}")
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        runtime_status.mark_job_success("player_intel_refresh", "no member data")
        return

    await asyncio.to_thread(db.snapshot_members, members)
    try:
        war = await asyncio.to_thread(cr_api.get_current_war)
        if war:
            await asyncio.to_thread(db.upsert_war_current_state, war)
    except Exception:
        war = {}

    targets = await asyncio.to_thread(
        db.get_player_intel_refresh_targets,
        PLAYER_INTEL_BATCH_SIZE,
        PLAYER_INTEL_STALE_HOURS,
    )
    if not targets:
        log.info("Player intel refresh: no stale targets")
        runtime_status.mark_job_success("player_intel_refresh", "no stale targets")
        return

    refreshed = 0
    progression_signals = []
    for target in targets:
        tag = target["tag"]
        try:
            profile = await asyncio.to_thread(cr_api.get_player, tag)
            if profile:
                profile_signals = await asyncio.to_thread(db.snapshot_player_profile, profile)
                if profile_signals:
                    progression_signals.extend(profile_signals)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log:
                await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
            refreshed += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    if progression_signals:
        announcements_channel_id = _get_singleton_channel_id("announcements")
        channel = bot.get_channel(announcements_channel_id)
        if channel:
            recent_posts = await asyncio.to_thread(
                db.list_channel_messages, announcements_channel_id, 20, "assistant",
            )
            result = await asyncio.to_thread(
                elixir_agent.observe_and_post,
                clan,
                war,
                progression_signals,
                recent_posts,
                await asyncio.to_thread(
                    db.build_memory_context,
                    channel_id=announcements_channel_id,
                ),
            )
            if result:
                await _post_to_elixir(channel, result)
                content = result.get("content", result.get("summary", ""))
                if content:
                    await asyncio.to_thread(
                        db.save_message,
                        _channel_scope(channel), "assistant", content,
                        channel_id=channel.id,
                        channel_name=getattr(channel, "name", None),
                        channel_kind=str(channel.type),
                        workflow="observation",
                        event_type=result.get("event_type"),
                    )

    log.info("Player intel refresh complete: refreshed %d members", refreshed)
    runtime_status.mark_job_success("player_intel_refresh", f"refreshed {refreshed} member(s)")


async def _clanops_weekly_review():
    runtime_status.mark_job_start("clanops_weekly_review")
    clanops_channels = prompts.discord_channels_by_role("clanops")
    if not clanops_channels:
        runtime_status.mark_job_failure("clanops_weekly_review", "no clanops channel configured")
        return

    target_config = clanops_channels[0]
    channel = bot.get_channel(target_config["id"])
    if not channel:
        runtime_status.mark_job_failure("clanops_weekly_review", "clanops channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("ClanOps weekly review refresh failed: %s", exc)

    review_content = await asyncio.to_thread(_build_weekly_clanops_review, clan, war)
    if not review_content:
        runtime_status.mark_job_success("clanops_weekly_review", "no review content")
        return

    await _post_to_elixir(channel, {"content": review_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        review_content,
        channel_id=channel.id,
        channel_name=getattr(channel, "name", None),
        channel_kind=str(channel.type),
        workflow="clanops",
        event_type="weekly_clanops_review",
    )
    runtime_status.mark_job_success("clanops_weekly_review", "weekly review posted")


# ── Bot events ────────────────────────────────────────────────────────────────
