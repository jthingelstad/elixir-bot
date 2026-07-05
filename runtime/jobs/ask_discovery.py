"""Capability-discovery rotation for the #ask-elixir daily post.

Pivot (Jamie, 2026-07-04): the daily post was random card trivia ("it isn't
interesting"); it becomes a rotating tour of what Elixir can actually DO,
each post carrying one real data nugget and 2-3 copy-pasteable questions.
Every sample question is backed by the interactive workflow's real tool
surface (INTERACTIVE_READ_TOOLS + memory context) — never promise an answer
the ask lane can't deliver.

Rotation state rides in the job's own runtime_job_status last_summary
("category=<key>" marker) — no new tables; deterministic cycle, empty-nugget
categories skipped.
"""

from __future__ import annotations

import json
import logging


log = logging.getLogger("elixir.ask_discovery")

_MARKER = "category="


# ---------------------------------------------------------------- nuggets
# Each returns a small dict of REAL facts (or None to skip the category).
# Read-only, one connection passed in, empty-DB safe.

def _war_now(conn):
    row = conn.execute(
        "SELECT payload_json FROM state_baselines "
        "WHERE entity_kind='riverrace' AND aspect='race' LIMIT 1"
    ).fetchone()
    if not row:
        return None
    p = json.loads(row["payload_json"] or "{}")
    if p.get("our_fame") is None:
        return None
    return {"current_fame": p.get("our_fame"), "period_type": p.get("period_type")}


def _war_history(conn):
    row = conn.execute(
        """SELECT ws.season_id,
                  SUM(CASE WHEN ww.our_rank = 1 THEN 1 ELSE 0 END) AS firsts,
                  COUNT(ww.section_index) AS weeks
           FROM war_seasons ws JOIN war_weeks ww ON ww.season_id = ws.season_id
           WHERE ww.our_rank IS NOT NULL
           GROUP BY ws.season_id ORDER BY ws.season_id DESC LIMIT 1"""
    ).fetchone()
    if not row or not row["weeks"]:
        return None
    return {"season_id": row["season_id"], "first_place_weeks": row["firsts"],
            "finished_weeks": row["weeks"]}


def _awards(conn):
    row = conn.execute(
        """SELECT a.award_type, a.season_id, p.current_name
           FROM awards a JOIN players p ON p.player_tag = a.player_tag
           WHERE a.rank = 1 AND a.award_type IN ('war_champ','donation_champ')
           ORDER BY a.awarded_at DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return {"latest_top_award": row["award_type"], "season_id": row["season_id"],
            "holder": row["current_name"]}


def _donations(conn):
    row = conn.execute(
        """SELECT p.current_name, pcs.donations_week
           FROM player_current_state pcs JOIN players p ON p.player_tag = pcs.player_tag
           WHERE COALESCE(pcs.donations_week, 0) > 0
           ORDER BY pcs.donations_week DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return {"top_donor_this_week": row["current_name"],
            "donations_this_week": row["donations_week"]}


def _ranked(conn):
    rows = conn.execute(
        """SELECT p.current_name, pcs.ranked_league, pcs.ranked_trophies
           FROM player_current_state pcs JOIN players p ON p.player_tag = pcs.player_tag
           WHERE pcs.ranked_league IS NOT NULL
           ORDER BY pcs.ranked_league DESC, COALESCE(pcs.ranked_trophies, 0) DESC
           LIMIT 1"""
    ).fetchone()
    if not rows:
        return None
    return {"top_ranked_player": rows["current_name"],
            "league_number": rows["ranked_league"], "rating": rows["ranked_trophies"]}


def _modes(conn):
    rows = conn.execute(
        """SELECT mode_group, COUNT(*) AS n FROM battle_events
           WHERE observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')
           GROUP BY mode_group ORDER BY n DESC LIMIT 3"""
    ).fetchall()
    if not rows:
        return None
    return {"battle_mix_7d": {r["mode_group"]: r["n"] for r in rows}}


def _roster(conn):
    count = conn.execute(
        "SELECT COUNT(*) FROM clan_memberships WHERE left_at IS NULL"
    ).fetchone()[0]
    newest = conn.execute(
        """SELECT p.current_name FROM clan_memberships cm
           JOIN players p ON p.player_tag = cm.player_tag
           WHERE cm.left_at IS NULL ORDER BY cm.joined_at DESC LIMIT 1"""
    ).fetchone()
    if not count:
        return None
    return {"member_count": count, "newest_member": newest["current_name"] if newest else None}


def _cards(conn):
    row = conn.execute(
        """SELECT COUNT(*) AS maxed FROM player_card_collection pcc
           JOIN card_catalog cc ON cc.card_id = pcc.card_id
           WHERE pcc.level + MAX(0, 16 - cc.max_level) >= 16"""
    ).fetchone()
    if not row or not row["maxed"]:
        return None
    return {"maxed_cards_across_clan": row["maxed"]}


def _memory(conn):
    n = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE retired_at IS NULL"
    ).fetchone()[0]
    if not n:
        return None
    return {"durable_memories": n}


