"""Composition support — voice is the subagent, enrichment is code
(recognition.md §7) and routing (§6).

Carried from event_core/live/runtime.py (intent_context, _META_MARKERS,
render_intent shape) with two changes: subject history reads the streams +
ledger instead of detections, and channel ids resolve from prompts/DISCORD.md
at call time instead of hard-coded constants (recognition.md §8).
"""

from __future__ import annotations

import json
import os
import re

# recognition.md §6 — intent prefix → lane key (lane key ≠ channel name; the
# leadership lane's key is 'arena-relay' but resolves to #leader-actions).
PREFIX_LANE = {
    "celebrate": "member-highlights",
    "clan": "clan-events",
    "cohort": "clan-events",
    "war": "river-race",
    "welcome": "reception",
    "leadership": "arena-relay",
}
FAIL_CLOSED_LANE = "arena-relay"   # unknown never leaks public

_DISCORD_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "prompts", "DISCORD.md"
)


def route(intent_type: str, scope: str) -> str:
    """Intent → lane key. Fail-closed: leadership scope/prefix and any unknown
    prefix route to the leadership lane (recognition.md §6)."""
    prefix = (intent_type or "").split(":", 1)[0]
    if scope == "leadership" or prefix == "leadership":
        return "arena-relay"
    return PREFIX_LANE.get(prefix, FAIL_CLOSED_LANE)


def channels(path: str = _DISCORD_MD) -> dict[str, dict]:
    """Parse prompts/DISCORD.md: lane key → {channel_id, channel_name,
    leadership}. Read at call time so channel config can't drift into code."""
    lanes: dict[str, dict] = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return lanes
    for m in re.finditer(
        r"^## #(?P<name>[\w-]+)\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S
    ):
        body = m.group("body")
        cid = re.search(r"^ID:\s*(\d+)", body, re.M)
        lane = re.search(r"^Lane:\s*([\w-]+)", body, re.M)
        scope = re.search(r"^MemoryScope:\s*(\w+)", body, re.M)
        if cid and lane:
            lanes[lane.group(1)] = {
                "channel_id": int(cid.group(1)),
                "channel_name": m.group("name"),
                "leadership": bool(scope and scope.group(1) == "leadership"),
            }
    return lanes


