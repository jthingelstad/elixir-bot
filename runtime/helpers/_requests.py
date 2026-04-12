__all__ = ["_fallback_channel_response"]


def _fallback_channel_response(question: str, workflow: str) -> str:
    del question
    if workflow == "clanops":
        return "I couldn't produce a clean answer from the data I have. Try asking a narrower clan ops question."
    return "I couldn't produce a clean answer just now. Try again in a moment."
