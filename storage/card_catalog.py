"""Card catalog storage layer.

Syncs and queries the Clash Royale card catalog from the /cards API endpoint.
Provides the data foundation for the lookup_cards LLM tool.
"""

from datetime import datetime, timezone

from db import managed_connection
from engine.normalize import card_display_max_level

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


def _escape_like(value: str) -> str:
    """Escape LIKE wildcard characters so they match literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@managed_connection
def sync_card_catalog(api_response: dict, conn=None) -> int:
    """Upsert all cards from a /cards API response.

    api_response should have 'items' and optionally 'supportItems'.
    Returns the number of cards synced.

    Genuinely-new cards (a card_id not previously in the catalog) also raise a
    `card_added` game_event so the game-level stream can announce them clan-wide
    with the card image — the Ronin case. The very first population of an empty
    catalog is a bootstrap, not news, so it emits nothing.
    """
    from storage import game_events as ge

    now = _utcnow()
    count = 0
    all_cards = list(api_response.get("items") or [])
    all_cards.extend(api_response.get("supportItems") or [])

    ge.ensure_schema(conn)  # lazily adds card_catalog.first_seen_at + game_events
    existing = {r[0] for r in conn.execute("SELECT card_id FROM card_catalog").fetchall()}
    bootstrap = not existing
    new_cards: list[dict] = []

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
                    hero_icon_url, evolution_icon_url, synced_at, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                card_id,
                name,
                elixir_cost,
                rarity,
                max_level,
                max_evolution_level,
                card_type,
                icon_url,
                hero_icon_url,
                evolution_icon_url,
                now,
                now,
            ),
        )
        count += 1
        if card_id not in existing and not bootstrap:
            new_cards.append(
                {
                    "card_id": card_id,
                    "name": name,
                    "rarity": rarity,
                    "elixir_cost": elixir_cost,
                    "card_type": card_type,
                    "icon_url": icon_url,
                }
            )

    for c in new_cards:
        ge.insert_game_event(
            conn,
            dedup_key=f"card_added:{c['card_id']}",
            event_type="card_added",
            change_key=f"card:{c['card_id']}",
            observed_at=now,
            payload=c,
        )

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict with a mode_label field.

    QA L16: mode_label here is a CAPABILITY of the card in the catalog (whether
    an Evo / Hero form exists at all), NOT whether a given member has unlocked
    it — the member-card tool uses the same word for unlock state. Emit explicit
    supports_evo / supports_hero booleans so the two can't be conflated, and note
    the capability sense on the label.
    """
    d = dict(row)
    # Report levels on the DISPLAY scale, like every other card surface. The
    # catalog stores the API's rarity-relative max (epic 11, legendary 8), so this
    # tool was telling the model Wall Breakers max at 11 while get_member_cards
    # said 16 for the same card. Two scales in one conversation is how a member
    # ended up reading "display Lv15/16, normalized 10/11".
    if isinstance(d.get("max_level"), int):
        d["max_level"] = card_display_max_level(d["max_level"]) or d["max_level"]
    evo = d.get("max_evolution_level")
    supports_evo = evo in (1, 3)
    supports_hero = evo in (2, 3)
    d["supports_evo"] = supports_evo
    d["supports_hero"] = supports_hero
    if evo == 3:
        d["mode_label"] = "Evo + Hero (available)"
    elif evo == 2:
        d["mode_label"] = "Hero (available)"
    elif evo == 1:
        d["mode_label"] = "Evo (available)"
    else:
        d["mode_label"] = None
    return d


@managed_connection
def lookup_cards(
    *,
    name=None,
    rarity=None,
    min_cost=None,
    max_cost=None,
    card_type=None,
    has_evolution=None,
    role=None,
    limit=25,
    conn=None,
) -> list[dict]:
    """Flexible card lookup for the LLM tool.

    ``role`` filters on the enriched behaviour facts rather than the catalog:
    win_condition, tank, mini_tank, support, swarm, building, spawner, spell,
    champion. A member asked "what cards are win cons?" and got the two in the deck
    he had just been shown, because there was no way to ask the catalog that
    question — the facts had been enriched for 122 cards and nothing could query
    them. Base forms only; an Evo does not change what a card is FOR.

    All parameters are optional filters. Returns
    ``{cards, total_matched, returned, truncated}`` so the caller can tell a
    complete result from a capped one (QA M18), and — when a name is given —
    orders by relevance so an exact/prefix match wins over an alphabetical one
    (QA M17: `name='Knight'` used to return "Golden Knight" first).
    """
    clauses = []
    params = []

    if name:
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(name)}%")
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
    if role:
        want = str(role).strip().lower().replace(" ", "_").replace("-", "_")
        if want.endswith("s"):
            want = want[:-1]  # "spells" -> "spell", "win_cons" -> "win_con"
        # Members say "win cons", the model may send "win_conditions" or "wincon".
        # Normalising here rather than demanding the exact enum keeps a real
        # question from silently returning zero cards.
        if want.startswith("win"):
            want = "win_condition"
        clauses.append(
            "card_id IN (SELECT card_id FROM card_facts WHERE evolution_level = 0 AND role = ?)"
        )
        params.append(want)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total_matched = conn.execute(f"SELECT COUNT(*) FROM card_catalog{where}", params).fetchone()[0]

    if name:
        # exact match, then prefix, then shortest (Knight before Golden Knight).
        order = (
            "ORDER BY (LOWER(name) = LOWER(?)) DESC, "
            "(LOWER(name) LIKE LOWER(?) || '%') DESC, LENGTH(name) ASC, name"
        )
        order_params = [name, _escape_like(name)]
    else:
        order = "ORDER BY name"
        order_params = []
    sql = f"SELECT * FROM card_catalog{where} {order} LIMIT ?"
    rows = conn.execute(sql, [*params, *order_params, limit]).fetchall()
    cards = [_row_to_dict(r) for r in rows]
    result = {
        "cards": cards,
        "total_matched": total_matched,
        "returned": len(cards),
        "truncated": total_matched > len(cards),
    }
    # QA L15: elixir_cost is NULL for costless cards (Mirror, tower troops), so a
    # min/max_cost filter silently drops them (SQL NULL comparisons are never
    # true). Flag it so "cost 0-10" reading as empty isn't mistaken for "no such
    # cards exist".
    if min_cost is not None or max_cost is not None:
        result["cost_filter_note"] = (
            "Cards with no elixir cost (Mirror, tower troops) are excluded by a "
            "min/max_cost filter — their elixir_cost is null, not a number."
        )
    return result


@managed_connection
def get_card_by_name(name: str, conn=None) -> dict | None:
    """Case-insensitive substring match, returns best match or None."""
    # Try exact match first
    row = conn.execute(
        "SELECT * FROM card_catalog WHERE LOWER(name) = LOWER(?)", (name,)
    ).fetchone()
    if row:
        return _row_to_dict(row)
    # Fall back to substring
    row = conn.execute(
        "SELECT * FROM card_catalog WHERE name LIKE ? ESCAPE '\\' ORDER BY LENGTH(name) LIMIT 1",
        (f"%{_escape_like(name)}%",),
    ).fetchone()
    return _row_to_dict(row) if row else None


@managed_connection
def catalog_count(conn=None) -> int:
    """Return the number of cards in the catalog."""
    row = conn.execute("SELECT COUNT(*) AS cnt FROM card_catalog").fetchone()
    return row["cnt"] if row else 0