# recognition.md §7 — the full meta-marker list, verbatim (each from a live
# incident; matching any one means "use the deterministic fallback").
META_MARKERS = (
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


def looks_like_meta(copy: str) -> bool:
    low = (copy or "").lower()
    return any(m in low for m in META_MARKERS)


def _payload(intent_row) -> dict:
    try:
        return json.loads(intent_row["payload_json"]) if intent_row["payload_json"] else {}
    except (TypeError, ValueError):
        return {}


def resolve_name(conn, tag: str | None) -> str | None:
    """Naming guard (recognition.md §7): current name via identity, never
    stale payload copy."""
    if not tag:
        return None
    row = conn.execute(
        "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
    ).fetchone()
    name = row[0] if row else None
    return name if name and str(name).strip() else None


def _subject_history(conn, tag: str, lane_leadership: bool, limit: int = 12) -> list[dict]:
    """The subject's recent recognized moments (ledger + intents), newest
    first, scope-gated to the target lane — a public post never sees
    leadership-only context (recognition.md §7)."""
    scopes = ("public", "leadership") if lane_leadership else ("public",)
    rows = conn.execute(
        f"""SELECT i.intent_type, i.payload_json, i.created_at
            FROM communication_intents i
            WHERE json_extract(i.payload_json, '$.subject_tag') = ?
              AND i.scope IN ({','.join('?' * len(scopes))})
              AND i.status IN ('pending', 'fulfilled')
            ORDER BY i.intent_id DESC LIMIT ?""",
        (tag, *scopes, limit),
    ).fetchall()
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            p = {}
        out.append({
            "intent_type": r["intent_type"],
            "event_type": p.get("event_type"),
            "occurred_at": p.get("occurred_at") or r["created_at"],
        })
    return out


def _recent_win(conn, tag: str, limit: int = 25) -> dict | None:
    """The subject's latest competitive win from battle_events — grounded
    telemetry the composer enriches via CR read tools (recognition.md §7)."""
    row = conn.execute(
        """SELECT battle_time, game_mode_name, opponent_tag, crowns_for,
                  crowns_against, trophy_change
           FROM battle_events
           WHERE player_tag = ? AND outcome = 'W' AND is_competitive = 1
           ORDER BY battle_time DESC LIMIT 1""",
        (tag,),
    ).fetchone()
    return dict(row) if row else None


def intent_context(conn, intent_row) -> str:
    """Presentation-free facts for the composing subagent (NOT copy).
    Port of event_core/live/runtime.py intent_context onto the streams."""
    payload = _payload(intent_row)
    intent_type = intent_row["intent_type"] or ""
    prefix = intent_type.split(":", 1)[0]
    tag = payload.get("subject_tag")
    facts = {"type": intent_type, "player": tag, **payload}
    name = resolve_name(conn, tag)
    if name:
        facts["player_name"] = name

    lane_leadership = intent_row["scope"] == "leadership"
    naming = (
        "Use the member's current in-game name (player_name) — never the raw tag. "
    )
    if prefix == "war":
        ask = (
            "Write ONE short post for the #river-race channel in your own voice from "
            "these war facts. Match the moment: momentum while the race is live, "
            "closure and recognition once it's won or finished. Never guilt."
        )
    elif prefix == "celebrate":
        win = _recent_win(conn, tag) if tag else None
        if win:
            facts["recent_win"] = win
        history = _subject_history(conn, tag, lane_leadership) if tag else []
        if history:
            facts["recent_history"] = history
        ask = (
            "Compose a short, natural #player-highlights post celebrating this "
            "milestone, in your own voice. " + naming +
            "Feature a concrete recent moment if recent_win is present; "
            "recent_history is their recent run for color. Ground every specific "
            "in these facts — do not invent details. A line or two."
        )
    elif prefix == "cohort":
        ask = (
            "Several members hit the same milestone today. Compose ONE short "
            "#clan-events post naming them together. " + naming
        )
    else:
        history = _subject_history(conn, tag, lane_leadership) if tag else []
        if history:
            facts["recent_history"] = history
        ask = (
            "Compose a short, natural post in your own voice for this clan event. "
            + naming + "Use only these facts; do not invent details."
        )
    return f"{ask}\n\n```json\n{json.dumps(facts, indent=2, default=str)}\n```"


def render_intent(intent_row) -> str:
    """Deterministic fallback copy (recognition.md §7 meta-marker guard);
    shape carried from event_core/live/discord.py render_intent."""
    p = _payload(intent_row)
    subj = p.get("player_name") or p.get("name") or p.get("subject_tag") or "A clan member"
    et = p.get("event_type") or (intent_row["intent_type"] or "").split(":", 1)[-1]
    if et == "arena_up":
        arena = p.get("arena_name") or "a new arena"
        return f"🏟️ {subj} advanced to {arena}!"
    if et == "level_up":
        lvl = p.get("level")
        return f"⬆️ {subj} reached King level {lvl}." if lvl else f"⬆️ {subj} leveled up."
    if et == "card_unlocked":
        card = p.get("card_name") or "a new card"
        if str(p.get("rarity", "")).lower() == "champion":
            return f"👑 {subj} unlocked Champion {card}!"
        return f"🎉 {subj} unlocked {card}."
    if et == "card_level_milestone":
        return f"⭐ {subj} took {p.get('card_name') or 'a card'} to level {p.get('milestone')}."
    if et == "career_wins_milestone":
        return f"🏆 {subj} reached {p.get('milestone')} career wins!"
    if et == "collection_level_milestone":
        return f"📚 {subj} reached collection level {p.get('milestone')}."
    if et == "best_trophies_peak":
        return f"🏆 {subj} hit a new trophy best of {p.get('best_trophies') or p.get('boundary')}!"
    if et == "trophy_push":
        return f"📈 {subj} pushed +{p.get('trophy_delta')} trophies over {p.get('battle_count')} battles."
    if et == "badge_earned":
        return f"🎖️ {subj} earned the {p.get('badge_name')} badge."
    if et == "ranked_pulse":
        return f"⚔️ {subj} is on a ranked tear — {p.get('wins')}W/{p.get('losses')}L this week."
    if et in ("pol_promotion", "ultimate_champion_reached", "pol_global_rank_attained"):
        return f"🏅 {subj} climbed the Path of Legends — {p.get('league') or 'a new league'}."
    if et == "member_joined":
        return f"👋 Welcome {subj} to POAP KINGS!"
    if et == "member_left":
        return f"👋 {subj} has left the clan. Wishing them well."
    if et == "role_changed":
        return f"🎉 {subj} was promoted to {p.get('new_role')}!"
    if et in ("member_birthday", "clan_birthday", "join_anniversary"):
        return f"🎂 Celebrating {subj} today!"
    if et == "weekly_donation_leader":
        leaders = p.get("leaders") or []
        top = leaders[0]["name"] if leaders and isinstance(leaders[0], dict) else subj
        return f"🎁 {top} led donations this week. Thank you!"
    if et == "week_finished":
        return f"🏁 War week finished — we placed #{p.get('our_rank')} with {p.get('our_fame')} fame."
    if et == "season_closed":
        champ = p.get("war_champ_name") or p.get("war_champ_tag") or "our top contributor"
        return f"🏆 War season closed — {champ} is the War Champ!"
    if et.startswith("cohort_wave"):
        return "🎉 Multiple members hit the same milestone today — a clan wave!"
    return f"📣 {subj}: {et.replace('_', ' ')}."
