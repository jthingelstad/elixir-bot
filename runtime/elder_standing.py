"""Weekly Elder Standing — a transparent, public readout of the Elder track.

POAP KINGS is transparent about *why* people are kicked / promoted / demoted; the
Elder track between those events was invisible. This assembles three named groups
from the management projection — Elders holding the bar, members rising toward it,
and Elders slipping ("stepping down") — each with the engine's own participation
evidence, and renders them for #announcements.

Since the 2026-07-12 scoring rework (participation, not league/prestige), being
"outranked" means *someone participated more than you this week* — in the player's
control — so the whole board is fair to surface. No Discord tables (they don't
render): bold headers + short bullet lines only.
"""
from __future__ import annotations

import logging
import re

from db import managed_connection


log = logging.getLogger("elixir.elder_standing")

THE_BAR = (
    "Elder is earned, and held, by what you put in — and it's all in your control: "
    "play Clan Wars (the biggest way you help the clan), or play Ranked, and donate "
    "generously. The more you participate, the stronger your standing. Nothing about "
    "your account level or arena counts — only what you do."
)


def _why(conn, m: dict, now: str) -> str:
    """Grounded, participation-based 'why' — war decks, ranked play, donations."""
    from engine.management import WAR_RATE_WINDOW, _ranked_battles

    bits: list[str] = []
    war = m.get("war_attendance_rate")
    if war is not None:
        bits.append(f"{round(war * 100)}% war decks")
    rb = _ranked_battles(conn, m.get("tag"), now, WAR_RATE_WINDOW)
    if rb:
        bits.append(f"{rb} ranked battles")
    donations = m.get("donations_4wk_avg")
    if donations is not None:
        bits.append(f"~{round(donations)} donations/wk")
    return ", ".join(bits)


@managed_connection
def build_elder_standing(now: str | None = None, *, conn=None) -> dict:
    """Assemble the three Elder-track groups from the LIVE band position, so the
    grouping is always consistent with the numbers shown:
    - holding: current Elders secure in the band (not demotable);
    - rising: members currently at the Elder bar (in the band's top-K `should_be`);
    - stepping_down: current Elders below the band (`demotable`), each tagged with
      its reason ('outranked' = out-participated; 'abandoned' = played neither).

    NB: this is CURRENT standing from `_elder_band`, NOT the weekly hysteresis
    promote_state/demote_state. Those lag by design (and, right after a scoring
    change, are stale from the last review) — grouping on them produced the
    Waltadr-outranking-Th15_Guy contradiction. Actual promote/demote *decisions*
    still use the hysteresis states; the report just shows where things stand."""
    from engine.db import utcnow
    from engine.management import _elder_band, _elder_scores
    from storage.war_analytics import _elder_role_review

    now = now or utcnow()
    scores = _elder_scores(conn, now, now[:10])
    band = _elder_band(conn, scores, now)
    rank = band["rank"]
    reviewed = {m["tag"]: m for m in _elder_role_review(conn=conn).get("reviewed", [])}

    def entry(tag, reason=None):
        m = {**reviewed.get(tag, {}), "tag": tag}
        e = {"tag": tag, "name": scores[tag].get("name") or m.get("name") or tag,
             "why": _why(conn, m, now)}
        if reason:
            e["reason"] = reason
        return e

    def by_rank(tags):
        return sorted(tags, key=lambda t: rank.get(t, 9999))

    elders = [t for t, s in scores.items() if s["role"] == "elder"]
    members = [t for t, s in scores.items() if s["role"] == "member"]
    holding = [entry(t) for t in by_rank(e for e in elders if e not in band["demotable"])]
    stepping = [entry(t, band["demote_reasons"].get(t) or "outranked")
                for t in by_rank(e for e in elders if e in band["demotable"])]
    rising = [entry(t) for t in by_rank(m for m in members if m in band["should_be"])]
    return {
        "composition": {"elders": len(elders), "members": len(members)},
        "holding": holding,
        "rising": rising,
        "stepping_down": stepping,
    }


# ── Deterministic render (grounded fallback; no Discord tables) ─────────────

def _bullets(members: list[dict]) -> str:
    return "\n".join(
        f"• **{m['name']}**" + (f" — {m['why']}" if m.get("why") else "")
        for m in members
    )


