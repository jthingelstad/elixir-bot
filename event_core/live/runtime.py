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

# intent-type prefix -> Discord channel (ids/lanes from prompts/DISCORD.md).
PUBLIC_HIGHLIGHTS = {"channel_id": 1482352147029950474, "channel_name": "player-highlights", "lane": "member-highlights", "leadership": False}
LEADER_ACTIONS = {"channel_id": 1513758211206025227, "channel_name": "leader-actions", "lane": "arena-relay", "leadership": True}
WELCOME = {"channel_id": 1476456514121109514, "channel_name": "welcome", "lane": "reception", "leadership": False}
RIVER_RACE = {"channel_id": 1482352067573059675, "channel_name": "river-race", "lane": "river-race", "leadership": False}
CLAN_EVENTS = {"channel_id": 1482352241628414013, "channel_name": "clan-events", "lane": "clan-events", "leadership": False}

_PREFIX_CHANNEL = {
    "celebrate": PUBLIC_HIGHLIGHTS,
    "welcome": WELCOME,
    "war": RIVER_RACE,
    "cohort": CLAN_EVENTS,
    "leadership": LEADER_ACTIONS,
}


def route_intent(intent) -> dict:
    """Map an intent to its target channel by intent_type prefix. Fail-closed:
    leadership scope/prefix and any unknown prefix route to the (private)
    leadership channel rather than leaking to a public one."""
    prefix = (intent.intent_type or "").split(":", 1)[0]
    if intent.scope == "leadership" or prefix == "leadership":
        return LEADER_ACTIONS
    return _PREFIX_CHANNEL.get(prefix, LEADER_ACTIONS)


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
    import logging

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
        logging.getLogger("elixir.event_core").warning(
            "compose_copy: agent returned no copy for %s; using fallback", intent.dedup_key
        )
    except Exception:
        logging.getLogger("elixir.event_core").exception(
            "compose_copy: agent failed for %s; using fallback", intent.dedup_key
        )
    return render_intent(intent)  # last-resort deterministic fallback


def make_agent_poster(send):
    """Build a SYNCHRONOUS poster for IntentConsumer: compose copy (agent voice)
    then post via `send(channel_id, text, scope) -> bool`, returning the send
    result. Critically, this composes AND sends before returning True, so the
    consumer only marks the intent fulfilled after a confirmed Discord post
    (at-least-once; no fulfil-before-send loss). `send` is the live service's
    bridge to the discord.py client (blocks on the actual post)."""

    def poster(intent) -> bool:
        ch = route_intent(intent)
        copy = compose_copy(intent)
        if not copy:
            return False
        return bool(send(ch["channel_id"], copy, intent.scope))

    return poster


def go_live_drain(app, conn) -> int:
    """Cutover step: drain all intents up to the current log head WITHOUT posting,
    so the downtime backlog/catch-up is not broadcast. Returns the head position."""
    from event_core.live.discord_consumer import IntentConsumer

    return IntentConsumer(app, conn, poster=lambda i: True).fast_forward()
