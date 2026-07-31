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
from engine.deck_hash import deck_hash


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _advantage_from_win_rate(win_rate: float) -> int:
    """Map a measured member win rate to a -2..+2 advantage band (0.5 = even;
    ~7 points per band)."""
    return max(-2, min(2, round((win_rate - 0.5) / 0.07)))


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
                level_gap(member_cards, their_cards, is_ranked=bool(r["is_ranked"])),
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
    ids = json.dumps(sorted((c.get("id"), c.get("evolution_level")) for c in cards))
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


def _rebuild_matchup_matrix(conn) -> int:
    """Recompute the whole 6x6 family matrix from measured clan outcomes
    (INSERT OR REPLACE). Weekly calibration is just re-running this. Returns cells."""
    now = _now()
    rows = conn.execute(
        """
        SELECT op.family our_f, tp.family their_f,
               SUM(b.outcome = 'W') w, SUM(b.outcome = 'L') l, COUNT(*) n
          FROM battle_enrichment e
          JOIN battle_events b   ON b.dedup_key = e.battle_dedup_key
          JOIN deck_profile op   ON op.deck_hash = e.our_deck_hash
          JOIN deck_profile tp   ON tp.deck_hash = e.their_deck_hash
         WHERE op.family <> 'unclassified' AND tp.family <> 'unclassified'
         GROUP BY op.family, tp.family
        """
    ).fetchall()
    cells = 0
    for our_f, their_f, w, losses, n in rows:
        decided = w + losses
        if decided == 0:
            continue
        wr = w / decided
        conn.execute(
            "INSERT OR REPLACE INTO matchup_expectation "
            "(our_family, their_family, advantage, measured_win_rate, n, basis, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                our_f,
                their_f,
                _advantage_from_win_rate(wr),
                round(wr, 3),
                n,
                "measured from clan battles",
                now,
            ),
        )
        cells += 1
    return cells


def _fill_expected_advantage(conn) -> int:
    """Snapshot each battle's matchup-cell advantage into
    battle_enrichment.expected_advantage and set performance (the upset detector:
    +1 = won when disadvantaged, -1 = lost when advantaged, else 0). Only battles
    whose both decks are profiled and whose cell exists. Returns rows updated."""
    cur = conn.execute(
        """
        UPDATE battle_enrichment AS e
           SET expected_advantage = m.advantage,
               performance = CASE
                   WHEN b.outcome = 'W' AND m.advantage < 0 THEN 1
                   WHEN b.outcome = 'L' AND m.advantage > 0 THEN -1
                   ELSE 0 END
          FROM battle_events b, deck_profile op, deck_profile tp, matchup_expectation m
         WHERE b.dedup_key = e.battle_dedup_key
           AND op.deck_hash = e.our_deck_hash
           AND tp.deck_hash = e.their_deck_hash
           AND m.our_family = op.family
           AND m.their_family = tp.family
           AND e.expected_advantage IS NULL
        """
    )
    return cur.rowcount


