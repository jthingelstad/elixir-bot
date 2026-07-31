"""Battle Intelligence — the ``get_battle_intelligence`` capability (Feature 1).

Computed-only views over ``battle_card_plays`` + ``battle_enrichment`` (both
filled by the Stage-A worker, no model): per-card matchup win rates, a member's
nemesis cards, a battle's closeness/margin read, and a member rollup. Everything
is form-aware — grouped on ``(card_id, evolution_level)`` so "Evo Knight" and
"Knight" are different cards.

Statistical floors live HERE, not in the prompt: a card/matchup win-rate claim
needs n>=30, else the view returns ``insufficient_sample`` with the real n and no
weak number. Features 2-3 extend the same tool with deck/matchup/prose views.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

import db as db_facade
from engine.deck_hash import card_form

CAPABILITY_ID = "battle_intelligence"
CONTRACT_VERSION = 1
_N_FLOOR = 30  # min encounters for a win-rate CLAIM (not for classification)


def _since(days) -> Optional[str]:
    """UTC cutoff for a `days` window, or None for all-time.

    Every view was previously an undefined "recent", so "am I improving?" and "how did
    I do this week?" were unanswerable. Views now report the window they used.
    """
    if not days:
        return None
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days), 365))
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# scope predicates. "competitive" alone covered 96% of battles (is_competitive
# includes ladder), so war and ranked were impossible to isolate — the #1 complaint
# from the question audit. These map to the same flags the rest of the codebase uses.
_SCOPES = {
    "all": "1=1",
    "competitive": "p.is_competitive = 1",  # denormalized onto battle_card_plays
    # battle_card_plays only carries is_competitive, so the mode-specific scopes
    # resolve against battle_events via the join below.
    "war": "b.is_war = 1",
    "ranked": "b.is_ranked = 1",
    "ladder": "b.is_ladder = 1",
}
# Scopes that need the battle_events join (the plays table lacks these flags).
_SCOPES_NEEDING_JOIN = {"war", "ranked", "ladder"}


def _plays_from(scope: str) -> str:
    """FROM clause for card/nemesis: join battle_events only when the scope needs it."""
    if scope in _SCOPES_NEEDING_JOIN:
        return "battle_card_plays p JOIN battle_events b ON b.dedup_key = p.battle_dedup_key"
    return "battle_card_plays p"


def _tag(value: str) -> str:
    clean = str(value or "").strip().upper()
    return clean if clean.startswith("#") else f"#{clean}"


def _card_label(name: Optional[str], evolution_level) -> str:
    base = name or "Unknown"
    form = card_form(evolution_level)
    return {"evo": f"Evo {base}", "hero": f"Hero {base}"}.get(form, base)


def _card_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(r[0]): r[1] for r in conn.execute("SELECT card_id, name FROM card_catalog")}


def _win_rate(w: int, losses: int) -> Optional[float]:
    decided = w + losses
    return round(w / decided, 3) if decided else None


def _resolve_card_id(conn: sqlite3.Connection, card: str) -> Optional[int]:
    row = conn.execute(
        "SELECT card_id FROM card_catalog WHERE name = ? COLLATE NOCASE", (card,)
    ).fetchone()
    return int(row[0]) if row else None


def _envelope(view: str, **extra: Any) -> dict[str, Any]:
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": view,
        **extra,
    }


def _card_view(conn, tag, card, scope, days=None) -> dict[str, Any]:
    if not card:
        return _envelope("card", available=False, error="card_required")
    card_id = _resolve_card_id(conn, card)
    if card_id is None:
        return _envelope("card", available=False, error="unknown_card", card=card)
    predicate = _SCOPES.get(scope, _SCOPES["all"])
    where = [f"p.card_id = {card_id}", predicate]
    params: list = []
    cutoff = _since(days)
    if cutoff:
        where.append("battle_time >= ?")
        params.append(cutoff)
    subject = "clan"
    if tag:
        where.append("p.player_tag = ?")
        params.append(tag)
        subject = tag
    rows = conn.execute(
        f"SELECT p.side, p.evolution_level, "
        f"SUM(p.outcome = 'W') w, SUM(p.outcome = 'L') l, COUNT(*) n "
        f"FROM {_plays_from(scope)} WHERE {' AND '.join(where)} "
        f"GROUP BY p.side, p.evolution_level",
        tuple(params),
    ).fetchall()
    names = _card_names(conn)
    playing: list[dict] = []
    facing: list[dict] = []
    for side, evo, w, losses, n in rows:
        entry = {
            "card": _card_label(names.get(card_id), evo),
            "n": n,
            "win_rate": _win_rate(w, losses) if n >= _N_FLOOR else None,
            "insufficient_sample": n < _N_FLOOR,
        }
        (playing if side == "member" else facing).append(entry)
    return _envelope(
        "card",
        available=True,
        subject=subject,
        card=card,
        scope=scope,
        window_days=days,
        playing=sorted(playing, key=lambda e: -e["n"]),
        facing=sorted(facing, key=lambda e: -e["n"]),
        note=(
            "Win rate omitted below n=30 (insufficient_sample). Intra-clan battles "
            "can double-count in clan-wide aggregates."
        ),
    )


def _nemesis_view(conn, tag, scope, days=None) -> dict[str, Any]:
    predicate = _SCOPES.get(scope, _SCOPES["all"])
    where = ["p.side = 'opponent'", predicate]
    params: list = []
    if tag:
        where.append("p.player_tag = ?")
        params.append(tag)
    cutoff = _since(days)
    if cutoff:
        where.append("p.battle_time >= ?")
        params.append(cutoff)
    rows = conn.execute(
        f"SELECT p.card_id, p.evolution_level, SUM(p.outcome = 'W') w, "
        f"SUM(p.outcome = 'L') l, COUNT(*) n FROM {_plays_from(scope)} "
        f"WHERE {' AND '.join(where)} "
        f"GROUP BY p.card_id, p.evolution_level HAVING n >= ? ORDER BY (1.0 * w / (w + l)) ASC",
        (*params, _N_FLOOR),
    ).fetchall()
    names = _card_names(conn)
    nemeses = [
        {
            "card": _card_label(names.get(cid), evo),
            "n": n,
            "member_win_rate": _win_rate(w, losses),
        }
        for cid, evo, w, losses, n in rows
        if _win_rate(w, losses) is not None
    ]
    return _envelope(
        "nemesis",
        available=True,
        subject=tag or "clan",
        scope=scope,
        window_days=days,
        nemeses=nemeses[:10],
        note="Opponent card-forms the member faces most poorly (n>=30, member win rate).",
    )


def _battle_view(conn, tag, limit) -> dict[str, Any]:
    if not tag:
        return _envelope("battle", available=False, error="member_tag_required")
    rows = conn.execute(
        "SELECT e.battle_time, b.outcome, b.opponent_name, b.game_mode_name, "
        "e.hp_margin, e.closeness, e.discipline_delta, e.level_gap, "
        "e.our_deck_hash, e.their_deck_hash, e.expected_advantage, e.performance, "
        "e.commentary, e.loss_nature, e.notable, e.confidence, "
        "op.archetype our_archetype, tp.archetype their_archetype, b.is_ranked "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        "LEFT JOIN deck_profile op ON op.deck_hash = e.our_deck_hash "
        "LEFT JOIN deck_profile tp ON tp.deck_hash = e.their_deck_hash "
        "WHERE e.player_tag = ? ORDER BY e.battle_time DESC LIMIT ?",
        (tag, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    closeness_word = {0: "stomp", 1: "clear", 2: "close", 3: "squeaker"}
    perf_word = {1: "upset win", -1: "underperformed", 0: "as expected"}
    battles = []
    for r in rows:
        battles.append(
            {
                "battle_time": r["battle_time"],
                "outcome": r["outcome"],
                "opponent": r["opponent_name"],
                "mode": r["game_mode_name"],
                "our_archetype": r["our_archetype"],
                "their_archetype": r["their_archetype"],
                "hp_margin": r["hp_margin"],
                "closeness": closeness_word.get(r["closeness"])
                if r["closeness"] is not None
                else None,
                "discipline_delta": r["discipline_delta"],
                "level_gap": None if r["is_ranked"] else r["level_gap"],
                "level_note": "levels_normalized" if r["is_ranked"] else None,
                "expected_advantage": r["expected_advantage"],
                "vs_expectation": perf_word.get(r["performance"])
                if r["performance"] is not None
                else None,
                "commentary": r["commentary"],
                "loss_nature": r["loss_nature"],
                "notable": bool(r["notable"]) if r["notable"] is not None else None,
                "our_deck_hash": r["our_deck_hash"],
                "their_deck_hash": r["their_deck_hash"],
            }
        )
    return _envelope(
        "battle",
        available=bool(battles),
        subject=tag,
        battles=battles,
        note="commentary is present only for allowlisted members (gated); NULL means "
        "prose was not generated for this member, not that the battle was unremarkable. "
        "closeness/hp_margin need both sides' tower data.",
    )


def _member_summary_view(conn, tag, days=None) -> dict[str, Any]:
    if not tag:
        return _envelope("member_summary", available=False, error="member_tag_required")
    agg = conn.execute(
        "SELECT COUNT(*) battles, "
        "AVG(discipline_delta) avg_discipline, "
        "SUM(closeness = 0) stomps, SUM(closeness = 3) squeakers, "
        "COUNT(hp_margin) with_tower_data "
        "FROM battle_enrichment WHERE player_tag = ? "
        + ("AND battle_time >= ? " if _since(days) else ""),
        (tag, _since(days)) if _since(days) else (tag,),
    ).fetchone()
    if not agg or agg["battles"] == 0:
        return _envelope("member_summary", available=False, subject=tag, error="no_battles")
    cards = conn.execute(
        "SELECT card_id, evolution_level, SUM(outcome = 'W') w, SUM(outcome = 'L') l, COUNT(*) n "
        "FROM battle_card_plays WHERE player_tag = ? AND side = 'member' "
        "GROUP BY card_id, evolution_level HAVING n >= ? ORDER BY (1.0 * w / (w + l)) DESC",
        (tag, _N_FLOOR),
    ).fetchall()
    names = _card_names(conn)
    ranked = [
        {"card": _card_label(names.get(cid), evo), "n": n, "win_rate": _win_rate(w, losses)}
        for cid, evo, w, losses, n in cards
        if _win_rate(w, losses) is not None
    ]
    # Prose (present only for allowlisted members): a few recent lines + the
    # loss_nature mix. Absent for non-allowlisted members (computed rollup still returns).
    recent_commentary = [
        row["commentary"]
        for row in conn.execute(
            "SELECT commentary FROM battle_enrichment WHERE player_tag = ? AND commentary IS NOT NULL "
            "ORDER BY battle_time DESC LIMIT 3",
            (tag,),
        ).fetchall()
    ]
    loss_nature_mix = dict(
        conn.execute(
            "SELECT loss_nature, COUNT(*) FROM battle_enrichment WHERE player_tag = ? "
            "AND loss_nature IS NOT NULL GROUP BY loss_nature",
            (tag,),
        ).fetchall()
    )
    # (3) Clan-relative context: "am I above average?" needed a distribution, not a
    # rollup. Win rate is computed over the same window so the comparison is fair.
    cutoff = _since(days)
    peers = conn.execute(
        "SELECT e.player_tag, SUM(b.outcome = 'W') w, SUM(b.outcome = 'L') l "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        "JOIN clan_memberships cm ON cm.player_tag = e.player_tag AND cm.left_at IS NULL "
        + ("WHERE e.battle_time >= ? " if cutoff else "")
        + "GROUP BY e.player_tag HAVING (w + l) >= 20",
        (cutoff,) if cutoff else (),
    ).fetchall()
    rates = sorted(r["w"] / (r["w"] + r["l"]) for r in peers if (r["w"] + r["l"]))
    mine = next(
        (r["w"] / (r["w"] + r["l"]) for r in peers if r["player_tag"] == tag and (r["w"] + r["l"])),
        None,
    )
    standing = None
    if mine is not None and len(rates) >= 5:
        below = sum(1 for x in rates if x < mine)
        standing = {
            "win_rate": round(mine, 3),
            "clan_median_win_rate": round(rates[len(rates) // 2], 3),
            "percentile": round(100 * below / len(rates)),
            "ranked_members": len(rates),
            "basis": "active members with 20+ battles in the same window",
        }

    return _envelope(
        "member_summary",
        available=True,
        subject=tag,
        window_days=days,
        clan_standing=standing,
        battles=agg["battles"],
        battles_with_tower_data=agg["with_tower_data"],
        stomps=agg["stomps"],
        squeakers=agg["squeakers"],
        avg_discipline_delta=round(agg["avg_discipline"], 2)
        if agg["avg_discipline"] is not None
        else None,
        best_cards=ranked[:5],
        worst_cards=ranked[5:][::-1][:5],  # ranked 6th-onward, worst first; no overlap with best
        recent_commentary=recent_commentary,  # empty for non-allowlisted members
        loss_nature_mix=loss_nature_mix,
        note="Card win rates are member-plays-it, n>=30. commentary/loss_nature present "
        "only for allowlisted members; their absence is not 'no issues'.",
    )


_FAMILIES = {"beatdown", "control", "cycle", "bait", "bridge spam", "siege"}


def _matchup_view(conn, our_family, their_family) -> dict[str, Any]:
    """Measured family matchup advantages (-2..+2 from clan win rates, n-gated)."""
    where, params = [], []
    for col, val in (("our_family", our_family), ("their_family", their_family)):
        if val:
            if val not in _FAMILIES:
                return _envelope("matchup", available=False, error="unknown_family", value=val)
            where.append(f"{col} = ?")
            params.append(val)
    sql = (
        "SELECT our_family, their_family, advantage, measured_win_rate, n FROM matchup_expectation"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY advantage DESC, n DESC"
    cells = [
        {
            "our_family": r["our_family"],
            "their_family": r["their_family"],
            "advantage": r["advantage"],
            "measured_win_rate": r["measured_win_rate"],
            "n": r["n"],
            "low_confidence": r["n"] < _N_FLOOR,
        }
        for r in conn.execute(sql, tuple(params)).fetchall()
    ]
    return _envelope(
        "matchup",
        available=bool(cells),
        cells=cells,
        note="advantage: +2 strongly favored .. -2 strongly against, from MEASURED "
        "clan outcomes (member perspective). low_confidence flags n<30.",
    )


def _deck_view(conn, tag) -> dict[str, Any]:
    """A member's observed decks: rules archetype/family, avg elixir, record."""
    if not tag:
        return _envelope("deck", available=False, error="member_tag_required")
    rows = conn.execute(
        "SELECT dp.archetype, dp.family, dp.avg_elixir, "
        "SUM(b.outcome = 'W') w, SUM(b.outcome = 'L') l, COUNT(*) n "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        "JOIN deck_profile dp ON dp.deck_hash = e.our_deck_hash "
        "WHERE e.player_tag = ? GROUP BY e.our_deck_hash ORDER BY n DESC LIMIT 10",
        (tag,),
    ).fetchall()
    decks = [
        {
            "archetype": r["archetype"],
            "family": r["family"],
            "avg_elixir": r["avg_elixir"],
            "battles": r["n"],
            "win_rate": _win_rate(r["w"], r["l"]),
        }
        for r in rows
    ]
    return _envelope("deck", available=bool(decks), subject=tag, decks=decks)


