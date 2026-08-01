"""Deck Intelligence — the ``get_deck_recommendations`` capability.

The sibling of Battle Intelligence: that one reads battles you already played, this
one reads the decks you *could* play. Distinct from ``get_deck_intelligence``
(capabilities/decks.py), which reports how a member's OBSERVED decks performed — this
tool is forward-looking and recommends decks they have never played. Everything is gated by what the member OWNS and
can field at level, so no view can suggest a deck they cannot actually build.

Five modes, because members ask five different questions:

  ``upgrades``  "What can I do to improve my deck?" — ranked by usage x level gap over
                the cards they ACTUALLY field. Ranking upgrades across the whole clan
                deck space recommends cards they never play (measured: maxing the card
                that was the weak link in 2,034 clan decks moved one member's own decks
                4.50 -> 4.50, because he plays none of them).
  ``discover``  "What decks should I consider?" — for a player in a rut or one whose
                main deck is already maxed. Deliberately includes decks NOBODY in the
                clan plays; filtering to locally-played decks would recycle the ruts.
  ``build``     "Build me N decks, one around each of these cards." Exactly what was
                asked for, nothing disjoint. This request used to be answered with a
                war set, and the constraint cost the member the deck he wanted: his
                best Bowler deck (0.12 from max, four air answers, three of them
                troops) was replaced by one at 0.25 with three, two of them spells.
  ``war_set``   Four war decks with 32 distinct cards. Confirmed against real battles:
                89% of pairwise comparisons between a member's war decks share zero
                cards. A constraint-satisfaction problem members solve badly by hand.
                ONLY for an explicit war request — the no-overlap rule makes every
                individual deck weaker, so it is never the helpful default.
  ``anchored``  "What is my best deck around <card>?" — same solver, one card pinned.

Plus ``read_deck_link`` for the inbound direction: a deck the member pasted, read
through the same role vocabulary a suggestion gets.

**What each deck explains about itself.** Every returned deck carries role_coverage
(which card fills each slot of the community deck formula, air answers separated into
troops vs spells vs heavy) and per-card roles, so the answer can teach the formula
rather than hand over eight names. These were computed and discarded for months while
the model narrated deck construction from its own memory.

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
from engine import card_roles, deck_links

CAPABILITY_ID = "deck_recommendations"
CONTRACT_VERSION = 1

_WAR_DECKS = 4  # a war set is four decks with no card reused
_USAGE_SINCE = "2026-06-01"  # window for "cards you actually field"
_FAMILIARITY_SLACK = 1.0  # levels_from_max a KNOWN deck may concede and still be picked
_MIN_USAGE_SHARE = 0.04  # a card must be ~1 slot in 1 of 3 decks before an upgrade is advice


def _tag(value) -> str:
    # str() coercion, not `value or ""`: a model can hand back a bare numeric tag, and
    # an AttributeError here surfaces as a dead tool call rather than a clean answer.
    v = str(value or "").strip().upper()
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


def _card_facts(conn, cat: dict) -> dict[tuple[int, int], dict]:
    """``{(card_id, form): fact}`` with ``name``/``elixir_cost`` merged in.

    177 rows — loaded once per call and read from memory. This is deliberately NOT a
    schema change: the role counts could be columns on deck_profile, but a migration
    applies itself to whichever database the first process touches, and the running
    build then fails every tick until it is restarted. Computing here costs nothing
    measurable and cannot take production down.
    """
    out: dict[tuple[int, int], dict] = {}
    try:
        rows = conn.execute("SELECT * FROM card_facts").fetchall()
    except sqlite3.OperationalError:  # hygiene: absent table is a state, not an outage
        # No enrichment yet. Every other field this capability returns still stands;
        # role_coverage reports itself incomplete rather than taking the view down.
        return out
    for r in rows:
        fact = dict(r)
        meta = cat.get(fact["card_id"]) or {}
        fact["name"] = meta.get("name")
        fact["elixir_cost"] = meta.get("elixir_cost")
        out[(fact["card_id"], fact["evolution_level"] or 0)] = fact
    return out


def _facts_for(deck_cards, facts: dict) -> list[Optional[dict]]:
    """Facts for a deck's 8 (card_id, form) pairs, falling back to the base form.

    An Evo/Hero form inherits its base card's role — evolution amplifies a role, it
    does not create a new one — so a missing form row is filled from base rather than
    dropping the card and silently understating the deck's coverage.
    """
    return [facts.get((cid, form)) or facts.get((cid, 0)) for cid, form in deck_cards]


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


def _played_archetypes(conn, tag: str) -> frozenset:
    """Archetypes this member has actually fielded — the grain at which a suggestion
    reads as new to them. Deck-hash novelty is invisible to a player: two lists that
    differ by one card are the same deck as far as anyone piloting it is concerned."""
    return frozenset(
        r["archetype"]
        for r in conn.execute(
            "SELECT DISTINCT dp.archetype FROM battle_enrichment e "
            "JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
            "JOIN deck_profile dp ON dp.deck_hash = e.our_deck_hash "
            "WHERE b.player_tag = ? AND e.our_deck_hash IS NOT NULL",
            (tag,),
        )
    )


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
        # The air floor scales with the deck's own cost: guides ask for 2-3 air answers
        # and exempt very cheap cycle decks, which defend by rotating rather than by
        # holding. The old flat floor of 1 passed decks whose only air answer was a
        # single spell. Cost of the tighter floor, measured over the corpus: 1.0%.
        if require_structure and (
            r["air_answer_count"] < card_roles.min_air_answers(r["avg_elixir"])
            or not r["has_small_spell"]
        ):
            continue
        # Slot legality (Evo + Hero + Wild = 3). Every profiled deck was observed in a
        # real battle so this cannot fire today; it exists so that a deck assembled by
        # any future code path can never be recommended in an unfieldable shape.
        if sum(1 for _, f in pairs if f) > card_roles.MAX_SPECIAL_SLOTS:
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


def _describe(
    d: dict,
    cat: dict,
    own: dict,
    fielded: dict,
    played: set,
    facts: dict,
    played_archetypes: frozenset = frozenset(),
) -> dict:
    """One deck, with the reason each card is in it.

    The role fields are the point of this function. Every one of them was already
    being computed and then dropped before it reached the reader, which left the
    model to narrate deck construction from its own memory — ungrounded, and it
    taught the player nothing they could reuse.
    """
    deck_facts = _facts_for(d["cards"], facts)
    coverage = card_roles.deck_role_coverage(
        deck_facts, family=d["family"], avg_elixir=d["avg_elixir"]
    )
    return {
        "archetype": d["archetype"],
        "family": d["family"],
        "avg_elixir": d["avg_elixir"],
        "levels_from_max": d["levels_from_max"],
        "air_answers": d["air_answers"],
        "role_coverage": coverage,
        "cards": [
            {
                "name": cat[cid]["name"],
                "form": ("Evo" if f == 1 else "Hero") if f else "base",
                "level": own[cid],
                "max_level": cat[cid]["max_level"],
                "levels_from_max": cat[cid]["max_level"] - own[cid],
                "elixir_cost": cat[cid].get("elixir_cost"),
                "roles": _card_roles(fact),
                "note": (fact or {}).get("note"),
            }
            for (cid, f), fact in zip(d["cards"], deck_facts, strict=True)
        ],
        # A tappable link beats eight card names the member has to find by hand. It
        # carries base cards only — the game's own share format cannot express Evo or
        # Hero form — so `link_omits_forms` tells the caller when the deck it is
        # describing depends on a form the link will silently drop.
        "copy_link": deck_links.build_deck_link(cid for cid, _ in d["cards"]),
        "link_omits_forms": [cat[cid]["name"] for cid, form in d["cards"] if form and cid in cat],
        "fielded_by_members": fielded.get(d["deck_hash"], 0),
        "you_play_this": d["deck_hash"] in played,
        # Exact-hash novelty answers the wrong question. A member was told he had
        # "not fielded this exact combo yet" about Royal Hogs — an archetype he had
        # played 21 times that month. Novelty a player recognizes lives at the
        # archetype level, so report both and let the caller say the true one.
        "you_play_this_archetype": d["archetype"] in played_archetypes,
    }


def _card_roles(fact: Optional[dict]) -> list[str]:
    """Every formula slot this card fills — plural by design. Valkyrie is a mini-tank
    AND a splash answer AND an anti-swarm card, and a single label hides two of the
    three reasons she might be in the list."""
    if not fact:
        return []
    roles = []
    if fact.get("is_win_condition"):
        roles.append("win condition")
    if card_roles.is_air_troop(fact):
        roles.append("heavy air answer" if card_roles.is_heavy_air_answer(fact) else "air answer")
    elif card_roles.is_air_answer(fact):
        roles.append("air answer (spell)")
    if card_roles.is_tank_answer(fact):
        roles.append("tank answer")
    if card_roles.is_splash_answer(fact):
        roles.append("splash")
    if card_roles.is_swarm(fact):
        roles.append("swarm")
    if card_roles.is_cycle_card(fact, fact.get("elixir_cost")):
        roles.append("cycle")
    if fact.get("spell_tier") in ("big", "small", "medium"):
        roles.append(f"{fact['spell_tier']} spell")
    base = fact.get("role")
    if base in ("tank", "mini_tank", "building", "spawner", "support") and base not in roles:
        roles.append(base.replace("_", "-"))
    return roles


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
    facts = _card_facts(conn, cat)
    played_arch = _played_archetypes(conn, tag)
    cands = _candidates(conn, cat, own, forms)

    # One deck per archetype: a list of 8 near-identical lists is not a set of options.
    seen: set[str] = set()
    picks: list[dict] = []
    for d in cands:
        if d["deck_hash"] in played or d["family"] in seen:
            continue
        seen.add(d["family"])
        picks.append(_describe(d, cat, own, fielded, played, facts, played_arch))
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
def _pick_disjoint(cands, count, *, pinned=None, played=frozenset()) -> list[dict]:
    """Greedy maximin: take the best-leveled deck, drop everything sharing a card,
    repeat. Prefers a different family each pick so the set covers varied matchups.

    Familiarity breaks near-ties. War is the worst place to hand someone four decks
    they have never piloted — this view once gave a member four unplayed lists while
    the deck he actually runs sat just outside. A deck he knows wins unless it is more
    than ``_FAMILIARITY_SLACK`` levels behind on readiness.
    """
    ranked = sorted(
        cands,
        key=lambda d: (
            d["levels_from_max"] - (_FAMILIARITY_SLACK if d["deck_hash"] in played else 0.0),
            d["worst_card_from_max"],
        ),
    )
    picks, used, fams = [], set(), set()
    if pinned is not None:
        picks.append(pinned)
        used |= pinned["card_ids"]
        fams.add(pinned["family"])
    for d in ranked:
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


def _resolve_card(cat, own, name) -> tuple[Optional[int], Optional[dict]]:
    """``(card_id, None)`` or ``(None, error_fields)``. Exact name first, then substring."""
    want = str(name or "").strip().lower()
    cid = next((k for k, v in cat.items() if v["name"].lower() == want), None)
    if cid is None:
        cid = next((k for k, v in cat.items() if want and want in v["name"].lower()), None)
    if cid is None:
        return None, {"error": "unknown_card", "card": name}
    if cid not in own:
        return None, {"error": "card_not_owned", "card": cat[cid]["name"]}
    return cid, None


def _build_view(conn, tag, anchors, count) -> dict[str, Any]:
    """Exactly the decks the player asked for — one per anchor card, no war constraint.

    This view exists because we had no way to answer "build me 2 decks, one around
    Bowler and one around Balloon". The request routed to ``war_set``, which returned
    four decks sharing no cards, and the 32-card disjointness DOWNGRADED both decks the
    player actually wanted: the best Bowler deck available to that member sat at 0.12
    levels from max with four air answers, and what he was handed instead was 0.25 from
    max with three, two of them spells. Cards were removed from his deck to make room
    for decks he never asked about.

    War is a specific request. Nothing here is disjoint unless the caller asks for a
    war set, and ``war_set`` remains the view that does.
    """
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("build", available=False, error="no_collection", member_tag=tag)
    names = [a for a in (anchors or []) if str(a or "").strip()]
    resolved: list[int] = []
    unresolved: list[dict] = []
    for name in names:
        cid, err = _resolve_card(cat, own, name)
        if err is not None or cid is None:
            unresolved.append(err or {"error": "unknown_card", "card": name})
        elif cid not in resolved:
            resolved.append(cid)
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    facts = _card_facts(conn, cat)
    played_arch = _played_archetypes(conn, tag)
    cands = _candidates(conn, cat, own, forms)
    if not cands:
        return _envelope(
            "build", available=False, error="no_buildable_decks", member_tag=tag, decks=[]
        )

    wanted = max(1, min(int(count or len(resolved) or 1), 6))
    picks: list[dict] = []
    used_hashes: set[str] = set()
    # One deck per anchor, best-first. Anchors are honoured in the order asked.
    for cid in resolved:
        best = next(
            (d for d in cands if cid in d["card_ids"] and d["deck_hash"] not in used_hashes),
            None,
        )
        if best is None:
            unresolved.append({"error": "no_buildable_deck_with_card", "card": cat[cid]["name"]})
            continue
        used_hashes.add(best["deck_hash"])
        picks.append(
            _describe(best, cat, own, fielded, played, facts, played_arch)
            | {"anchor_card": cat[cid]["name"]}
        )
    # Only fill past the anchors when MORE decks were asked for than cards named.
    seen_fams = {p["family"] for p in picks}
    for d in cands:
        if len(picks) >= wanted:
            break
        if d["deck_hash"] in used_hashes or d["family"] in seen_fams:
            continue
        used_hashes.add(d["deck_hash"])
        seen_fams.add(d["family"])
        picks.append(_describe(d, cat, own, fielded, played, facts, played_arch))
    return _envelope(
        "build",
        available=True,
        member_tag=tag,
        requested_count=wanted,
        anchors=[cat[c]["name"] for c in resolved],
        unresolved=unresolved,
        decks=picks[:wanted],
        buildable_deck_count=len(cands),
        note=(
            "Decks built to the request. These are NOT a war set — they may share cards, "
            "which is fine everywhere except Clan Wars. Offer war_set only if the player "
            "asks for war decks."
        ),
    )


def _war_set_view(conn, tag) -> dict[str, Any]:
    cat = _catalog(conn)
    own = _collection(conn, tag)
    if not own:
        return _envelope("war_set", available=False, error="no_collection", member_tag=tag)
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    facts = _card_facts(conn, cat)
    played_arch = _played_archetypes(conn, tag)
    cands = _candidates(conn, cat, own, forms)
    picks = _pick_disjoint(cands, _WAR_DECKS, played=frozenset(played))
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
        decks=[_describe(d, cat, own, fielded, played, facts, played_arch) for d in picks],
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
    if isinstance(card, (list, tuple)):
        card = card[0] if card else ""
    cid, err = _resolve_card(cat, own, card)
    if err is not None or cid is None:
        return _envelope(
            "anchored",
            available=False,
            member_tag=tag,
            **(err or {"error": "unknown_card", "card": card}),
        )
    forms = _owned_forms(conn, tag)
    played = set(_their_decks(conn, tag))
    fielded = _fielded_by(conn)
    facts = _card_facts(conn, cat)
    played_arch = _played_archetypes(conn, tag)
    cands = [d for d in _candidates(conn, cat, own, forms) if cid in d["card_ids"]]
    seen: set[str] = set()
    picks: list[dict] = []
    for d in cands:
        if d["family"] in seen and len(seen) >= 3:
            continue
        seen.add(d["family"])
        picks.append(_describe(d, cat, own, fielded, played, facts, played_arch))
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
    anchors: Optional[list] = None,
    count: Optional[int] = None,
    limit: int = 6,
    source: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Deck recommendations bound by what a member owns and can field at level.

    Views: ``upgrades`` (improve the deck they play), ``discover`` (new decks worth
    considering, including ones nobody here plays), ``build`` (exactly N decks, one per
    card in ``anchors`` — the right view for "build me 2 decks around X and Y"),
    ``war_set`` (four decks, 32 distinct cards — ONLY when war is asked for),
    ``anchored`` (best deck around a single ``card``). All need ``member_tag``.
    Read-only.
    """
    if view not in {"upgrades", "discover", "war_set", "anchored", "build"}:
        return _envelope(view, available=False, error="unsupported_view")
    if not member_tag:
        return _envelope(view, available=False, error="member_tag_required")
    tag = _tag(member_tag)
    # Several cards named for one deck-shaped question is a `build`, not an `anchored`.
    # `anchored` used to take card[0] and silently drop the rest, so "a Bowler deck and
    # a Balloon deck" quietly became a Bowler deck.
    if view == "anchored" and isinstance(card, (list, tuple)) and len(card) > 1:
        view, anchors = "build", list(card)
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
        if view == "build":
            return _build_view(conn, tag, anchors or ([card] if card else []), count)
        if view == "anchored":
            return _anchored_view(conn, tag, card, limit)
        return _discover_view(conn, tag, limit)
    finally:
        if own_conn:
            conn.close()


