"""Battle Intelligence Feature 1 — Stage A: the computed enrichment writer.

Extracts ``battle_card_plays`` (both sides, form-aware) and computed
``battle_enrichment`` rows for 1v1 battles, clan-wide, no model. Idempotent
(``INSERT OR IGNORE`` on dedup-keyed PKs) and self-catching-up: it processes
battles that lack a ``battle_enrichment`` row, so the 15-minute job and the
one-time backfill are the SAME operation at different batch sizes, and a re-run
or an overlap is a no-op. Duels (``rounds_json``) and 2v2 (``teammate_tag``)
get no rows.

Keys are ``battle_events.dedup_key`` verbatim (the v25/v26 lesson).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from db import managed_connection
from engine.battle_metrics import (
    closeness_band,
    discipline_delta,
    hp_margin,
    level_gap,
)
from engine.deck_hash import _identity_pairs, deck_hash


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _card_max_levels(conn) -> dict:
    """``{card_id: rarity max level}`` — what turns a rarity-relative API level
    into one that two decks can be compared on. See engine.battle_metrics.level_gap.
    """
    return {r[0]: r[1] for r in conn.execute("SELECT card_id, max_level FROM card_catalog")}


def _parse_deck(deck_json) -> list[dict]:
    """The stored 8-card deck as card dicts, or [] if unparseable / not 8 cards.
    A 1v1 ``deck_json`` is always exactly 8 cards; 16/24 are duel concatenations
    (already excluded upstream) and are rejected here defensively."""
    if not deck_json:
        return []
    try:
        cards = json.loads(deck_json) if isinstance(deck_json, str) else deck_json
    except TypeError, ValueError:
        return []
    if not isinstance(cards, list) or len(cards) != 8:
        return []
    return [c for c in cards if isinstance(c, dict)]


def _card_play_rows(row: sqlite3.Row, side: str, cards: list[dict]) -> list[tuple]:
    return [
        (
            row["dedup_key"],
            side,
            c.get("id"),
            c.get("level"),
            c.get("evolution_level"),
            c.get("star_level"),
            row["player_tag"],  # the SUBJECT member, stamped on BOTH sides
            row["battle_time"],
            row["outcome"],
            row["mode_group"],
            row["is_competitive"],
        )
        for c in cards
        if c.get("id") is not None
    ]


# Only clean 1v1 battles with an exactly-8-card member deck are enriched. The
# json_array_length filter excludes 12-card boat payloads and any non-8 shape in
# SQL, so they never clog the self-catching-up scan (which finds battles lacking
# an enrichment row). Duels/2v2 are already excluded by rounds_json/teammate_tag.
_UNENRICHED_SQL = """
    SELECT b.dedup_key, b.player_tag, b.battle_time, b.outcome, b.mode_group,
           b.is_competitive, b.is_ranked, b.deck_json, b.opponent_deck_json,
           b.elixir_leaked, b.opponent_elixir_leaked,
           b.king_tower_hp, b.princess_towers_hp_json,
           b.opponent_king_tower_hp, b.opponent_princess_towers_hp_json
      FROM battle_events b
      LEFT JOIN battle_enrichment e ON e.battle_dedup_key = b.dedup_key
     WHERE e.battle_dedup_key IS NULL
       AND b.teammate_tag IS NULL AND b.rounds_json IS NULL
       AND b.deck_json IS NOT NULL
       AND json_array_length(b.deck_json) = 8
     ORDER BY b.observed_at DESC
     LIMIT ?
