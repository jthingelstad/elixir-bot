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

_SCOPES = {"all": "1=1", "competitive": "is_competitive = 1"}


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


def _card_view(conn, tag, card, scope) -> dict[str, Any]:
    if not card:
        return _envelope("card", available=False, error="card_required")
    card_id = _resolve_card_id(conn, card)
    if card_id is None:
        return _envelope("card", available=False, error="unknown_card", card=card)
    predicate = _SCOPES.get(scope, _SCOPES["all"])
    where = [f"card_id = {card_id}", predicate]
    params: list = []
    subject = "clan"
    if tag:
        where.append("player_tag = ?")
        params.append(tag)
        subject = tag
    rows = conn.execute(
        f"SELECT side, evolution_level, "
        f"SUM(outcome = 'W') w, SUM(outcome = 'L') l, COUNT(*) n "
        f"FROM battle_card_plays WHERE {' AND '.join(where)} "
        f"GROUP BY side, evolution_level",
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
        playing=sorted(playing, key=lambda e: -e["n"]),
        facing=sorted(facing, key=lambda e: -e["n"]),
        note=(
            "Win rate omitted below n=30 (insufficient_sample). Intra-clan battles "
            "can double-count in clan-wide aggregates."
        ),
    )


def _nemesis_view(conn, tag, scope) -> dict[str, Any]:
    predicate = _SCOPES.get(scope, _SCOPES["all"])
    where = ["side = 'opponent'", predicate]
    params: list = []
    if tag:
        where.append("player_tag = ?")
        params.append(tag)
    rows = conn.execute(
        f"SELECT card_id, evolution_level, SUM(outcome = 'W') w, SUM(outcome = 'L') l, "
        f"COUNT(*) n FROM battle_card_plays WHERE {' AND '.join(where)} "
        f"GROUP BY card_id, evolution_level HAVING n >= ? ORDER BY (1.0 * w / (w + l)) ASC",
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
                "our_deck_hash": r["our_deck_hash"],
                "their_deck_hash": r["their_deck_hash"],
            }
        )
    return _envelope(
        "battle",
        available=bool(battles),
        subject=tag,
        battles=battles,
        note="Computed read only (no commentary in Feature 1). closeness/hp_margin "
        "need both sides' tower data; NULL where absent.",
    )


def _member_summary_view(conn, tag) -> dict[str, Any]:
    if not tag:
        return _envelope("member_summary", available=False, error="member_tag_required")
    agg = conn.execute(
        "SELECT COUNT(*) battles, "
        "AVG(discipline_delta) avg_discipline, "
        "SUM(closeness = 0) stomps, SUM(closeness = 3) squeakers, "
        "COUNT(hp_margin) with_tower_data "
        "FROM battle_enrichment WHERE player_tag = ?",
        (tag,),
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
    return _envelope(
        "member_summary",
        available=True,
        subject=tag,
        battles=agg["battles"],
        battles_with_tower_data=agg["with_tower_data"],
        stomps=agg["stomps"],
        squeakers=agg["squeakers"],
        avg_discipline_delta=round(agg["avg_discipline"], 2)
        if agg["avg_discipline"] is not None
        else None,
        best_cards=ranked[:5],
        worst_cards=ranked[5:][::-1][:5],  # ranked 6th-onward, worst first; no overlap with best
        note="Computed rollup (no prose). Card win rates are member-plays-it, n>=30.",
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
    source: Any = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Computed battle intelligence. Views: ``card`` (needs ``card``, optional
    ``member_tag`` else clan-wide), ``nemesis`` (optional ``member_tag``),
    ``battle``/``member_summary``/``deck`` (need ``member_tag``), ``matchup``
    (optional ``our_family``/``their_family`` else the full matrix). Read-only."""
    if view not in {"card", "nemesis", "battle", "member_summary", "matchup", "deck"}:
        return _envelope(view, available=False, error="unsupported_view")
    tag = _tag(member_tag) if member_tag else None
    own = conn is None
    if conn is None:
        provider = source or db_facade
        conn = provider.get_connection()
    try:
        if view == "card":
            return _card_view(conn, tag, card, scope)
        if view == "nemesis":
            return _nemesis_view(conn, tag, scope)
        if view == "battle":
            return _battle_view(conn, tag, limit)
        if view == "matchup":
            return _matchup_view(conn, our_family, their_family)
        if view == "deck":
            return _deck_view(conn, tag)
        return _member_summary_view(conn, tag)
    finally:
        if own:
            conn.close()
