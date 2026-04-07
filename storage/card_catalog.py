"""Card catalog storage layer.

Syncs and queries the Clash Royale card catalog from the /cards API endpoint.
Provides the data foundation for the lookup_cards LLM tool and the card
training quiz module.
"""

import json
from datetime import datetime, timezone

from db import get_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card_type_from_id(card_id: int) -> str:
    """Derive card type from the Clash Royale card ID range."""
    prefix = card_id // 1000000
    if prefix == 26:
        return "troop"
    elif prefix == 27:
        return "building"
    elif prefix == 28:
        return "spell"
    elif prefix == 159:
        return "tower_troop"
    return "unknown"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_card_catalog(api_response: dict, conn=None) -> int:
    """Upsert all cards from a /cards API response.

    api_response should have 'items' and optionally 'supportItems'.
    Returns the number of cards synced.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        now = _utcnow()
        count = 0
        all_cards = list(api_response.get("items") or [])
        all_cards.extend(api_response.get("supportItems") or [])

        for card in all_cards:
            card_id = card.get("id")
            if card_id is None:
                continue
            name = card.get("name") or ""
            elixir_cost = card.get("elixirCost")  # None for support cards
            rarity = (card.get("rarity") or "").lower()
            max_level = card.get("maxLevel")
            max_evolution_level = card.get("maxEvolutionLevel")  # None if no evo
            card_type = _card_type_from_id(card_id)
            icon_urls = card.get("iconUrls") or {}
            icon_url = icon_urls.get("medium")
            hero_icon_url = icon_urls.get("heroMedium")
            evolution_icon_url = icon_urls.get("evolutionMedium")

            conn.execute(
                """INSERT INTO card_catalog
                       (card_id, name, elixir_cost, rarity, max_level,
                        max_evolution_level, card_type, icon_url,
                        hero_icon_url, evolution_icon_url, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(card_id) DO UPDATE SET
                       name = excluded.name,
                       elixir_cost = excluded.elixir_cost,
                       rarity = excluded.rarity,
                       max_level = excluded.max_level,
                       max_evolution_level = excluded.max_evolution_level,
                       card_type = excluded.card_type,
                       icon_url = excluded.icon_url,
                       hero_icon_url = excluded.hero_icon_url,
                       evolution_icon_url = excluded.evolution_icon_url,
                       synced_at = excluded.synced_at""",
                (
                    card_id, name, elixir_cost, rarity, max_level,
                    max_evolution_level, card_type, icon_url,
                    hero_icon_url, evolution_icon_url, now,
                ),
            )
            count += 1

        conn.commit()
        return count
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict with a mode_label field."""
    d = dict(row)
    evo = d.get("max_evolution_level")
    if evo == 3:
        d["mode_label"] = "Evo + Hero"
    elif evo == 2:
        d["mode_label"] = "Hero"
    elif evo == 1:
        d["mode_label"] = "Evo"
    else:
        d["mode_label"] = None
    return d


def lookup_cards(
    *,
    name=None,
    rarity=None,
    min_cost=None,
    max_cost=None,
    card_type=None,
    has_evolution=None,
    limit=25,
    conn=None,
) -> list[dict]:
    """Flexible card lookup for the LLM tool.

    All parameters are optional filters. Returns a list of card dicts.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        clauses = []
        params = []

        if name:
            clauses.append("name LIKE ?")
            params.append(f"%{name}%")
        if rarity:
            clauses.append("rarity = ?")
            params.append(rarity.lower())
        if min_cost is not None:
            clauses.append("elixir_cost >= ?")
            params.append(min_cost)
        if max_cost is not None:
            clauses.append("elixir_cost <= ?")
            params.append(max_cost)
        if card_type:
            clauses.append("card_type = ?")
            params.append(card_type.lower())
        if has_evolution is True:
            clauses.append("max_evolution_level IS NOT NULL")
        elif has_evolution is False:
            clauses.append("max_evolution_level IS NULL")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM card_catalog{where} ORDER BY name LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_card_by_name(name: str, conn=None) -> dict | None:
    """Case-insensitive substring match, returns best match or None."""
    close = conn is None
    conn = conn or get_connection()
    try:
        # Try exact match first
        row = conn.execute(
            "SELECT * FROM card_catalog WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        if row:
            return _row_to_dict(row)
        # Fall back to substring
        row = conn.execute(
            "SELECT * FROM card_catalog WHERE name LIKE ? ORDER BY LENGTH(name) LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_random_cards(
    count: int,
    *,
    card_type=None,
    rarity=None,
    exclude_ids=None,
    conn=None,
) -> list[dict]:
    """Return random cards from the catalog for quiz question generation."""
    close = conn is None
    conn = conn or get_connection()
    try:
        clauses = []
        params = []

        if card_type:
            clauses.append("card_type = ?")
            params.append(card_type.lower())
        if rarity:
            clauses.append("rarity = ?")
            params.append(rarity.lower())
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            clauses.append(f"card_id NOT IN ({placeholders})")
            params.extend(exclude_ids)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM card_catalog{where} ORDER BY RANDOM() LIMIT ?"
        params.append(count)

        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_all_cards(conn=None) -> list[dict]:
    """Return the full card catalog."""
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM card_catalog ORDER BY name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def catalog_count(conn=None) -> int:
    """Return the number of cards in the catalog."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM card_catalog").fetchone()
        return row["cnt"] if row else 0
    finally:
        if close:
            conn.close()