"""


@managed_connection
def enrich_battles(limit: int = 500, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Process up to ``limit`` un-enriched 1v1 battles. Returns work-set counts
    for job telemetry: a caught-up run reports ``enriched=0`` distinctly from a
    broken one. Runs in one transaction (the decorator commits)."""
    limit = max(1, min(int(limit or 500), 20000))
    rows = conn.execute(_UNENRICHED_SQL, (limit,)).fetchall()
    max_levels = _card_max_levels(conn)

    _CP_SQL = (
        "INSERT OR IGNORE INTO battle_card_plays "
        "(battle_dedup_key, side, card_id, level, evolution_level, star_level, "
        " player_tag, battle_time, outcome, mode_group, is_competitive) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    card_plays = 0
    enriched = 0
    for r in rows:
        member_cards = _parse_deck(r["deck_json"])
        opp_cards = _parse_deck(r["opponent_deck_json"])  # [] if absent/duel-shaped
        their_cards = opp_cards if len(opp_cards) == 8 else None

        if len(member_cards) == 8:  # SQL guarantees 8 elements; guards a non-dict element
            before = conn.total_changes
            conn.executemany(_CP_SQL, _card_play_rows(r, "member", member_cards))
            if their_cards:
                conn.executemany(_CP_SQL, _card_play_rows(r, "opponent", their_cards))
            card_plays += conn.total_changes - before
            margin = hp_margin(
                r["king_tower_hp"],
                r["princess_towers_hp_json"],
                r["opponent_king_tower_hp"],
                r["opponent_princess_towers_hp_json"],
            )
            values = (
                r["dedup_key"],
                r["player_tag"],
                r["battle_time"],
                margin,
                closeness_band(margin),
                discipline_delta(r["opponent_elixir_leaked"], r["elixir_leaked"]),
                level_gap(
                    member_cards,
                    their_cards,
                    is_ranked=bool(r["is_ranked"]),
                    max_levels=max_levels,
                ),
                deck_hash(member_cards),
                deck_hash(their_cards) if their_cards else None,
            )
        else:
            # Defensive: 8-element JSON but a non-dict card. Mark it processed with
            # NULL metrics so it is never re-scanned (no backfill clog).
            values = (
                r["dedup_key"],
                r["player_tag"],
                r["battle_time"],
                None,
                None,
                None,
                None,
                None,
                None,
            )

        conn.execute(
            "INSERT OR IGNORE INTO battle_enrichment "
            "(battle_dedup_key, player_tag, battle_time, hp_margin, closeness, "
            " discipline_delta, level_gap, our_deck_hash, their_deck_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        enriched += 1

    return {"enriched": enriched, "card_plays": card_plays, "scanned": len(rows)}


def backfill(*, batch: int = 2000, db_path: Optional[str] = None) -> dict:
    """One-time backfill: loop ``enrich_battles`` in bounded, self-committing
    batches until no un-enriched 1v1 battles remain. Bounded chunks keep each
    transaction small so it never blocks the running bot's WAL for long. Opens
    its own connections (one per batch) via the ``managed_connection`` default
    path; pass ``db_path`` only in tests/validation.
    """
    import db as db_facade

    totals = {"enriched": 0, "card_plays": 0, "batches": 0}
    while True:
        conn = db_facade.get_connection(db_path) if db_path else db_facade.get_connection()
        try:
            result = enrich_battles(batch, conn=conn)
            conn.commit()
        finally:
            conn.close()
        totals["enriched"] += result["enriched"]
        totals["card_plays"] += result["card_plays"]
        totals["batches"] += 1
        if result["scanned"] == 0:  # every scanned battle gets a row, so this terminates
            break
    return totals


# ── Feature 2: deck profiles + measured matchup matrix (all $0, no model) ──────
#
# Archetype/family come from the deterministic ``_classify`` RULES, so a deck's
# profile is a pure function of its cards (no era versioning). The matchup matrix
# is MEASURED from clan outcomes — all 36 family cells clear n>=30 on live data,
# so the clan's own history is the matrix. Imported lazily to keep the Stage-A
# worker's import graph tiny and avoid a storage->capabilities import at module load.


def _profile_row(cards_json, catalog):
    """(family, archetype, win_condition, avg_elixir, ids_json) for a deck_json,
    or None if it is not a clean 8-card deck."""
    from capabilities.decks import _average_elixir, _classify
    from storage.cards import _enrich_deck_cards

    try:
        raw = json.loads(cards_json) if isinstance(cards_json, str) else cards_json
    except TypeError, ValueError:
        return None
    if not isinstance(raw, list) or len(raw) != 8:
        return None
    cards = _enrich_deck_cards(raw, catalog)
    names = {c["name"] for c in cards if c.get("name")}
    avg = _average_elixir(cards)
    label, family, wincons = _classify(names, avg)
    # Identity comes from the RAW deck_json via the same helper deck_hash() uses.
    # Reading evolution_level off the catalog-enriched card silently dropped it, so
    # every deck was stored base-form: 983 of 1,678 member decks (59%) actually run an
    # Evo or Hero. That made the Evo ownership gate inert (decks needing an Evo the
    # member lacks looked buildable) and scored deck facts against base-form cards.
    ids = json.dumps([list(p) for p in _identity_pairs(raw)])
    return family, label, ", ".join(wincons), avg, ids


def _profile_new_decks(conn) -> int:
    """Write a deck_profile row for every observed deck_hash not yet profiled
    (both member and opponent sides). Returns rows written. $0."""
    from storage.cards import _deck_catalog

    catalog = _deck_catalog(conn)
    now = _now()
    # A representative deck_json per un-profiled hash, from either side.
    rows = conn.execute(
        """
        SELECT h, dj FROM (
            SELECT e.our_deck_hash h, b.deck_json dj
              FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key
             WHERE e.our_deck_hash IS NOT NULL
            UNION
            SELECT e.their_deck_hash h, b.opponent_deck_json dj
              FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key
             WHERE e.their_deck_hash IS NOT NULL
        )
        WHERE h NOT IN (SELECT deck_hash FROM deck_profile)
        GROUP BY h
        """
    ).fetchall()
    written = 0
    for h, dj in rows:
        profiled = _profile_row(dj, catalog)
        if profiled is None:
            continue
        family, archetype, wincond, avg, ids = profiled
        conn.execute(
            "INSERT OR IGNORE INTO deck_profile "
            "(deck_hash, family, archetype, win_condition, avg_elixir, cards_json, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (h, family, archetype, wincond, avg, ids, now),
        )
        written += 1
    return written


@managed_connection
def rebuild_deck_intel(*, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Feature 2 Stage-B (all $0): profile any deck seen in battle but not yet
    classified. The matchup matrix that used to be rebuilt here is gone -- family
    matchup measured ~3 points once adjusted for who plays which archetype, against
    22 for card levels, so it was a confident number in front of members that said
    almost nothing."""
    profiled = _profile_new_decks(conn)
    return {"profiled": profiled}


# ── v2 Layers 2-3: deck facts + per-battle structural tags (computed, $0) ──────
#
# Replaces Feature 3's per-battle prose. Same reads, as structured data a summarizer
# can aggregate — no per-battle model call, no elixir-discipline crutch, and the
# level_validity fix so normalized modes never produce a fictional level claim.


def _card_facts_map(conn) -> dict:
    """(card_id, evolution_level) -> facts dict, with elixir_cost joined in."""
    rows = conn.execute(
        "SELECT f.*, c.elixir_cost FROM card_facts f "
        "LEFT JOIN card_catalog c ON c.card_id = f.card_id"
    ).fetchall()
    return {(r["card_id"], r["evolution_level"] or 0): dict(r) for r in rows}


def _deck_card_facts(conn, deck_hash: str, facts: dict) -> list[dict]:
    """The 8 cards' facts for a deck, via its stored (card_id, evolution_level) set."""
    row = conn.execute(
        "SELECT cards_json FROM deck_profile WHERE deck_hash = ?", (deck_hash,)
    ).fetchone()
    if not row or not row["cards_json"]:
        return []
    try:
        pairs = json.loads(row["cards_json"])
    except TypeError, ValueError:
        return []
    out = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        got = facts.get((pair[0], pair[1] or 0))
        if got:
            out.append(got)
    return out


def _restate_level_gaps(conn) -> int:
    """Recompute battle_enrichment.level_gap for every enriched battle.

    level_gap is a stored snapshot of a formula that changed: it used to average
    rarity-relative API levels, which measured a deck's rarity mix as much as its
    strength. Without a restatement the correction reaches only battles enriched
    from here on, leaving 13,000 rows carrying a number that decisive_factor ranks
    above every other explanation.
    """
    max_levels = _card_max_levels(conn)
    updated = 0
    rows = conn.execute(
        "SELECT e.battle_dedup_key, b.deck_json, b.opponent_deck_json, b.is_ranked "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        "WHERE b.opponent_deck_json IS NOT NULL"
    ).fetchall()
    for r in rows:
        mine, theirs = _parse_deck(r["deck_json"]), _parse_deck(r["opponent_deck_json"])
        if len(mine) != 8 or len(theirs) != 8:
            continue
        conn.execute(
            "UPDATE battle_enrichment SET level_gap = ? WHERE battle_dedup_key = ?",
            (
                level_gap(mine, theirs, is_ranked=bool(r["is_ranked"]), max_levels=max_levels),
                r["battle_dedup_key"],
            ),
        )
        updated += 1
    return updated


def _noop_checkpoint() -> None:
    """Default for a BORROWED connection: never commit someone else's work."""


# Rows per executemany, and per released write-lock hold. Small enough that the
# single SQLite writer is handed back promptly, large enough that per-statement
# overhead stops mattering.
WRITE_CHUNK = 500


def _fill_deck_facts(conn, *, restate: bool = False, checkpoint=_noop_checkpoint) -> int:
    """Compute each deck's completeness counts from its cards' enriched facts.

    ``restate`` recomputes every profiled deck rather than only the incomplete ones,
    and is what a ROLE DEFINITION change needs. The counts are a snapshot of rules
    that do change: is_air_answer was fixed on 2026-08-01 after big spells were
    found to be counted as air answers on 10% of the corpus, and without a restate
    those decks would have carried the inflated count forever behind their own
    facts_complete=1 stamp. The same trap as the expected_advantage IS NULL guard.
    """
    from engine.card_roles import deck_facts

    facts = _card_facts_map(conn)
    if not facts:
        return 0
    updated = 0
    # Retry decks marked incomplete, not just never-scored ones. A deck scored while
    # the card enricher was still running got counts from a partial fact table; without
    # this it would keep those wrong counts forever, because its own `facts_complete=0`
    # stamp excluded it from the next pass.
    rows = conn.execute(
        "SELECT deck_hash FROM deck_profile"
        if restate
        else "SELECT deck_hash FROM deck_profile WHERE facts_complete IS NULL OR facts_complete = 0"
    ).fetchall()
    pending: list[tuple] = []
    for r in rows:
        card_facts = _deck_card_facts(conn, r["deck_hash"], facts)
        if not card_facts:
            continue
        d = deck_facts(card_facts)
        pending.append(
            (
                d["air_answer_count"],
                d["tank_answer_count"],
                d["splash_answer_count"],
                d["swarm_count"],
                d["bait_unit_count"],
                d["has_big_spell"],
                d["has_small_spell"],
                d["facts_complete"],
                r["deck_hash"],
            )
        )
        if len(pending) >= WRITE_CHUNK:
            updated += _flush_deck_facts(conn, pending)
            checkpoint()
    updated += _flush_deck_facts(conn, pending)
    checkpoint()
    return updated


def _flush_deck_facts(conn, pending: list[tuple]) -> int:
    if not pending:
        return 0
    conn.executemany(
        "UPDATE deck_profile SET air_answer_count=?, tank_answer_count=?, "
        "splash_answer_count=?, swarm_count=?, bait_unit_count=?, has_big_spell=?, "
        "has_small_spell=?, facts_complete=? WHERE deck_hash=?",
        pending,
    )
    n = len(pending)
    pending.clear()
    return n


def _deck_summary(conn, deck_hash: str, facts: dict) -> dict:
    """A deck's derived counts plus how much air pressure it brings (for the matchup)."""
    from engine.card_roles import deck_facts

    card_facts = _deck_card_facts(conn, deck_hash, facts)
    if not card_facts:
        return {}
    d = deck_facts(card_facts)
    # Air pressure this deck brings: units that actually fly (spells don't count).
    d["air_threat_count"] = sum(
        1
        for f in card_facts
        if f.get("unit_domain") == "air" and f.get("spell_tier") in (None, "none")
    )
    return d


_BATTLE_TAG_BATCH = 5000


def _fill_battle_tags(
    conn,
    limit: Optional[int] = _BATTLE_TAG_BATCH,
    force: bool = False,
    checkpoint=_noop_checkpoint,
) -> int:
    """Snapshot per-battle structural tags for battles that don't have them yet.

    ``force`` re-tags every battle (see ``rebuild_interpreted``); ``limit=None`` lifts
    the batch cap so a forced sweep completes in one pass.
    """
    from engine.card_roles import decisive_factor, level_validity

    facts = _card_facts_map(conn)
    if not facts:
        return 0
    where = (
        "1=1"
        if force
        else (
            "e.level_validity IS NULL OR EXISTS ("
            "  SELECT 1 FROM deck_profile dp"
            "  WHERE dp.deck_hash IN (e.our_deck_hash, e.their_deck_hash)"
            "    AND COALESCE(dp.facts_complete, 0) = 0)"
        )
    )
    sql = (
        "SELECT e.battle_dedup_key, e.our_deck_hash, e.their_deck_hash, e.level_gap, "
        "e.closeness, e.discipline_delta, b.game_mode_name, b.is_ranked "
        "FROM battle_enrichment e JOIN battle_events b ON b.dedup_key = e.battle_dedup_key "
        f"WHERE {where}"
    )
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    updated = 0
    pending: list[tuple] = []
    for r in rows:
        validity = level_validity(r["game_mode_name"], r["is_ranked"])
        pending.append(
            (
                validity,
                decisive_factor(
                    level_gap=r["level_gap"],
                    level_ok=(validity == "real"),
                    closeness=r["closeness"],
                    discipline_delta=r["discipline_delta"],
                ),
                r["battle_dedup_key"],
            )
        )
        if len(pending) >= WRITE_CHUNK:
            updated += _flush_battle_tags(conn, pending)
            checkpoint()
    updated += _flush_battle_tags(conn, pending)
    checkpoint()
    return updated


def _flush_battle_tags(conn, pending: list[tuple]) -> int:
    if not pending:
        return 0
    conn.executemany(
        "UPDATE battle_enrichment SET level_validity=?, decisive_factor=? WHERE battle_dedup_key=?",
        pending,
    )
    n = len(pending)
    pending.clear()
    return n


def rebuild_interpreted(*, force: bool = False, conn: Optional[sqlite3.Connection] = None) -> dict:
    """v2 Layers 2-3 (all $0): deck facts from enriched card facts, then per-battle
    structural tags. Safe to re-run; fills what is missing, and with ``force`` also
    RESTATES what is already there — which is what a role-definition change needs.

    ``force`` re-tags every battle regardless of state. Needed after the card-facts
    table gains coverage, because a battle tagged against partial facts looks settled:
    its decks are complete now, so the incremental guard would skip it.

    **Deliberately NOT @managed_connection.** That decorator makes the whole call one
    transaction, and SQLite has exactly one writer. On 2026-08-03 this held it for
    46.1s across 117,434 statements: every API persist failed with `database is
    locked` for 17 minutes (28 dropped payloads), the engine tick was skipped as
    "maximum number of running instances reached", and the job never finished —
    `run_count 1, success_count 0`. This work is idempotent and explicitly safe to
    re-run, so it does not need to be atomic; it needs to release the writer. When it
    owns the connection it commits between chunks. When the connection is BORROWED it
    commits nothing, because an inner commit would persist a caller's partial work and
    defeat the engine tick's per-step rollback guard.
    """
    from db import get_connection

    owns = conn is None
    conn = conn or get_connection()
    checkpoint = conn.commit if owns else _noop_checkpoint
    try:
        gaps = _restate_level_gaps(conn) if force else 0
        checkpoint()
        decks = _fill_deck_facts(conn, restate=force, checkpoint=checkpoint)
        if force:
            battles = _fill_battle_tags(conn, limit=None, force=True, checkpoint=checkpoint)
        else:
            battles = 0
            while True:
                n = _fill_battle_tags(conn, checkpoint=checkpoint)
                battles += n
                # Stop on a SHORT batch, not an empty one. The incremental WHERE also
                # matches battles whose decks are still facts_complete=0, and the
                # UPDATE does not clear that — so a permanently-incomplete deck
                # re-selects the same rows forever and `if not n` never fires. A full
                # batch is the only evidence there may be more.
                if n < _BATTLE_TAG_BATCH:
                    break
        checkpoint()
        return {"deck_facts": decks, "battle_tags": battles, "level_gaps": gaps}
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()