@managed_connection
def rebuild_deck_intel(*, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Feature 2 Stage-B (all $0): profile new decks, recompute the measured
    matchup matrix, and fill expected_advantage/performance. Returns work-set
    counts for telemetry."""
    profiled = _profile_new_decks(conn)
    cells = _rebuild_matchup_matrix(conn)
    filled = _fill_expected_advantage(conn)
    return {"profiled": profiled, "matchup_cells": cells, "expected_filled": filled}


# ── Feature 3: gated per-battle prose (Haiku, allowlist + date gate) ───────────

PROSE_PROMPT_VERSION = 1
BATTLE_PROSE_MIN_DATE = "2026-07-20"
_PROSE_MODEL = "claude-haiku-4-5-20251001"
_LOSS_NATURE = {"structural", "piloting", "level", "close", "unclear"}
_CLOSENESS_WORD = {0: "a stomp", 1: "a clear margin", 2: "close", 3: "a squeaker (very close)"}
_PERF_WORD = {
    1: "upset (won when the matchup was against them)",
    -1: "underperformed (lost a matchup they were favored in)",
    0: "as expected",
}


def _prose_input_hash(row, prompt_version: int) -> str:
    """sha256 over everything the prose depends on, so a Feature-2 re-profile or a
    prompt bump changes the hash and triggers regeneration (plan §4)."""
    import hashlib

    parts = [
        str(row["hp_margin"]),
        str(row["closeness"]),
        str(row["discipline_delta"]),
        str(row["level_gap"]),
        str(row["expected_advantage"]),
        str(row["performance"]),
        str(row["our_deck_hash"]),
        str(row["their_deck_hash"]),
        str(prompt_version),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


_GATED_PROSE_SQL = """
    SELECT e.battle_dedup_key, e.battle_time, b.outcome, b.game_mode_name,
           b.crowns_for, b.crowns_against, b.is_ranked,
           e.hp_margin, e.closeness, e.discipline_delta, e.level_gap,
           e.expected_advantage, e.performance, e.our_deck_hash, e.their_deck_hash,
           op.archetype our_archetype, tp.archetype their_archetype,
           e.input_hash, e.prompt_version
      FROM battle_enrichment e
      JOIN battle_events b ON b.dedup_key = e.battle_dedup_key
      JOIN player_metadata pm ON pm.player_tag = e.player_tag
                             AND pm.battle_enrichment_enabled = 1
      LEFT JOIN deck_profile op ON op.deck_hash = e.our_deck_hash
      LEFT JOIN deck_profile tp ON tp.deck_hash = e.their_deck_hash
     WHERE e.battle_time >= ?
       AND (e.commentary IS NULL OR e.prompt_version IS NULL OR e.prompt_version <> ?)
     ORDER BY (e.performance IS NOT NULL AND e.performance <> 0) DESC, e.battle_time DESC
     LIMIT ?
"""


def generate_prose_batch(limit: int = 10, *, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Feature 3: write per-battle prose for up to ``limit`` GATED battles
    (allowlisted member ∩ battle_time >= min date) missing prose at the current
    prompt_version. One Haiku call per battle; idempotent via input_hash. This is
    the only LLM spend in Battle Intelligence v1.

    Deliberately NOT wrapped in one transaction: the LLM call takes seconds, and
    holding a write lock across it would block the live bot's DB (and the model
    call's own ``record_llm_call``). Each battle is read lock-free, generated, then
    written+committed in a short per-battle transaction. Opens its own connection
    with a busy_timeout unless one is provided (tests)."""
    import db as db_facade
    import elixir_agent

    own = conn is None
    if own:
        conn = db_facade.get_connection()
        conn.execute("PRAGMA busy_timeout = 30000")
    rows = conn.execute(
        _GATED_PROSE_SQL,
        (BATTLE_PROSE_MIN_DATE, PROSE_PROMPT_VERSION, max(1, min(int(limit or 10), 200))),
    ).fetchall()
    now = _now()
    written = refreshed = 0
    for r in rows:
        ihash = _prose_input_hash(r, PROSE_PROMPT_VERSION)
        if r["input_hash"] == ihash and r["prompt_version"] == PROSE_PROMPT_VERSION:
            continue  # already current (defensive; the WHERE mostly excludes these)
        was_present = r["prompt_version"] is not None
        context = {
            "mode": r["game_mode_name"],
            "outcome": "win" if r["outcome"] == "W" else "loss" if r["outcome"] == "L" else "draw",
            "crowns": f"{r['crowns_for']}-{r['crowns_against']}",
            "our_deck": r["our_archetype"],
            "their_deck": r["their_archetype"],
            "how_close": _CLOSENESS_WORD.get(r["closeness"])
            if r["closeness"] is not None
            else "unknown (tower data missing)",
            "hp_margin": r["hp_margin"],
            "elixir_discipline_delta": r["discipline_delta"],
            "deck_level_gap": None if r["is_ranked"] else r["level_gap"],
            "expected_matchup_advantage": r["expected_advantage"],
            "result_vs_expectation": _PERF_WORD.get(r["performance"])
            if r["performance"] is not None
            else None,
        }
        try:
            result = elixir_agent.generate_battle_prose(context)
        except Exception:  # noqa: BLE001 - one battle's LLM failure must not stop the batch
            continue
        if not isinstance(result, dict) or result.get("error") or not result.get("commentary"):
            continue  # transient; retried next run
        loss_nature = result.get("loss_nature")
        if loss_nature not in _LOSS_NATURE:
            loss_nature = None  # honor the CHECK constraint; model returned null/other
        conn.execute(
            "UPDATE battle_enrichment SET commentary = ?, loss_nature = ?, notable = ?, "
            "confidence = ?, model = ?, prompt_version = ?, input_hash = ?, enriched_at = ? "
            "WHERE battle_dedup_key = ?",
            (
                str(result.get("commentary"))[:600],
                loss_nature,
                1 if result.get("notable") else 0,
                str(result.get("confidence") or "")[:10] or None,
                _PROSE_MODEL,
                PROSE_PROMPT_VERSION,
                ihash,
                now,
                r["battle_dedup_key"],
            ),
        )
        if own:
            conn.commit()  # short per-battle write; no lock held across the LLM call
        if was_present:
            refreshed += 1
        else:
            written += 1
    if own:
        conn.close()
    return {"prose_written": written, "prose_refreshed": refreshed, "scanned": len(rows)}
