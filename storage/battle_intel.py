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
from typing import Optional

from db import managed_connection
from engine.battle_metrics import (
    closeness_band,
    discipline_delta,
    hp_margin,
    level_gap,
)
from engine.deck_hash import deck_hash


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
