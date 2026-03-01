"""elixir_agent.py — LLM-powered observation and response engine for Elixir."""
import json
import logging
import os
from openai import OpenAI

log = logging.getLogger("elixir_agent")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

ELIXIR_PERSONALITY = (
    "You are Elixir 🧪, the sharp-minded chronicler and advisor for POAP KINGS, "
    "a Clash Royale clan. You know the game deeply — arenas, card donations, River Race, "
    "trophy pushing, Elder/Co-Leader promotions. You're confident, direct, and occasionally "
    "witty. You avoid repeating yourself. You always sign off with 🧪."
)

OBSERVE_SYSTEM = (
    ELIXIR_PERSONALITY + "\n\n"
    "Your job: review the current clan data and recent post history, then decide if anything "
    "is genuinely worth posting. Only post when something real has happened — a trophy milestone, "
    "a donation leader pulling ahead, war battle activity, a new member settling in, an inactive "
    "member pattern. Do NOT post if nothing interesting has changed or you covered it recently.\n\n"
    "Respond with JSON only (no markdown wrapper):\n"
    '{"event_type": "clan_observation|arena_milestone|donation_milestone|war_update|member_join|member_leave", '
    '"member_tags": [], "member_names": [], "summary": "one sentence", '
    '"content": "full Discord-ready markdown post", "metadata": {}}\n\n'
    "Or respond with exactly: null\n\nif there is nothing worth posting right now."
)

LEADER_SYSTEM = (
    ELIXIR_PERSONALITY + "\n\n"
    "You are answering a question from a clan leader in #leader-lounge. "
    "Base your answer entirely on the clan data provided. Be direct, give a concrete recommendation, "
    "and explain your reasoning briefly.\n\n"
    "Respond with JSON only (no markdown wrapper):\n"
    '{"event_type": "leader_response", "member_tags": [], "member_names": [], '
    '"summary": "one sentence TL;DR", "content": "full Discord-ready markdown response", "metadata": {}}'
)


def _clan_context(clan_data: dict, war_data: dict, recent: list) -> str:
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_summary = []
    for m in sorted(members, key=lambda x: x.get("clanRank", x.get("clan_rank", 99))):
        member_summary.append(
            f"  {m.get('name','?')} | rank #{m.get('clanRank', m.get('clan_rank','?'))} | "
            f"{m.get('trophies',0):,} trophies | {m.get('donations',0)} donations | "
            f"role: {m.get('role','member')} | arena: {m.get('arena','?')} | "
            f"last_seen: {m.get('lastSeen', m.get('last_seen','?'))}"
        )
    war_summary = "No active war."
    if war_data and war_data.get("state") not in (None, "notInWar"):
        parts = war_data.get("clan", {}).get("participants", [])
        fame = war_data.get("clan", {}).get("fame", 0)
        used = [p["name"] for p in parts if p.get("decksUsedToday", 0) > 0]
        unused = [p["name"] for p in parts if p.get("decksUsedToday", 0) == 0]
        war_summary = (
            f"River Race state: {war_data.get('state')} | fame: {fame:,} | "
            f"battled today: {', '.join(used) or 'nobody'} | "
            f"not yet: {', '.join(unused) or 'everyone done'}"
        )
    recent_summary = json.dumps(recent, indent=2) if recent else "None yet."
    return (
        f"=== CLAN ROSTER ===\n" + "\n".join(member_summary) +
        f"\n\n=== WAR STATUS ===\n{war_summary}" +
        f"\n\n=== RECENT ELIXIR POSTS (last {len(recent)}) ===\n{recent_summary}"
    )


def _parse_response(text: str):
    text = text.strip()
    if text.lower() == "null":
        return None
    try:
        # strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        log.error("Failed to parse agent response: %s\nRaw: %s", e, text)
        return None


def observe_and_post(clan_data: dict, war_data: dict, recent_entries: list):
    """Returns observation dict or None if nothing worth posting."""
    context = _clan_context(clan_data, war_data, recent_entries)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": OBSERVE_SYSTEM},
                {"role": "user", "content": context}
            ],
            temperature=0.7,
            max_tokens=800,
        )
        return _parse_response(resp.choices[0].message.content)
    except Exception as e:
        log.error("observe_and_post error: %s", e)
        return None


def respond_to_leader(question: str, author_name: str, clan_data: dict, war_data: dict, recent_entries: list):
    """Returns leader response dict."""
    context = _clan_context(clan_data, war_data, recent_entries)
    user_msg = f"Leader '{author_name}' asks: {question}\n\n{context}"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": LEADER_SYSTEM},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.7,
            max_tokens=800,
        )
        return _parse_response(resp.choices[0].message.content)
    except Exception as e:
        log.error("respond_to_leader error: %s", e)
        return None
