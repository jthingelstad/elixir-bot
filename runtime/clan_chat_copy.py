"""Clash Royale in-game clan chat copy generation and guardrails."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import elixir_agent

CLAN_CHAT_DEFAULT_MAX_CHARS = 240
CLAN_CHAT_WELCOME_MAX_CHARS = 120
DISCORD_INVITE_ROUTE = "POAPKINGS . COM > Members"

ROLE_ACTION_TYPES = {
    "promotion_recommendation",
    "demotion_recommendation",
    "kick_recommendation",
}

_RAW_LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_DISCORD_MENTION_RE = re.compile(r"<[@#!&][^>]+>|@(everyone|here)\b", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_MESSAGE_LABEL_RE = re.compile(r"^\s*(?:copy|message)\s*\d*\s*:", re.IGNORECASE)
_NUMBERED_LABEL_RE = re.compile(r"^\s*\d+[.)]\s+")


@dataclass(frozen=True)
class ClanChatCopyResult:
    messages: list[str]
    summary: str = ""
    violations: list[str] = field(default_factory=list)
    used_fallback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


def clip_clan_chat_text(text: str, *, limit: int = CLAN_CHAT_DEFAULT_MAX_CHARS) -> str:
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    clipped = body[: max(0, limit - 3)]
    word_boundary = clipped.rfind(" ")
    if word_boundary > 0:
        clipped = clipped[:word_boundary]
    return clipped.rstrip(" .,;:") + "..."


def _content_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def messages_from_agent_result(result: dict | None) -> list[str]:
    if not isinstance(result, dict):
        return []
    messages = _content_items(result.get("messages"))
    if messages:
        return messages
    return _content_items(result.get("content"))


def _has_discord_markdown(message: str) -> bool:
    return (
        "```" in message
        or "`" in message
        or "**" in message
        or "__" in message
        or bool(_MARKDOWN_LINK_RE.search(message))
    )


def validate_clan_chat_messages(
    messages: list[str],
    *,
    max_messages: int = 1,
    max_chars: int = CLAN_CHAT_DEFAULT_MAX_CHARS,
    required_terms: tuple[str, ...] = (),
    exact_once_terms: tuple[str, ...] = (),
    forbidden_terms: tuple[str, ...] = (),
) -> ClanChatCopyResult:
    clean_messages = [
        clip_clan_chat_text(str(message or ""), limit=max_chars)
        for message in messages[: max(1, int(max_messages or 1))]
        if str(message or "").strip()
    ]
    violations: list[str] = []
    if not clean_messages:
        violations.append("empty")
    combined = "\n".join(clean_messages)
    combined_lower = combined.lower()

    for idx, message in enumerate(clean_messages, 1):
        if len(message) > max_chars:
            violations.append(f"message_{idx}_too_long")
        if _RAW_LINK_RE.search(message):
            violations.append(f"message_{idx}_raw_link")
        if _DISCORD_MENTION_RE.search(message):
            violations.append(f"message_{idx}_discord_mention")
        if _has_discord_markdown(message):
            violations.append(f"message_{idx}_discord_markdown")
        if _MESSAGE_LABEL_RE.search(message) or _NUMBERED_LABEL_RE.search(message):
            violations.append(f"message_{idx}_label")

    for term in required_terms:
        if term and term not in combined:
            violations.append(f"missing_required:{term}")
    for term in exact_once_terms:
        if term and combined.count(term) != 1:
            violations.append(f"not_exactly_once:{term}")
    for term in forbidden_terms:
        if term and term.lower() in combined_lower:
            violations.append(f"forbidden:{term}")

    return ClanChatCopyResult(messages=clean_messages, violations=violations)


def _valid_or_none(
    messages: list[str],
    *,
    max_messages: int,
    max_chars: int,
    required_terms: tuple[str, ...],
    exact_once_terms: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
    summary: str = "",
    used_fallback: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ClanChatCopyResult | None:
    result = validate_clan_chat_messages(
        messages,
        max_messages=max_messages,
        max_chars=max_chars,
        required_terms=required_terms,
        exact_once_terms=exact_once_terms,
        forbidden_terms=forbidden_terms,
    )
    if result.violations:
        return None
    return ClanChatCopyResult(
        messages=result.messages,
        summary=summary,
        used_fallback=used_fallback,
        metadata=dict(metadata or {}),
    )


async def generate_clan_chat_copy(
    *,
    intent: str,
    context: str,
    max_messages: int = 1,
    max_chars: int = CLAN_CHAT_DEFAULT_MAX_CHARS,
    required_terms: tuple[str, ...] = (),
    exact_once_terms: tuple[str, ...] = (),
    forbidden_terms: tuple[str, ...] = (),
    fallback_messages: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    signature: dict[str, Any] | None = None,
) -> ClanChatCopyResult | None:
    """Generate and validate copy for Clash Royale in-game clan chat."""
    request = {
        "intent": intent,
        "target_surface": "Clash Royale in-game clan chat",
        "persona": "Elixir in-game relay persona",
        "context": context,
        "max_messages": max_messages,
        "max_chars_per_message": max_chars,
        "required_terms": list(required_terms),
        "exact_once_terms": list(exact_once_terms),
        "forbidden_terms": list(forbidden_terms),
        "signature": signature or {"enabled": False},
        "metadata": metadata or {},
    }
    generated = await asyncio.to_thread(elixir_agent.generate_clan_chat_copy, request)
    messages = messages_from_agent_result(generated)
    summary = str((generated or {}).get("summary") or "") if isinstance(generated, dict) else ""
    result = _valid_or_none(
        messages,
        max_messages=max_messages,
        max_chars=max_chars,
        required_terms=required_terms,
        exact_once_terms=exact_once_terms,
        forbidden_terms=forbidden_terms,
        summary=summary,
        metadata=metadata,
    )
    if result is not None:
        return result
    if fallback_messages:
        return _valid_or_none(
            fallback_messages,
            max_messages=max_messages,
            max_chars=max_chars,
            required_terms=required_terms,
            exact_once_terms=exact_once_terms,
            forbidden_terms=forbidden_terms,
            summary="fallback clan chat copy",
            used_fallback=True,
            metadata=metadata,
        )
    return None


def _clan_chat_action_reason(rationale: str) -> str:
    reason = " ".join((rationale or "").split()).rstrip(".")
    reason = re.sub(r"\s*\([^)]*\)", "", reason)
    parts = [part.strip(" .,;:") for part in reason.split(";") if part.strip(" .,;:")]
    if parts:
        selected: list[str] = []
        for part in parts:
            candidate = "; ".join([*selected, part])
            if len(candidate) <= 90:
                selected.append(part)
                continue
            if not selected:
                selected.append(clip_clan_chat_text(part, limit=90).removesuffix("...").rstrip(" .,;:"))
            break
        if selected:
            return "; ".join(selected)
    return clip_clan_chat_text(reason, limit=90).removesuffix("...").rstrip(" .,;:")


def role_action_clan_chat_copy(
    *,
    action_type: str,
    target_player_name: str | None,
    rationale: str,
    max_chars: int = CLAN_CHAT_DEFAULT_MAX_CHARS,
) -> str | None:
    """Deterministic fallback for role-action transparency messages."""
    if action_type not in ROLE_ACTION_TYPES:
        return None
    name = " ".join((target_player_name or "this member").split()) or "this member"
    reason = _clan_chat_action_reason(rationale)
    if action_type == "promotion_recommendation":
        text = (
            f"Promoting {name} to Elder for {reason}. Thanks for helping POAP KINGS."
            if reason
            else f"Promoting {name} to Elder. Thanks for helping POAP KINGS."
        )
    elif action_type == "demotion_recommendation":
        text = (
            f"Moving {name} back to Member for now: {reason}. Roles should match current activity."
            if reason
            else f"Moving {name} back to Member for now. Roles should match current activity."
        )
    else:
        text = (
            f"Removing {name} from the clan for {reason}. Keeping POAP KINGS active and fair."
            if reason
            else f"Removing {name} from the clan. Keeping POAP KINGS active and fair."
        )
    result = validate_clan_chat_messages(
        [text],
        max_messages=1,
        max_chars=max_chars,
        required_terms=(),
        exact_once_terms=(),
        forbidden_terms=(),
    )
    return result.messages[0] if result.messages and not result.violations else None


__all__ = [
    "CLAN_CHAT_DEFAULT_MAX_CHARS",
    "CLAN_CHAT_WELCOME_MAX_CHARS",
    "DISCORD_INVITE_ROUTE",
    "ClanChatCopyResult",
    "clip_clan_chat_text",
    "generate_clan_chat_copy",
    "messages_from_agent_result",
    "role_action_clan_chat_copy",
    "validate_clan_chat_messages",
]
