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
    "clan": CLAN_EVENTS,
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


def _subject_history(conn, intent, limit: int = 12) -> list[dict]:
    """The subject player's recent detection stream (newest first), excluding the
    detection that triggered THIS intent. Scope-gated to the target channel so a
    public post never sees leadership-only detections.

    This is the v5 payoff in practice: composing in the context of everything the
    player has done recently, not just the single event that fired. Returns [] for
    non-player subjects, first-seen players, or any read error (compose degrades to
    the single-event behavior, never fails the post)."""
    tag = intent.subject_tag or ""
    if not tag.startswith("#") or conn is None:
        return []
    from event_core.mind.communication import PUBLIC_INTENT_PREFIX
    from event_core.read import tools

    leadership = bool(route_intent(intent).get("leadership"))
    scope = "leadership" if leadership else "public"
    current = (intent.dedup_key or "").removeprefix("intent:detection:")
    try:
        # Over-fetch so type-filtering below doesn't starve the window.
        rows = tools.get_player_detections(conn, tag, scope=scope, limit=max(limit * 3, limit + 1))
    except Exception:
        return []
    history: list[dict] = []
    for r in rows:
        if r.get("dedup_key") == current:
            continue  # don't echo the triggering event back as "history"
        dtype = r.get("detection_type")
        # On a public post, only carry detection types we'd actually surface, so
        # retired/internal signals (e.g. battle_hot_streak, which still has rows in
        # the projection but no longer posts) don't pollute the holistic context.
        # Leadership posts get the full stream.
        if not leadership and dtype not in PUBLIC_INTENT_PREFIX:
            continue
        entry = {"type": dtype, "when": r.get("occurred_at")}
        raw = r.get("payload_json")
        if raw:
            try:
                entry["facts"] = json.loads(raw)
            except Exception:
                pass
        history.append(entry)
        if len(history) >= limit:
            break
    return history


def intent_context(intent, conn=None) -> str:
    """Presentation-free facts for the agent to compose from (NOT copy).

    When `conn` (a projections-DB connection) is supplied and the subject is a
    player, the player's recent detection stream is included as `recent_history`
    so the post can be holistic rather than reacting to the triggering event in
    isolation."""
    summary = intent.summary or {}
    payload = {'type': intent.intent_type, 'player': intent.subject_tag, **summary}

    dtype = summary.get("detection_type")
    if dtype in ("war_update", "war_complete"):
        facts = json.dumps(payload, indent=2, default=str)
        if dtype == "war_complete":
            ask = (
                "Our clan-wars race just finished. Write ONE short result post for "
                "the #river-race channel in your own voice — where we placed, the "
                "fame total, and a nod to the clan. Celebratory but not over the top."
            )
        else:
            ask = (
                "Write ONE short clan-wars standing for the #river-race channel in "
                "your own voice: where we sit right now (rank, fame, the gap to the "
                "rival clans in \"standings\"), and a nudge to use remaining war decks. "
                "Optionally use your tools to check who still has decks to play today. "
                "Keep it to a few lines."
            )
        return (
            f"{ask} Be concrete and motivating; use these facts and do not invent "
            f"details.\n\n```json\n{facts}\n```"
        )
    if dtype == "new_season":
        # New clan-wars season -> an opponent intel briefing. The agent has CR
        # read tools; have it scout the competitor clans in our river-race group.
        facts = json.dumps(payload, indent=2, default=str)
        return (
            "A new Clash Royale clan-wars season has started. Write a short opponent "
            "intel briefing for the leadership-facing river-race channel in your own "
            "voice: use your tools to look up the other clans in our current river race "
            "group and call out the main threats (clan score, war trophies, notable "
            "members). Be concrete; do not invent details.\n\n"
            f"```json\n{facts}\n```"
        )

    history = _subject_history(conn, intent)
    if history:
        payload["recent_history"] = history
    facts = json.dumps(payload, indent=2, default=str)
    if history:
        return (
            "Compose a short, natural post in your own voice for this clan event. "
            "The triggering event is the top-level fields (\"type\", \"player\", and "
            "the detection fields); \"recent_history\" is what this player has done "
            "recently, newest first. Where it makes the post better, set the moment "
            "in the context of their recent run (e.g. a streak of milestones, a hot "
            "week). Stay concise; use only these facts and do not invent details.\n\n"
            f"```json\n{facts}\n```"
        )
    return (
        "Compose a short, natural post in your own voice for this clan event. "
        "Use only these facts; do not invent details.\n\n"
        f"```json\n{facts}\n```"
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


# High-precision markers that the agent returned a meta/diagnostic note ("I'm not
# posting this and here's why") instead of channel copy. Seen live: a "Signal data
# inconsistent…" line posted to #player-highlights, and a "Signal is from… would
# be stale" note in a war dry-run. None of these phrasings occur in a real Elixir
# post, so matching any one means "don't post this; use the deterministic fallback."
_META_MARKERS = (
    "skipping post",
    "skip this post",
    "would be stale",
    "is stale",
    "signal is from",
    "signal data",
    "data inconsistent",
    "inconsistent with",
    "live race is now",
    "as an ai",
    "unable to compose",
    "cannot compose",
)


def _looks_like_meta(copy: str) -> bool:
    low = copy.lower()
    return any(m in low for m in _META_MARKERS)


def compose_copy(intent) -> str | None:
    """Agent composes voice-appropriate copy for the intent's channel lane.

    Opens a short-lived projections-DB connection so the compose prompt can carry
    the subject player's recent detection stream (see intent_context). The read is
    best-effort: if the connection or read fails, compose proceeds with the single
    triggering event."""
    import logging

    import elixir_agent
    from event_core import config, db
    from event_core.live.discord import render_intent

    ch = route_intent(intent)
    conn = None
    try:
        conn = db.connect(config.PROJECTIONS_DB)
    except Exception:
        logging.getLogger("elixir.event_core").warning(
            "compose_copy: could not open projections DB for %s; composing without history",
            intent.dedup_key,
        )
    try:
        result = elixir_agent.generate_channel_update(
            ch["channel_name"], ch["lane"], intent_context(intent, conn), leadership=ch["leadership"]
        )
        copy = _extract_copy(result)
        if copy and _looks_like_meta(copy):
            logging.getLogger("elixir.event_core").warning(
                "compose_copy: agent returned a meta/diagnostic note (not a post) for "
                "%s; using fallback. text=%r", intent.dedup_key, copy[:160]
            )
            copy = None
        if copy:
            return copy
        logging.getLogger("elixir.event_core").warning(
            "compose_copy: agent returned no copy for %s; using fallback", intent.dedup_key
        )
    except Exception:
        logging.getLogger("elixir.event_core").exception(
            "compose_copy: agent failed for %s; using fallback", intent.dedup_key
        )
    finally:
        if conn is not None:
            conn.close()
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
