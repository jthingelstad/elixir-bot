import re

__all__ = [
    "_is_status_request", "_is_schedule_request", "_is_db_status_request",
    "_is_clan_list_request", "_clan_status_mode", "_is_war_status_request",
    "_extract_profile_target", "_is_roster_join_dates_request",
    "_is_kick_risk_request", "_is_top_war_contributors_request",
    "_is_member_deck_request", "_is_deck_review_request",
    "_classify_deck_request", "_is_help_request", "_fallback_channel_response",
]


def _is_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "status",
        "!status",
        "/status",
        "elixir status",
        "@elixir status",
        "health",
        "health check",
    }


def _is_schedule_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "schedule",
        "schedules",
        "!schedule",
        "/schedule",
        "job schedule",
        "job schedules",
        "elixir schedule",
        "@elixir schedule",
    }


def _is_db_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "db status",
        "database status",
        "!db-status",
        "/db-status",
        "elixir db status",
        "@elixir db status",
    }


def _is_clan_list_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "clan list",
        "list clan",
        "clan roster",
        "list roster",
        "member list",
        "list members",
        "show clan members",
        "show roster",
    }


def _clan_status_mode(text: str) -> str | None:
    normalized = " ".join((text or "").strip().lower().split())
    if normalized in {
        "clan status",
        "!clan-status",
        "/clan-status",
        "clan health",
        "clan health check",
        "poap kings status",
    }:
        return "full"
    if normalized in {
        "clan status short",
        "clan status brief",
        "!clan-status-short",
        "/clan-status-short",
        "clan health short",
        "poap kings status short",
    }:
        return "short"
    return None


def _is_war_status_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "war status",
        "!war-status",
        "/war-status",
        "river race status",
        "war health",
        "current war status",
        "poap kings war status",
    }


def _extract_profile_target(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        tokens = re.findall(r'''(?:"([^"]+)")|(?:'([^']+)')|(\S+)''', raw)
        flat = [a or b or c for a, b, c in tokens]
    except Exception:
        flat = raw.split()
    lowered = [token.lower() for token in flat]
    if len(flat) >= 2 and lowered[0] == "profile":
        return " ".join(flat[1:]).strip() or None
    if len(flat) >= 3 and lowered[0] == "member" and lowered[1] == "profile":
        return " ".join(flat[2:]).strip() or None
    return None


def _is_roster_join_dates_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who are the members of the clan and when did they join?",
        "who are the members of the clan and when did they join",
        "who is in the clan and when did they join?",
        "who is in the clan and when did they join",
        "list the clan members and when they joined",
        "list clan members and join dates",
        "show clan members and join dates",
        "roster with join dates",
    }


def _is_kick_risk_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who is at risk of being kicked based on participation thresholds?",
        "who is at risk of being kicked based on participation thresholds",
        "who is at risk of being kicked?",
        "who is at risk of being kicked",
        "who is at kick risk?",
        "who is at kick risk",
        "who should be kicked for inactivity?",
        "who should be kicked for inactivity",
        "which members are inactive for more than 1 week?",
        "which members are inactive for more than 1 week",
        "which members are inactive for more than a week?",
        "which members are inactive for more than a week",
    }


def _is_top_war_contributors_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "who are the top 5 contributors to clan wars this season?",
        "who are the top 5 contributors to clan wars this season",
        "who are the top war contributors this season?",
        "who are the top war contributors this season",
        "top war contributors this season",
        "show top war contributors this season",
        "who are the top contributors to clan wars this season?",
        "who are the top contributors to clan wars this season",
    }


_DECK_DISPLAY_PATTERNS = (
    r"\bwhat cards are in my deck\b",
    r"\bwhat cards were in my deck\b",
    r"\bwhat is in my deck\b",
    r"\bwhat's in my deck\b",
    r"\bshow (?:me )?my deck\b",
    r"\bshow (?:me )?@?[a-z0-9_.-]+'?s deck\b",
    r"\bwhat cards are in @?[a-z0-9_.-]+'?s deck\b",
    r"\bwhat cards are in @?[a-z0-9_.-]+ deck\b",
    r"\bwhat is in @?[a-z0-9_.-]+'?s deck\b",
    r"\bwhat's in @?[a-z0-9_.-]+'?s deck\b",
)