def get_battle_intelligence(
    *,
    view: str = "battle",
    member_tag: Optional[str] = None,
    card: Optional[str] = None,
    our_family: Optional[str] = None,
    their_family: Optional[str] = None,
    scope: str = "all",
    limit: int = 20,
    days: Optional[int] = None,
    source: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Computed battle intelligence. Views: ``card`` (needs ``card``, optional
    ``member_tag`` else clan-wide), ``nemesis`` (optional ``member_tag``),
    ``battle``/``member_summary``/``deck`` (need ``member_tag``), ``matchup``
    (optional ``our_family``/``their_family`` else the full matrix). Read-only."""
    if view not in {
        "card",
        "nemesis",
        "battle",
        "member_summary",
        "matchup",
        "deck",
        "coaching",
        "newcomer",
    }:
        return _envelope(view, available=False, error="unsupported_view")
    tag = _tag(member_tag) if member_tag else None
    own = conn is None
    if conn is None:
        provider = source or db_facade
        conn = provider.get_connection()
    try:
        if view == "card":
            return _card_view(conn, tag, card, scope, days)
        if view == "nemesis":
            return _nemesis_view(conn, tag, scope, days)
        if view == "battle":
            return _battle_view(conn, tag, limit)
        if view == "matchup":
            return _matchup_view(conn, our_family, their_family)
        if view == "deck":
            return _deck_view(conn, tag)
        if view == "coaching":
            return _coaching_view(conn, tag, limit, days)
        if view == "newcomer":
            return _newcomer_view(conn, tag)
        return _member_summary_view(conn, tag, days)
    finally:
        if own:
            conn.close()


def _coaching_view(conn, tag, limit, days=None) -> dict[str, Any]:
    """Aggregated structural read of a member's recent play — the input the Layer-4
    summarizer reasons over. All computed; no per-battle model call."""
    if not tag:
        return _envelope("coaching", available=False, error="member_tag_required")
    # A `days` window should govern the sample, not `limit` (default 20) — otherwise
    # "last 7 days" and "last 30 days" return the identical most-recent 20 battles.
    n = 500 if days else max(5, min(int(limit or 40), 200))
    rows = conn.execute(
        "SELECT e.battle_time, b.outcome, e.closeness, e.performance, e.level_gap, "
        "e.level_validity, e.air_matchup, e.wincon_pressure, e.spell_bait_exposed, "
        "e.decisive_factor, op.archetype our_archetype, op.family our_family, "
        "op.air_answer_count, op.tank_answer_count, op.splash_answer_count, "
        "tp.archetype their_archetype, tp.family their_family "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        "LEFT JOIN deck_profile op ON op.deck_hash = e.our_deck_hash "
        "LEFT JOIN deck_profile tp ON tp.deck_hash = e.their_deck_hash "
        "WHERE e.player_tag = ? "
        + ("AND e.battle_time >= ? " if _since(days) else "")
        + "ORDER BY e.battle_time DESC LIMIT ?",
        (tag, _since(days), n) if _since(days) else (tag, n),
    ).fetchall()
    if not rows:
        return _envelope("coaching", available=False, subject=tag, error="no_battles")

    def tally(key):
        out: dict = {}
        for r in rows:
            v = r[key]
            if v is not None:
                out[str(v)] = out.get(str(v), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    wins = sum(1 for r in rows if r["outcome"] == "W")
    losses = sum(1 for r in rows if r["outcome"] == "L")
    # What they lose TO — the pattern a player can act on.
    lost_to: dict = {}
    for r in rows:
        if r["outcome"] == "L" and r["their_archetype"]:
            lost_to[r["their_archetype"]] = lost_to.get(r["their_archetype"], 0) + 1
    # Their own decks' structural gaps (from the most-played deck).
    primary = next((r for r in rows if r["our_archetype"]), None)
    deck_shape = (
        {
            "archetype": primary["our_archetype"],
            "air_answers": primary["air_answer_count"],
            "tank_answers": primary["tank_answer_count"],
            "splash_answers": primary["splash_answer_count"],
        }
        if primary
        else None
    )
    return _envelope(
        "coaching",
        available=True,
        subject=tag,
        window_days=days,
        battles=len(rows),
        record={"wins": wins, "losses": losses},
        win_rate=_win_rate(wins, losses),
        upsets=sum(1 for r in rows if r["performance"] == 1),
        underperformances=sum(1 for r in rows if r["performance"] == -1),
        decisive_factors=tally("decisive_factor"),
        air_matchups=tally("air_matchup"),
        wincon_pressure=tally("wincon_pressure"),
        primary_deck_shape=deck_shape,
        lost_to_archetypes=dict(sorted(lost_to.items(), key=lambda kv: -kv[1])[:5]),
        spell_bait_exposed_battles=sum(1 for r in rows if r["spell_bait_exposed"]),
        level_normalized_battles=sum(1 for r in rows if r["level_validity"] == "normalized"),
        note="Structural aggregate over recent battles. level_gap claims are valid only "
        "where level_validity='real'; normalized modes (ranked, Showdown) cap card levels.",
    )


def _newcomer_view(conn, tag) -> dict[str, Any]:
    """Who is this player? — the profile Elixir needs the first time it meets someone.

    A brand-new member has no history, so the awareness read is nearly empty and a
    welcome falls back to trophies (the one fact the prompt explicitly calls a
    FALLBACK, not the lead). But the roster/profile poll already stores plenty that
    distinguishes THIS player: their King level, the deck they walked in with, how
    deep their collection is, how many Evolutions they have unlocked, their peak.
    This view surfaces exactly those so a welcome can show Elixir actually looked.
    """
    if not tag:
        return _envelope("newcomer", available=False, error="member_tag_required")
    state = conn.execute(
        "SELECT trophies, best_trophies, arena_name, current_deck_json, "
        "donations_week FROM player_current_state WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    if not state:
        return _envelope("newcomer", available=False, subject=tag, error="no_profile_yet")

    # The deck they walked in with — named with the shipped rules classifier.
    deck = None
    try:
        import json as _json

        from capabilities.decks import _archetype
        from storage.cards import _deck_catalog, _enrich_deck_cards

        raw = _json.loads(state["current_deck_json"] or "[]")
        if len(raw) == 8:
            cards = _enrich_deck_cards(raw, _deck_catalog(conn))
            arch = _archetype(cards)
            deck = {
                "archetype": arch["label"],
                "family": arch["family"],
                "avg_elixir": arch["average_elixir"],
                "cards": [c.get("name") for c in cards],
            }
    except Exception:  # noqa: BLE001 - a welcome must never fail on deck naming
        deck = None

    collection = conn.execute(
        "SELECT COUNT(*) known, SUM(level >= 14) maxed, SUM(evolution_level >= 1) evos "
        "FROM player_card_collection WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    form = conn.execute(
        "SELECT SUM(outcome = 'W') w, SUM(outcome = 'L') l FROM battle_events "
        "WHERE player_tag = ? AND teammate_tag IS NULL",
        (tag,),
    ).fetchone()
    meta = conn.execute(
        "SELECT cr_account_age_years years, cr_collection_level collection_level, "
        "cr_battle_wins career_wins FROM player_metadata WHERE player_tag = ?",
        (tag,),
    ).fetchone()

    return _envelope(
        "newcomer",
        available=True,
        subject=tag,
        # expLevel (King level) is DEPRECATED and actively misleading — a 7k-trophy
        # account can report King level 1. Collection Level is the current progression
        # number, and it arrives as a badge (see engine.projections).
        collection_level=meta["collection_level"] if meta else None,
        trophies=state["trophies"],
        best_trophies=state["best_trophies"],
        arena=state["arena_name"],
        deck=deck,
        cards_known=collection["known"] if collection else None,
        cards_at_14_plus=collection["maxed"] if collection else None,
        evolutions_unlocked=collection["evos"] if collection else None,
        years_played=meta["years"] if meta else None,
        career_wins=meta["career_wins"] if meta else None,
        observed_record=(
            {"wins": form["w"], "losses": form["l"]} if form and form["w"] is not None else None
        ),
        note="First-impression facts. Lead with what distinguishes THIS player — their "
        "deck, Evolutions unlocked, King level, or peak — not trophies. Fields that are "
        "null are genuinely unknown; never guess one. Collection Level is the current "
        "progression number — King level (expLevel) is deprecated and not reported.",
    )