# ── inbound: a deck the member pasted ────────────────────────────────────────
def read_deck_link(
    *,
    link: Optional[str] = None,
    member_tag: Optional[str] = None,
    source: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Resolve a deck a member pasted into the same read a suggestion gets.

    The other half of the share loop: Elixir hands out links, so members will hand
    them back, and "here's my deck" should not require typing eight card names.
    Deliberately the SAME role_coverage the recommendation views return, so a deck
    the member brought and a deck Elixir proposed are analysed by one code path and
    describe themselves in the same vocabulary.

    Card levels are filled in when ``member_tag`` is given and the member owns the
    card; a pasted deck may well be someone else's, so an unowned card is reported
    as unowned rather than as a gap in their collection.
    """
    parsed = deck_links.parse_deck_link(link)
    if not parsed:
        return _envelope("pasted_deck", available=False, error="no_deck_link_found")
    own_conn = conn is None
    if conn is None:
        provider = source or db_facade
        conn = provider.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cat = _catalog(conn)
        facts = _card_facts(conn, cat)
        unknown = [cid for cid in parsed["card_ids"] if cid not in cat]
        if unknown:
            return _envelope(
                "pasted_deck", available=False, error="unknown_card_ids", card_ids=unknown
            )
        tag = _tag(member_tag) if member_tag else None
        own = _collection(conn, tag) if tag else {}
        pairs = [(cid, 0) for cid in parsed["card_ids"]]
        deck_facts = _facts_for(pairs, facts)
        elixirs = [cat[cid].get("elixir_cost") or 0 for cid in parsed["card_ids"]]
        avg_elixir = round(sum(elixirs) / len(elixirs), 2) if all(elixirs) else None
        coverage = card_roles.deck_role_coverage(deck_facts, family=None, avg_elixir=avg_elixir)
        tt = parsed["tower_troop_id"]
        return _envelope(
            "pasted_deck",
            available=True,
            member_tag=tag,
            avg_elixir=avg_elixir,
            role_coverage=coverage,
            tower_troop=(cat.get(tt) or {}).get("name") if tt else None,
            shared_by_tag=parsed["shared_by_tag"],
            cards=[
                {
                    "name": cat[cid]["name"],
                    "elixir_cost": cat[cid].get("elixir_cost"),
                    "rarity": cat[cid].get("rarity"),
                    "roles": _card_roles(fact),
                    "note": (fact or {}).get("note"),
                    "their_level": own.get(cid),
                    "owned_by_them": cid in own if tag else None,
                    "levels_from_max": (cat[cid]["max_level"] - own[cid]) if cid in own else None,
                }
                for cid, fact in zip(parsed["card_ids"], deck_facts, strict=True)
            ],
            note=(
                "Read from a Clash Royale share link. The link format carries BASE CARDS "
                "ONLY — it cannot express Evolution or Hero forms, so do not state which "
                "cards are evolved and ask if it matters. Card levels shown are this "
                "member's own; a pasted deck may belong to someone else."
            ),
        )
    finally:
        if own_conn:
            conn.close()
