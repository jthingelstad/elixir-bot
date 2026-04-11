import re

import db

__all__ = [
    "_pick_resolved_member", "_rewrite_member_refs_in_text",
    "_apply_member_refs_to_result", "_match_clan_member",
    "_resolve_member_candidate", "_extract_member_deck_target",
    "_build_member_deck_report",
]

from runtime.helpers._common import (
    _bot,
    _chicago,
    _fmt_iso_short,
    _runtime_app,
)


def _pick_resolved_member(matches):
    if not matches:
        return None
    exactish = [item for item in matches if item.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0]
    if len(matches) == 1:
        return matches[0]
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top
    return None


def _rewrite_member_refs_in_text(text: str, replacements: list[tuple[str, str]]) -> str:
    updated = text or ""
    if not updated:
        return updated
    for alias, ref in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if not alias or not ref or ref == alias:
            continue
        pattern = re.compile(
            rf"(?<![\w<]){re.escape(alias)}(?![\w>])(?!\s*\((?:<@|@))",
            re.IGNORECASE,
        )
        updated = pattern.sub(ref, updated)
    return updated


async def _apply_member_refs_to_result(result: dict | None):
    # Mention injection disabled — return result unchanged.
    # Data (discord links, identities) is preserved; we just no longer
    # rewrite player names into Discord <@id> pings in bot output.
    return result

def _match_clan_member(nickname):
    """Match a Discord nickname to a clan member. Returns (tag, name) or None.

    Uses Elixir's member resolver but only accepts high-confidence exact matches.
    """
    normalized = (nickname or "").lower().strip()
    if not normalized:
        return None

    try:
        matches = db.resolve_member(nickname, limit=2)
        if matches:
            best = matches[0]
            if best.get("match_source") in {"player_tag_exact", "current_name_exact", "alias_exact"}:
                if len(matches) == 1 or matches[0].get("match_score") != matches[1].get("match_score"):
                    return (best["player_tag"], best.get("current_name") or best.get("member_name"))
            return None
    except Exception:
        pass

    try:
        snapshot = db.get_active_roster_map()
        for tag, name in snapshot.items():
            if name.lower().strip() == normalized:
                return (tag, name)
    except Exception:
        return None
    return None


def _resolve_member_candidate(query: str):
    matches = db.resolve_member(query, limit=3)
    if not matches:
        return None, f"I couldn't find a clan member matching {query}."
    exactish = [item for item in matches if item.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0], None
    if len(matches) == 1:
        return matches[0], None
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top, None
    choices = ", ".join(
        item.get("member_ref_with_handle") or item.get("current_name") or item["player_tag"]
        for item in matches[:3]
    )
    return None, f"I couldn't tell which member you meant. Top matches: {choices}"


def _extract_member_deck_target(text: str, message):
    normalized = " ".join((text or "").strip().lower().split())
    if "my deck" in normalized:
        linked = db.get_linked_member_for_discord_user(message.author.id)
        if linked:
            return linked["player_tag"]
    mentioned_users = [
        user for user in getattr(message, "mentions", [])
        if getattr(user, "id", None) != getattr(getattr(_bot(), "user", None), "id", None)
    ]
    if len(mentioned_users) == 1:
        linked = db.get_linked_member_for_discord_user(mentioned_users[0].id)
        if linked:
            return linked["player_tag"]
        for candidate in (
            getattr(mentioned_users[0], "display_name", None),
            getattr(mentioned_users[0], "global_name", None),
            getattr(mentioned_users[0], "name", None),
        ):
            if candidate:
                return candidate
    handles = re.findall(r"(?<!\S)@([A-Za-z0-9_.-]{2,32})", text or "")
    if handles:
        return f"@{handles[0]}"
    return None


def _build_member_deck_report(member_query: str):
    member, error = _resolve_member_candidate(member_query)
    if error:
        return error
    deck = db.get_member_current_deck(member["player_tag"])
    label = member.get("member_ref_with_handle") or member.get("member_ref") or member.get("current_name") or member["player_tag"]
    if not deck or not deck.get("cards"):
        return f"I don't have a stored current deck yet for {label}."
    lines = [f"**Current Deck for {label}**"]
    has_mode_data = False
    for card in deck.get("cards") or []:
        card_name = card.get("name") or "Unknown Card"
        card_level = card.get("level")
        mode_status_label = card.get("mode_status_label")
        if card.get("supports_evo") or card.get("supports_hero"):
            has_mode_data = True
        suffix = f" ({mode_status_label})" if mode_status_label else ""
        if card_level is None:
            lines.append(f"- {card_name}{suffix}")
        else:
            lines.append(f"- {card_name} — Level {card_level}{suffix}")
    if has_mode_data:
        lines.append(
            "_Activation depends on deck slot; these labels show what the card supports or has unlocked._"
        )
    if deck.get("fetched_at"):
        lines.append(f"_Snapshot: {_fmt_iso_short(deck['fetched_at'])}_")
    return "\n".join(lines)