def render_elder_standing(standing: dict, *, date: str | None = None) -> str:
    """Deterministic, table-free render — the grounded fallback and the shape the
    composed version stays faithful to."""
    holding = standing.get("holding") or []
    rising = standing.get("rising") or []
    stepping = standing.get("stepping_down") or []

    parts = [
        "**Elder Standing**" + (f" — {date}" if date else ""),
        "",
        "POAP KINGS is transparent about how Elder works and where everyone stands. "
        "Here's this week's picture.",
        "",
        f"**The bar.** {THE_BAR}",
        "",
        "**Holding strong.**",
    ]
    parts += ["These Elders keep earning it:\n" + _bullets(holding)] if holding \
        else ["_(no Elders to feature this week)_"]

    parts += ["", "**On the rise.**"]
    parts += ["Trending toward Elder — keep it up:\n" + _bullets(rising)] if rising \
        else ["No one's knocking on the door just yet."]

    parts += ["", "**Stepping-down watch.**"]
    if stepping:
        parts += [
            "These Elders are on the bubble — the band is competitive and others are "
            "participating hard. Nothing is locked in; a strong week of war or ranked "
            "keeps your seat:\n" + _bullets(stepping)
        ]
    else:
        parts += ["No one's slipping — the Elder corps is solid."]

    return "\n".join(parts).strip()


# ── Facts brief for the LLM composer + grounding guard ─────────────────────

def facts_for_model(standing: dict, *, date: str | None = None) -> str:
    def _fmt(members):
        return "\n".join(
            f"- {m['name']} ({m['why'] or 'no evidence'})" for m in members
        ) or "- (none)"

    comp = standing.get("composition", {})
    return (
        f"DATE: {date or 'this week'}\n"
        f"CLAN: {comp.get('elders', 0)} elders, {comp.get('members', 0)} members.\n\n"
        f"THE BAR (what Elder takes): {THE_BAR}\n\n"
        "HOLDING STRONG — current Elders in good standing:\n"
        f"{_fmt(standing.get('holding') or [])}\n\n"
        "ON THE RISE — members trending toward Elder:\n"
        f"{_fmt(standing.get('rising') or [])}\n\n"
        "STEPPING-DOWN WATCH — Elders on the bubble because the band is competitive "
        "and others participated more (nothing enacted; a strong week recovers it):\n"
        f"{_fmt(standing.get('stepping_down') or [])}\n"
    )


def facts_member_names(standing: dict) -> set[str]:
    names: set[str] = set()
    for key in ("holding", "rising", "stepping_down"):
        for m in standing.get(key) or []:
            if m.get("name"):
                names.add(m["name"])
    return names


def output_is_grounded(text: str, standing: dict) -> bool:
    """Every member CALLED OUT must be real. Members are named in bold at the
    start of a bullet line (`- **Name** — …`); a bullet-lead bold that isn't a
    member from the facts means the LLM invented someone → reject and fall back to
    the deterministic render. (Bolded stats mid-line, e.g. **86 ranked battles**,
    are not name claims and are ignored.)"""
    allow = facts_member_names(standing)
    claimed = re.findall(r"^\s*[-•*]\s*\*\*([^*]+?)\*\*", text, re.M)
    return all(name.strip() in allow for name in claimed)


def compose_elder_standing_report(*, date: str | None = None, now: str | None = None,
                                  conn=None) -> tuple[str, str]:
    """Assemble → LLM-warm → grounding-guard → deterministic fallback. Returns
    (report_text, source) where source is 'composed' (LLM passed the guard) or
    'deterministic' (LLM missing / failed the guard). No posting here."""
    standing = build_elder_standing(now=now, conn=conn)
    deterministic = render_elder_standing(standing, date=date)
    try:
        from agent.workflows import generate_elder_standing

        composed = generate_elder_standing(facts_for_model(standing, date=date))
        if composed and output_is_grounded(composed, standing):
            return composed, "composed"
    except Exception:
        log.warning(
            "elder standing composition failed; using deterministic renderer",
            exc_info=True,
        )
    return deterministic, "deterministic"


__all__ = [
    "build_elder_standing",
    "render_elder_standing",
    "compose_elder_standing_report",
    "facts_for_model",
    "facts_member_names",
    "output_is_grounded",
    "THE_BAR",
]