_DECK_REVIEW_PATTERNS = (
    r"\breview (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
    r"\bimprove (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
    r"\b(?:fix|tune|tweak|optimize|optimise) (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
    r"\bwhat should i (?:change|swap|drop|add|replace)\b.*\bdeck",
    r"\bwhat (?:cards )?(?:should|could) i (?:change|swap|drop|add|replace)\b",
    r"\bswap (?:cards )?in (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
    r"\bany (?:advice|tips|suggestions) (?:on|for|about) (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
    r"\bhow (?:can|do) i (?:improve|fix|tune) (?:my|@?[a-z0-9_.-]+'?s)(?: war| river race)? decks?\b",
)

_DECK_SUGGEST_PATTERNS = (
    r"\b(?:build|make) (?:me )?(?:a |another |new )?deck",
    r"\b(?:suggest|recommend) (?:a |another |new )?deck",
    r"\bwhat (?:deck|decks) should i (?:play|use|run)\b",
    r"\bhelp me (?:start|get into|begin) (?:playing |learning )?(?:war|river race|clan war)",
    r"\b(?:build|make) (?:me )?(?:my |four )?war decks?\b",
    r"\bi (?:want to|wanna|need to) (?:play|start|get into|begin) (?:playing |learning )?(?:war|river race|clan war)",
    r"\bi don'?t know what (?:to play|deck to use)\b",
)

_WAR_DECK_KEYWORDS = (
    "war deck", "war decks", "river race deck", "river race decks",
    "clan war deck", "clan war decks", "river race", "clan wars",
    "war kit", "duel deck", "war battle", "river-race",
)

_WAR_SUGGEST_PATTERNS = (
    r"\bhelp me (?:start|get into|begin) (?:playing |learning )?(?:war|river race|clan war)",
    r"\b(?:build|make) (?:me )?(?:my |four )?war decks?\b",
    r"\bi (?:want to|wanna|need to) (?:play|start|get into|begin) (?:playing |learning )?(?:war|river race|clan war)",
)


def _classify_deck_request(text: str) -> dict | None:
    """Classify a deck-related message into {subject, mode}.

    Returns None if the message isn't deck-related.
    subject: 'display' | 'review' | 'suggest'
    mode:    'regular' | 'war'

    Display = static "show me the cards" report (no LLM).
    Review = critique an existing deck (LLM workflow).
    Suggest = build a new deck from collection (LLM workflow).
    """
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return None

    war_kw_match = any(kw in normalized for kw in _WAR_DECK_KEYWORDS)
    war_suggest_match = any(re.search(p, normalized) for p in _WAR_SUGGEST_PATTERNS)

    if any(re.search(p, normalized) for p in _DECK_SUGGEST_PATTERNS) or war_suggest_match:
        mode = "war" if war_kw_match or war_suggest_match else "regular"
        return {"subject": "suggest", "mode": mode}

    has_deck_word = "deck" in normalized or "river race" in normalized or "clan war" in normalized
    if not has_deck_word:
        return None

    mode = "war" if war_kw_match else "regular"
    if any(re.search(p, normalized) for p in _DECK_REVIEW_PATTERNS):
        return {"subject": "review", "mode": mode}
    if "current deck" in normalized:
        return {"subject": "display", "mode": "regular"}
    if any(re.search(p, normalized) for p in _DECK_DISPLAY_PATTERNS):
        return {"subject": "display", "mode": "regular"}
    return None


def _is_member_deck_request(text: str) -> bool:
    classified = _classify_deck_request(text)
    return bool(classified and classified["subject"] == "display")


def _is_deck_review_request(text: str) -> bool:
    classified = _classify_deck_request(text)
    return bool(classified and classified["subject"] in {"review", "suggest"})


def _is_help_request(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized in {
        "help",
        "!help",
        "/help",
        "elixir help",
        "@elixir help",
        "what can you do",
        "what do you do",
    }


def _fallback_channel_response(question: str, workflow: str) -> str:
    normalized = " ".join((question or "").strip().lower().split())
    if "war participation rate" in normalized:
        return "I don't have enough recent war participation data to answer that reliably yet."
    if "what cards are in my deck" in normalized or "current deck" in normalized:
        return "I couldn't build a clean deck answer just now. Try again in a moment."
    if workflow == "clanops":
        return "I couldn't produce a clean answer from the data I have. Try asking a narrower clan ops question."
    return "I couldn't produce a clean answer just now. Try again in a moment."
