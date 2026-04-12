"""Discord onboarding and member verification helpers."""

from __future__ import annotations

import asyncio
import re

import discord

import cr_api
import db
import elixir_agent

_DISCORD_REF_RE = re.compile(r"^<@!?(\d+)>$")


def _candidate_display_values(member: discord.Member) -> list[str]:
    values = []
    for value in (
        getattr(member, "nick", None),
        getattr(member, "display_name", None),
        getattr(member, "global_name", None),
        getattr(member, "name", None),
    ):
        text = (value or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _find_unique_guild_member_for_clan_member(guild: discord.Guild, clan_name: str):
    normalized = (clan_name or "").strip().lower()
    if not normalized:
        return None

    matches = []
    for member in getattr(guild, "members", []) or []:
        for candidate in _candidate_display_values(member):
            if candidate.lower() == normalized:
                matches.append(member)
                break

    unique = list({member.id: member for member in matches}.values())
    if len(unique) == 1:
        return unique[0]
    return None


def _find_unique_guild_member_for_discord_ref(guild: discord.Guild, discord_ref: str):
    ref = (discord_ref or "").strip()
    if not ref:
        return None

    mention_match = _DISCORD_REF_RE.match(ref)
    if mention_match:
        return guild.get_member(int(mention_match.group(1)))

    if ref.isdigit():
        return guild.get_member(int(ref))

    normalized = ref.lstrip("@").strip().lower()
    if not normalized:
        return None

    matches = []
    for member in getattr(guild, "members", []) or []:
        candidates = []
        for value in (
            getattr(member, "name", None),
            getattr(member, "display_name", None),
            getattr(member, "global_name", None),
            getattr(member, "nick", None),
        ):
            text = (value or "").strip()
            if text:
                candidates.append(text)
                candidates.append(text.lstrip("@"))
        if any(candidate.lower() == normalized for candidate in candidates):
            matches.append(member)

    unique = list({member.id: member for member in matches}.values())
    if len(unique) == 1:
        return unique[0]
    return None


async def resolve_discord_member_input(discord_ref: str):
    import runtime.app as app

    guild = app.bot.get_guild(app.GUILD_ID) if app.GUILD_ID else None
    if guild is None:
        return None
    return _find_unique_guild_member_for_discord_ref(guild, discord_ref)


async def _onboarding_channel():
    import runtime.app as app

    channel_id = app._get_singleton_channel_id("onboarding")
    return app.bot.get_channel(channel_id)


async def _send_onboarding_message(event_type: str, prompt_text: str, fallback: str):
    channel = await _onboarding_channel()
    if not channel:
        return
    import runtime.app as app

    msg = await asyncio.to_thread(elixir_agent.generate_message, event_type, prompt_text)
    await app._post_to_elixir(channel, {"content": msg or fallback})


async def refresh_clan_roster_from_clan_data(clan_data: dict | None, *, reason: str = "") -> bool:
    member_list = (clan_data or {}).get("memberList") or []
    if not member_list:
        return False
    try:
        await asyncio.to_thread(db.snapshot_members, member_list)
        return True
    except Exception as exc:
        import runtime.app as app

        app.log.warning("Onboarding roster refresh failed during %s: %s", reason or "unknown", exc)
        return False


async def refresh_clan_roster_from_api(*, reason: str = "") -> bool:
    import runtime.app as app

    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
    except Exception as exc:
        app.log.warning("Onboarding clan fetch failed during %s: %s", reason or "unknown", exc)
        return False
    return await refresh_clan_roster_from_clan_data(clan, reason=reason)


async def _ensure_member_role(discord_member: discord.Member, member_tag: str, cr_name: str) -> tuple[bool, str]:
    import runtime.app as app

    if not app.MEMBER_ROLE_ID:
        return False, "Member role is not configured."

    role_status = app._member_role_grant_status()
    if not role_status["ok"]:
        return False, f"Member role auto-grant unavailable: {role_status['reason']}."

    member_role = discord_member.guild.get_role(app.MEMBER_ROLE_ID)
    if not member_role:
        return False, "Configured Member role was not found in the guild."
    if member_role in discord_member.roles:
        return True, f"{cr_name} already has the Member role."

    try:
        await discord_member.add_roles(member_role, reason=f"Matched clan member: {cr_name} ({member_tag})")
        return True, f"Granted the Member role to {cr_name}."
    except discord.Forbidden:
        app.log.error("Cannot assign member role to %s — check bot permissions and role hierarchy", discord_member.id)
        return False, "Couldn't assign the Member role due to Discord permissions."


async def remove_member_role_for_tag(member_tag: str, *, reason: str) -> tuple[bool, str]:
    """Remove the Member role from the Discord user linked to a clan member.

    Called when a clan member leaves (kicked or quit). Keeps the discord_links
    row intact so a rejoin recognises the same person.
    """
    import runtime.app as app

    if not app.MEMBER_ROLE_ID:
        return False, "Member role is not configured."

    role_status = app._member_role_grant_status()
    if not role_status["ok"]:
        return False, f"Member role management unavailable: {role_status['reason']}."

    identity = await asyncio.to_thread(db.get_member_identity, member_tag)
    discord_user_id = identity.get("discord_user_id") if identity else None
    if not discord_user_id:
        return False, f"No linked Discord user for {member_tag}."

    guild = app.bot.get_guild(app.GUILD_ID) if app.GUILD_ID else None
    if guild is None:
        return False, "Guild not cached in the running bot."

    guild_member = guild.get_member(int(discord_user_id))
    if guild_member is None:
        return False, f"Discord user {discord_user_id} not in guild."

    member_role = guild.get_role(app.MEMBER_ROLE_ID)
    if not member_role:
        return False, "Configured Member role was not found in the guild."
    if member_role not in guild_member.roles:
        return True, f"{guild_member.display_name} did not have the Member role."

    try:
        await guild_member.remove_roles(member_role, reason=reason)
        return True, f"Removed Member role from {guild_member.display_name}."
    except discord.Forbidden:
        app.log.error("Cannot remove member role from %s — check bot permissions", guild_member.id)
        return False, "Couldn't remove the Member role due to Discord permissions."


async def handle_member_join(member: discord.Member):
    await asyncio.to_thread(
        db.upsert_discord_user,
        member.id,
        username=member.name,
        global_name=getattr(member, "global_name", None),
        display_name=member.display_name,
    )
    await refresh_clan_roster_from_api(reason="discord_member_join")
    await _send_onboarding_message(
        "discord_member_join",
        (
            f"A new user '{member.display_name}' ({member.mention}) just joined the Discord server. "
            f"Welcome them in #reception and explain how to set their server nickname "
            f"to match their Clash Royale in-game name to get verified."
        ),
        (
            f"Welcome to the server, {member.mention}! Set your server nickname "
            f"to your Clash Royale name and I'll get you verified."
        ),
    )


async def handle_member_update(before: discord.Member, after: discord.Member):
    import runtime.app as app

    if before.nick == after.nick or not after.nick:
        return

    await asyncio.to_thread(
        db.upsert_discord_user,
        after.id,
        username=after.name,
        global_name=getattr(after, "global_name", None),
        display_name=after.display_name,
    )

    if not app.MEMBER_ROLE_ID:
        return
    member_role = after.guild.get_role(app.MEMBER_ROLE_ID)
    if not member_role or member_role in after.roles:
        return

    match = await asyncio.to_thread(app._match_clan_member, after.nick)
    if not match:
        await refresh_clan_roster_from_api(reason="nickname_update_no_match")
        match = await asyncio.to_thread(app._match_clan_member, after.nick)
    if not match:
        await _send_onboarding_message(
            "nickname_no_match",
            (
                f"User {after.mention} set their nickname to '{after.nick}' but it doesn't "
                f"match anyone in the clan roster. Let them know and suggest they check "
                f"the spelling or join the clan first. Channel: #reception."
            ),
            f"Hmm {after.mention}, I don't see **{after.nick}** in our roster.",
        )
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

    granted, message = await _ensure_member_role(after, tag, cr_name)
    if not granted:
        await _send_onboarding_message(
            "role_grant_failed",
            (
                f"Matched user {after.mention} to clan member '{cr_name}' ({tag}) but "
                f"couldn't assign the member role due to permissions. Let them know "
                f"a leader will help. Channel: #reception."
            ),
            message,
        )
        return

    await _send_onboarding_message(
        "nickname_matched",
        (
            f"User {after.mention} set their nickname to '{cr_name}' which matches "
            f"clan member tag {tag}. They've been granted the member role. "
            f"Welcome them and let them know they have full access. Channel: #reception."
        ),
        f"Welcome aboard, {cr_name}! You now have full access.",
    )


async def verify_discord_membership(member_tag: str) -> str:
    import runtime.app as app

    identity = await asyncio.to_thread(db.get_member_identity, member_tag)
    if not identity:
        raise ValueError(f"No clan member found for {member_tag}.")

    label = await asyncio.to_thread(db.format_member_reference, member_tag)
    guild = app.bot.get_guild(app.GUILD_ID) if app.GUILD_ID else None
    if guild is None:
        return "Guild is not cached in the running bot right now."

    discord_user_id = identity.get("discord_user_id")
    guild_member = None

    if discord_user_id and str(discord_user_id).isdigit():
        guild_member = guild.get_member(int(discord_user_id))
        if guild_member is None:
            try:
                guild_member = await guild.fetch_member(int(discord_user_id))
            except Exception:
                app.log.warning(
                    "onboarding guild.fetch_member failed discord_user_id=%s member_tag=%s",
                    discord_user_id, member_tag, exc_info=True,
                )
                guild_member = None

    if guild_member is None:
        guild_member = _find_unique_guild_member_for_clan_member(guild, identity.get("member_name") or "")
        if guild_member is not None:
            await asyncio.to_thread(
                db.link_discord_user_to_member,
                guild_member.id,
                member_tag,
                username=guild_member.name,
                display_name=guild_member.display_name,
                source="manual_verify_discord",
            )
            identity = await asyncio.to_thread(db.get_member_identity, member_tag)

    if guild_member is None:
        return (
            f"I couldn't find a unique Discord user for {label}. "
            "Ask them to set their server nickname to match their Clash name or send a message in Discord first."
        )

    await asyncio.to_thread(
        db.upsert_discord_user,
        guild_member.id,
        username=guild_member.name,
        global_name=getattr(guild_member, "global_name", None),
        display_name=guild_member.display_name,
    )
    await asyncio.to_thread(
        db.link_discord_user_to_member,
        guild_member.id,
        member_tag,
        username=guild_member.name,
        display_name=guild_member.display_name,
        source="manual_verify_discord",
    )

    granted, role_message = await _ensure_member_role(
        guild_member,
        member_tag,
        identity.get("member_name") or member_tag,
    )
    linked_label = await asyncio.to_thread(db.format_member_reference, member_tag)
    if not granted:
        return f"Verified Discord identity for {linked_label}, but {role_message}"
    return f"Verified Discord identity for {linked_label}. {role_message}"


__all__ = [
    "handle_member_join",
    "handle_member_update",
    "refresh_clan_roster_from_api",
    "refresh_clan_roster_from_clan_data",
    "resolve_discord_member_input",
    "verify_discord_membership",
]
