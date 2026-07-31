"""Deck Intelligence — the ``get_deck_recommendations`` capability.

The sibling of Battle Intelligence: that one reads battles you already played, this
one reads the decks you *could* play. Distinct from ``get_deck_intelligence``
(capabilities/decks.py), which reports how a member's OBSERVED decks performed — this
tool is forward-looking and recommends decks they have never played. Everything is gated by what the member OWNS and
can field at level, so no view can suggest a deck they cannot actually build.

Four modes, because members ask four different questions:

  ``upgrades``  "What can I do to improve my deck?" — ranked by usage x level gap over
                the cards they ACTUALLY field. Ranking upgrades across the whole clan
                deck space recommends cards they never play (measured: maxing the card
                that was the weak link in 2,034 clan decks moved one member's own decks
                4.50 -> 4.50, because he plays none of them).
  ``discover``  "What decks should I consider?" — for a player in a rut or one whose
                main deck is already maxed. Deliberately includes decks NOBODY in the
                clan plays; filtering to locally-played decks would recycle the ruts.
  ``war_set``   Four war decks with 32 distinct cards. Confirmed against real battles:
                89% of pairwise comparisons between a member's war decks share zero
                cards. A constraint-satisfaction problem members solve badly by hand.
  ``anchored``  "What is my best deck around <card>?" — same solver, one card pinned.

**Why deck win rates are absent here.** Measured over 12,687 clan battles: player skill
spans 36.4%-70.2%, as wide as the deck spread, and only 2 shared-deck observations exist
clan-wide — so there is no evidence a deck that works for one member transfers to
another. The same archetype label appears at 83.0% and 39.9%. Recommending on a
borrowed win rate would be reporting who played it, not what it is. Ranking here is
therefore level readiness + structural soundness, with clan usage reported only as
``fielded_by`` context, never as a quality claim.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import db as db_facade

CAPABILITY_ID = "deck_recommendations"
CONTRACT_VERSION = 1

_WAR_DECKS = 4  # a war set is four decks with no card reused
_MIN_AIR_ANSWERS = 1  # structural floor: a deck with no air answer loses on structure
_USAGE_SINCE = "2026-06-01"  # window for "cards you actually field"
_MIN_USAGE_SHARE = 0.04  # a card must be ~1 slot in 1 of 3 decks before an upgrade is advice


def _tag(value: str) -> str:
    v = (value or "").strip().upper()
    return v if v.startswith("#") else f"#{v}"


def _envelope(view: str, **extra: Any) -> dict[str, Any]:
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": view,
        **extra,
    }


def _catalog(conn) -> dict[int, dict]:
    return {
        r["card_id"]: dict(r)
        for r in conn.execute(
            "SELECT card_id, name, elixir_cost, rarity, max_level, max_evolution_level "
            "FROM card_catalog"
        )
    }


def _collection(conn, tag: str) -> dict[int, int]:
    """``{card_id: level}``. Level is RARITY-RELATIVE (legendary maxes at 8, common at
    16), so callers must compare ``max_level - level``, never the raw level."""
    return {
        r["card_id"]: r["level"]
        for r in conn.execute(
            "SELECT card_id, level FROM player_card_collection "
            "WHERE player_tag = ? AND level IS NOT NULL",
            (tag,),
        )
    }


def _owned_forms(conn, tag: str) -> dict[int, int]:
    """Deepest non-base form the member owns per card.

    The profile's ``evolutionLevel`` is NOT reliable on its own — 104 observed
    form-plays across the clan are absent from it — so it is unioned with forms the
    member has demonstrably fielded. Both are positive evidence; neither is complete.
    """
    forms: dict[int, int] = {}
    for r in conn.execute(
        "SELECT card_id, evolution_level FROM player_card_collection "
        "WHERE player_tag = ? AND evolution_level IS NOT NULL",
        (tag,),
    ):
        forms[r["card_id"]] = r["evolution_level"] or 0
    for r in conn.execute(
        "SELECT card_id, MAX(COALESCE(evolution_level, 0)) f FROM battle_card_plays "
        "WHERE side = 'member' AND player_tag = ? GROUP BY card_id",
        (tag,),
    ):
        if r["f"] > forms.get(r["card_id"], 0):
            forms[r["card_id"]] = r["f"]
    return forms


def _fielded_by(conn) -> dict[str, int]:
    """Distinct clan members who field each deck. Context, never a quality claim."""
    return {
        r["h"]: r["m"]
        for r in conn.execute(
            "SELECT e.our_deck_hash h, COUNT(DISTINCT b.player_tag) m "
            "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
            "WHERE e.our_deck_hash IS NOT NULL GROUP BY 1"
        )
    }


def _their_decks(conn, tag: str) -> dict[str, int]:
    return {
        r["h"]: r["n"]
        for r in conn.execute(
            "SELECT e.our_deck_hash h, COUNT(*) n FROM battle_enrichment e "
            "JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
            "WHERE b.player_tag = ? AND e.our_deck_hash IS NOT NULL GROUP BY 1 ORDER BY n DESC",
            (tag,),
        )
    }


def _candidates(conn, cat, own, forms, *, require_structure=True) -> list[dict]:
    """Every buildable deck: owns all 8 cards, owns each required Evo/Hero form, and
    (optionally) clears the structural floor. ``from_max`` is the rarity-independent
    readiness measure — the only cross-rarity-comparable level quantity."""
    out = []
    for r in conn.execute(
        "SELECT deck_hash, archetype, family, avg_elixir, cards_json, air_answer_count, "
        "tank_answer_count, splash_answer_count, has_big_spell, has_small_spell "
        "FROM deck_profile WHERE facts_complete = 1"
    ):
        pairs = [(p[0], p[1] or 0) for p in json.loads(r["cards_json"])]
        if any(cid not in own or cid not in cat for cid, _ in pairs):
            continue
        if any(f and forms.get(cid, 0) < f for cid, f in pairs):
            continue
        if require_structure and (
            r["air_answer_count"] < _MIN_AIR_ANSWERS or not r["has_small_spell"]
        ):
            continue
        gaps = [cat[cid]["max_level"] - own[cid] for cid, _ in pairs]
        out.append(
            {
                "deck_hash": r["deck_hash"],
                "archetype": r["archetype"],
                "family": r["family"],
                "avg_elixir": r["avg_elixir"],
                "air_answers": r["air_answer_count"],
                "has_big_spell": bool(r["has_big_spell"]),
                "has_small_spell": bool(r["has_small_spell"]),
                "cards": pairs,
                "card_ids": frozenset(cid for cid, _ in pairs),
                "levels_from_max": round(sum(gaps) / 8, 2),
                "worst_card_from_max": max(gaps),
            }
        )
    out.sort(key=lambda d: (d["levels_from_max"], d["worst_card_from_max"]))
    return out


def _describe(d: dict, cat: dict, own: dict, fielded: dict, played: set) -> dict:
    return {
        "archetype": d["archetype"],
        "family": d["family"],
        "avg_elixir": d["avg_elixir"],
        "levels_from_max": d["levels_from_max"],
        "air_answers": d["air_answers"],
        "cards": [
            {
                "name": cat[cid]["name"],
                "form": ("Evo" if f == 1 else "Hero") if f else "base",
                "level": own[cid],
                "max_level": cat[cid]["max_level"],
                "levels_from_max": cat[cid]["max_level"] - own[cid],
            }
            for cid, f in d["cards"]
        ],
        "fielded_by_members": fielded.get(d["deck_hash"], 0),
        "you_play_this": d["deck_hash"] in played,
    }


# ── mode A: upgrades ─────────────────────────────────────────────────────────
def _upgrades_view(conn, tag) -> dict[str, Any]:
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("upgrades", available=False, error="no_collection", member_tag=tag)
    usage: dict[int, int] = {
        r["card_id"]: r["n"]
        for r in conn.execute(
            "SELECT card_id, COUNT(*) n FROM battle_card_plays "
            "WHERE side = 'member' AND player_tag = ? AND battle_time >= ? GROUP BY 1",
            (tag, _USAGE_SINCE),
        )
    }
    total = sum(usage.values())
    rows = []
    for cid, n in usage.items():
        if cid not in own or cid not in cat:
            continue
        gap = cat[cid]["max_level"] - own[cid]
        if gap <= 0:
            continue
        share = n / total if total else 0.0
        rows.append(
            {
                "card": cat[cid]["name"],
                "level": own[cid],
                "max_level": cat[cid]["max_level"],
                "levels_from_max": gap,
                "usage_share": round(share, 3),
                "impact": round(share * gap, 3),
            }
        )
    rows.sort(key=lambda r: -r["impact"])
    # Materiality floor. Everyone dabbles, so *some* played card is always below max and
    # an unfiltered list hands a maxed veteran "upgrade Bandit" off 1.8% usage — advice
    # that is technically true and practically noise. A card has to be a real part of how
    # they play before upgrading it is worth saying.
    material = [r for r in rows if r["usage_share"] >= _MIN_USAGE_SHARE]
    return _envelope(
        "upgrades",
        available=True,
        member_tag=tag,
        battles_sampled=total,
        since=_USAGE_SINCE,
        upgrades=material[:8],
        all_played_cards_maxed=not rows,
        no_material_upgrades=not material,
        incidental_cards_below_max=len(rows) - len(material),
        min_usage_share=_MIN_USAGE_SHARE,
        note=(
            "Ranked by usage x levels_from_max — impact on decks this member actually "
            "fields. Levels are rarity-relative; levels_from_max is the comparable one. "
            "Cards below the usage floor are excluded as incidental. When "
            "no_material_upgrades is true, say their deck is in good shape rather than "
            "reaching for a card they barely play."
        ),
    )


# ── mode B: discover ─────────────────────────────────────────────────────────
def _meta_overlay(conn, cat, own, forms) -> list[dict]:
    """Current-meta decks (LLM + web search) the member can actually build. The clan is
    a thin slice of the meta and its data is skill-confounded, so this is the only
    source that can speak to what is strong right now rather than what is played here."""
    try:
        rows = conn.execute(
            "SELECT name, archetype, family, tier, cards_json, win_condition, note, "
            "source_url, snapshot_at FROM meta_decks "
            "WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM meta_decks)"
        ).fetchall()
    except sqlite3.OperationalError:  # hygiene: absent table is a state, not an outage
        # No snapshot table yet (pre-v31, or never refreshed). The computed suggestions
        # stand on their own and the caller sees meta_snapshot_available=false, so this
        # is reported to the reader rather than swallowed — degrade, don't fail.
        return []
    out = []
    for r in rows:
        pairs = [(p[0], p[1] or 0) for p in json.loads(r["cards_json"])]
        missing = [
            cat[cid]["name"] if cid in cat else str(cid) for cid, _ in pairs if cid not in own
        ]
        no_form = [
            f"{cat[cid]['name']} ({'Evo' if f == 1 else 'Hero'})"
            for cid, f in pairs
            if cid in cat and f and forms.get(cid, 0) < f
        ]
        gaps = [cat[cid]["max_level"] - own[cid] for cid, _ in pairs if cid in own and cid in cat]
        out.append(
            {
                "name": r["name"],
                "archetype": r["archetype"],
                "tier": r["tier"],
                "buildable": not missing and not no_form,
                "missing_cards": missing,
                "missing_forms": no_form,
                "levels_from_max": round(sum(gaps) / len(gaps), 2) if gaps else None,
                "note": r["note"],
                "source_url": r["source_url"],
                "snapshot_at": r["snapshot_at"],
            }
        )
    out.sort(
        key=lambda d: (
            not d["buildable"],
            len(d["missing_cards"]),
            d["levels_from_max"] if d["levels_from_max"] is not None else 99,
        )
    )
    return out


def _discover_view(conn, tag, limit) -> dict[str, Any]:
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("discover", available=False, error="no_collection", member_tag=tag)
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    cands = _candidates(conn, cat, own, forms)

    # One deck per archetype: a list of 8 near-identical lists is not a set of options.
    seen: set[str] = set()
    picks: list[dict] = []
    for d in cands:
        if d["deck_hash"] in played or d["family"] in seen:
            continue
        seen.add(d["family"])
        picks.append(_describe(d, cat, own, fielded, played))
        if len(picks) >= max(1, min(int(limit or 6), 12)):
            break
    meta = _meta_overlay(conn, cat, own, forms)
    return _envelope(
        "discover",
        available=True,
        member_tag=tag,
        buildable_deck_count=len(cands),
        suggestions=picks,
        meta_snapshot=meta[:8],
        meta_snapshot_available=bool(meta),
        evidence_limits=(
            "Ranked by level readiness and structural soundness, NOT by win rate: clan "
            "deck win rates are skill-confounded and do not transfer between members. "
            "fielded_by_members is context, not a quality claim. Decks nobody here plays "
            "are included deliberately."
        ),
    )


# ── modes C/D: war set and anchored ──────────────────────────────────────────
def _pick_disjoint(cands, count, *, pinned=None) -> list[dict]:
    """Greedy maximin: take the best-leveled deck, drop everything sharing a card,
    repeat. Prefers a different family each pick so the set covers varied matchups."""
    picks, used, fams = [], set(), set()
    if pinned is not None:
        picks.append(pinned)
        used |= pinned["card_ids"]
        fams.add(pinned["family"])
    for d in cands:
        if len(picks) >= count:
            break
        if d["card_ids"] & used:
            continue
        if d["family"] in fams and len(fams) < count - 1:
            continue
        picks.append(d)
        used |= d["card_ids"]
        fams.add(d["family"])
    return picks


def _war_set_view(conn, tag) -> dict[str, Any]:
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("war_set", available=False, error="no_collection", member_tag=tag)
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    cands = _candidates(conn, cat, own, forms)
    picks = _pick_disjoint(cands, _WAR_DECKS)
    if len(picks) < _WAR_DECKS:
        return _envelope(
            "war_set",
            available=False,
            member_tag=tag,
            error="no_feasible_set",
            decks_found=len(picks),
            buildable_deck_count=len(cands),
        )
    cards = set().union(*[p["card_ids"] for p in picks])
    return _envelope(
        "war_set",
        available=True,
        member_tag=tag,
        decks=[_describe(d, cat, own, fielded, played) for d in picks],
        distinct_cards=len(cards),
        worst_deck_from_max=max(p["levels_from_max"] for p in picks),
        buildable_deck_count=len(cands),
        note=(
            "Four war decks sharing no cards (confirmed rule: 89% of a member's war deck "
            "pairs share zero cards). Ranked on level readiness; no win rates are implied."
        ),
    )


def _anchored_view(conn, tag, card, limit) -> dict[str, Any]:
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("anchored", available=False, error="no_collection", member_tag=tag)
    want = (card or "").strip().lower()
    cid = next((k for k, v in cat.items() if v["name"].lower() == want), None)
    if cid is None:
        cid = next((k for k, v in cat.items() if want and want in v["name"].lower()), None)
    if cid is None:
        return _envelope("anchored", available=False, error="unknown_card", card=card)
    if cid not in own:
        return _envelope(
            "anchored",
            available=False,
            error="card_not_owned",
            card=cat[cid]["name"],
            member_tag=tag,
        )
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    cands = [d for d in _candidates(conn, cat, own, forms) if cid in d["card_ids"]]
    seen: set[str] = set()
    picks: list[dict] = []
    for d in cands:
        if d["family"] in seen and len(seen) >= 3:
            continue
        seen.add(d["family"])
        picks.append(_describe(d, cat, own, fielded, played))
        if len(picks) >= max(1, min(int(limit or 5), 10)):
            break
    return _envelope(
        "anchored",
        available=True,
        member_tag=tag,
        anchor_card=cat[cid]["name"],
        anchor_level=own[cid],
        anchor_max_level=cat[cid]["max_level"],
        buildable_decks_with_anchor=len(cands),
        decks=picks,
        note="Every deck contains the anchor card and is buildable at this member's levels.",
    )


def get_deck_recommendations(
    *,
    view: str = "discover",
    member_tag: Optional[str] = None,
    card: Optional[str] = None,
    limit: int = 6,
    source: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Deck recommendations bound by what a member owns and can field at level.

    Views: ``upgrades`` (improve the deck they play), ``discover`` (new decks worth
    considering, including ones nobody here plays), ``war_set`` (four decks, 32 distinct
    cards), ``anchored`` (best deck around ``card``). All need ``member_tag``. Read-only.
    """
    if view not in {"upgrades", "discover", "war_set", "anchored"}:
        return _envelope(view, available=False, error="unsupported_view")
    if not member_tag:
        return _envelope(view, available=False, error="member_tag_required")
    tag = _tag(member_tag)
    own_conn = conn is None
    if conn is None:
        provider = source or db_facade
        conn = provider.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        if view == "upgrades":
            return _upgrades_view(conn, tag)
        if view == "war_set":
            return _war_set_view(conn, tag)
        if view == "anchored":
            return _anchored_view(conn, tag, card, limit)
        return _discover_view(conn, tag, limit)
    finally:
        if own_conn:
            conn.close()
