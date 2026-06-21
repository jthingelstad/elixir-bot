"""Event-driven live posting — agent-composed, reactive (not scheduled awareness).

The only schedule is the ingest poll (external API pacing). Posting is reactive:
the IntentConsumer turns new CommunicationIntent events into posts, each composed
by the agent in the target channel's voice (reusing generate_channel_update — no
templates), routed by scope/type.

go_live_drain brings state current then fast-forwards the consumer to the log head
so the downtime backlog is NOT posted; only post-go-live events are.
"""
from __future__ import annotations

import json

# scope/intent-type -> Discord channel (from prompts/DISCORD.md). Confirmed mapping:
PUBLIC_HIGHLIGHTS = {"channel_id": 1482352147029950474, "channel_name": "player-highlights", "lane": "member-highlights", "leadership": False}
LEADER_ACTIONS = {"channel_id": 1513758211206025227, "channel_name": "leader-actions", "lane": "arena-relay", "leadership": True}


def route_intent(intent) -> dict:
    """Map an intent to its target channel config."""
    if intent.scope == "leadership" or (intent.intent_type or "").startswith("leadership:"):
        return LEADER_ACTIONS
    return PUBLIC_HIGHLIGHTS


def intent_context(intent) -> str:
    """Presentation-free facts for the agent to compose from (NOT copy)."""
    return (
        "Compose a short, natural post in your own voice for this clan event. "
        "Use only these facts; do not invent details.\n\n"
        f"```json\n{json.dumps({'type': intent.intent_type, 'player': intent.subject_tag, **(intent.summary or {})}, indent=2, default=str)}\n```"
    )


def _extract_copy(result) -> str | None:
    """Pull post text from generate_channel_update's structured return."""
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, dict):
        posts = result.get("posts")
        if posts:
            p = posts[0]
            return (p.get("content") or p.get("summary") or "").strip() or None if isinstance(p, dict) else str(p)
        return (result.get("content") or result.get("summary") or "").strip() or None
    return None


def compose_copy(intent) -> str | None:
    """Agent composes voice-appropriate copy for the intent's channel lane."""
    import elixir_agent
    from event_core.live.discord import render_intent

    ch = route_intent(intent)
    try:
        result = elixir_agent.generate_channel_update(
            ch["channel_name"], ch["lane"], intent_context(intent), leadership=ch["leadership"]
        )
        copy = _extract_copy(result)
        if copy:
            return copy
    except Exception:
        pass
    return render_intent(intent)  # last-resort fallback only


class CollectingPoster:
    """Composes copy (agent) and collects (channel_id, text) to post; marks the
    intent fulfilled. The async service layer sends the collected posts to Discord."""

    def __init__(self):
        self.queued: list[dict] = []

    def __call__(self, intent) -> bool:
        ch = route_intent(intent)
        copy = compose_copy(intent)
        if not copy:
            return False
        self.queued.append({"channel_id": ch["channel_id"], "text": copy, "scope": intent.scope})
        return True


def prepare_posts(app, conn) -> list[dict]:
    """Process new intents reactively: compose + collect posts (does not send).
    Returns [{channel_id, text, scope}] for the async layer to deliver."""
    from event_core.live.discord_consumer import IntentConsumer

    poster = CollectingPoster()
    IntentConsumer(app, conn, poster).run()
    return poster.queued


def go_live_drain(app, conn) -> int:
    """Cutover step: drain all intents up to the current log head WITHOUT posting,
    so the downtime backlog/catch-up is not broadcast. Returns the head position."""
    from event_core.live.discord_consumer import IntentConsumer

    return IntentConsumer(app, conn, poster=lambda i: True).fast_forward()