def _screenshots(conn):
    return {"feature": "screenshot reading is always on in #ask-elixir"}


CATALOG = [
    {
        "key": "war_now",
        "blurb": "Elixir watches the River Race live — standings, fame, and who still has decks today.",
        "nugget": _war_now,
        "questions": ["How's the war going?",
                      "How many war decks are left today?",
                      "Where did we place last week?"],
    },
    {
        "key": "my_stats",
        "blurb": "Elixir knows your history here — trophies, battles, tenure, your whole arc in the clan.",
        "nugget": _roster,
        "questions": ["How am I doing this week?",
                      "When did I join the clan?",
                      "What's my best trophy count ever?"],
    },
    {
        "key": "cards",
        "blurb": "Elixir tracks every member's card collection and knows the game's matchups cold.",
        "nugget": _cards,
        "questions": ["What cards am I closest to maxing?",
                      "What's a good counter to Mega Knight?",
                      "Who in the clan has Balloon maxed?"],
    },
    {
        "key": "war_history",
        "blurb": "Every war week and season lives in Elixir's records — placements, fame, streaks.",
        "nugget": _war_history,
        "questions": ["How did we do this war season?",
                      "Who were our top war contributors this week?",
                      "Have we ever lost a war week?"],
    },
    {
        "key": "awards",
        "blurb": "War Champ, donation champ, season honors — Elixir keeps the clan's trophy case.",
        "nugget": _awards,
        "questions": ["Who was the last War Champ?",
                      "Who won awards last season?"],
    },
    {
        "key": "donations",
        "blurb": "Elixir sees every donation — who's generous this week and all-time.",
        "nugget": _donations,
        "questions": ["Who's donated the most this week?",
                      "How many cards have I donated?"],
    },
    {
        "key": "ranked",
        "blurb": "Elixir follows Ranked too — leagues, ratings, and who's pushing for Ultimate Champion.",
        "nugget": _ranked,
        "questions": ["Who's our top Ranked player?",
                      "What league is Atternam in?"],
    },
    {
        "key": "modes",
        "blurb": "Ladder, war, Ranked, 2v2, events — Elixir sees every battle the clan plays, in every mode.",
        "nugget": _modes,
        "questions": ["What game modes does the clan play most?",
                      "How many battles did we play this week?"],
    },
    {
        "key": "screenshots",
        "blurb": "Drop any Clash Royale screenshot here — decks, battles, shop offers — and Elixir reads it.",
        "nugget": _screenshots,
        "questions": ["(post a battle screenshot) What went wrong here?",
                      "(post a deck screenshot) How would you improve this deck?"],
    },
    {
        "key": "memory",
        "blurb": "Elixir remembers — clan history, member stories, past seasons; ask it anything about us.",
        "nugget": _memory,
        "questions": ["What do you remember about this clan?",
                      "What happened in the clan this week?"],
    },
]


def last_category_key(status_summary: str | None) -> str | None:
    if not status_summary or _MARKER not in status_summary:
        return None
    return status_summary.split(_MARKER, 1)[1].split()[0].strip()


def pick_category(conn, last_key: str | None) -> tuple[dict, dict] | None:
    """Next category in the cycle whose nugget has data. Deterministic:
    starts after last_key, wraps once; None if every nugget is empty."""
    keys = [c["key"] for c in CATALOG]
    start = (keys.index(last_key) + 1) if last_key in keys else 0
    for i in range(len(CATALOG)):
        cat = CATALOG[(start + i) % len(CATALOG)]
        try:
            facts = cat["nugget"](conn)
        except Exception:
            log.warning("discovery nugget %s failed", cat["key"], exc_info=True)
            facts = None
        if facts:
            return cat, facts
    return None


def fallback_post(cat: dict, facts: dict) -> str:
    """Deterministic copy when composition fails — blurb + nugget + questions."""
    fact_line = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in facts.items()
                          if k != "feature")
    lines = [cat["blurb"]]
    if fact_line:
        lines.append(f"(Right now: {fact_line}.)")
    lines.append("")
    lines.append("Try asking:")
    lines.extend(f"> {q}" for q in cat["questions"])
    return "\n".join(lines)


def compose_context(cat: dict, facts: dict) -> str:
    """The LLM composition ask — facts-only, questions verbatim."""
    return "\n".join([
        "Write today's short #ask-elixir capability spotlight.",
        "Goal: help members discover ONE thing they can ask Elixir, in Elixir's voice.",
        f"Capability: {cat['blurb']}",
        f"Live facts you may reference (the ONLY permissible specifics): {json.dumps(facts, default=str)}",
        "Rules:",
        "- 2-4 short sentences, then a 'Try asking:' section.",
        "- The 'Try asking:' lines must be EXACTLY these, each on its own quoted line:",
        *[f"  > {q}" for q in cat["questions"]],
        "- Do not invent any number, name, or event not in the facts above.",
        "- No headers, no walls of text — this is a chat channel.",
        "- Warm and useful, not salesy.",
    ])
