__all__ = [
    "_is_status_request", "_is_schedule_request", "_is_db_status_request",
    "_is_clan_list_request", "_clan_status_mode", "_is_war_status_request",
    "_extract_profile_target", "_is_help_request", "_fallback_channel_response",
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
