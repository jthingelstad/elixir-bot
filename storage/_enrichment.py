"""Member-reference enrichment shared across storage modules.

These helpers decorate row dicts with readable member references and rank
fields before they are surfaced to the LLM. They live in the storage layer
(not db) because they depend on identity formatting and rank computation,
which are storage concerns — keeping them out of db lets db stay a pure
connection/schema layer with no upward imports.
"""

import sqlite3

from storage._formatting import callable_name, format_member_reference
from storage.member_ranks import RANK_FIELDS, compute_member_ranks


def _member_reference_fields(conn: sqlite3.Connection, member_id: int, item: dict) -> dict:
    tag = item.get("player_tag") or item.get("tag")
    if not tag:
        row = conn.execute(
            "SELECT player_tag FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        tag = row["player_tag"] if row else None
    if not tag:
        return item
    item["member_ref"] = format_member_reference(tag, conn=conn)
    # Substitute readable forms for every name field surfaced to the LLM so
    # ²⁸/Ｓｈａｆｉｔｈ-style names get narrated as "28" / "Shafith Nihal" instead
    # of the literal unicode. The DB columns stay literal — only the dict
    # passed to callers (and the LLM) is transformed.
    for name_field in ("current_name", "name", "player_name", "member_name"):
        if item.get(name_field):
            item[name_field] = callable_name(item[name_field])
    item.update(_member_ranks_for(conn, member_id))
    return item


# Member-rank cache keyed on id(conn). sqlite3.Connection rejects arbitrary
# attribute assignment, so we can't stash this on the conn itself. Bounded
# size keeps memory predictable; FIFO eviction keeps the policy simple.
# Connections are short-lived (managed_connection opens fresh per public
# call), so id-collision after close is theoretically possible but rare —
# tests can call _clear_member_ranks_cache() to reset between assertions.
_MEMBER_RANKS_CACHE: dict[int, dict] = {}
_MEMBER_RANKS_CACHE_MAX = 16


def _clear_member_ranks_cache() -> None:
    """Test hook to drop all cached rank tables."""
    _MEMBER_RANKS_CACHE.clear()


def _member_ranks_for(conn: sqlite3.Connection, member_id: int) -> dict:
    """Return rank fields for one member.

    The full rank table is computed once per connection and cached at the
    module level keyed on id(conn). Subsequent lookups are O(1) — important
    because ``_member_reference_fields`` is called per-row in roster,
    digest, and promotion-candidate flows. Inactive members and members
    with insufficient data get every field set to ``None`` so consumers
    can distinguish "no data" from a real rank.
    """
    key = id(conn)
    cache = _MEMBER_RANKS_CACHE.get(key)
    if cache is None:
        cache = compute_member_ranks(conn=conn)
        if len(_MEMBER_RANKS_CACHE) >= _MEMBER_RANKS_CACHE_MAX:
            _MEMBER_RANKS_CACHE.pop(next(iter(_MEMBER_RANKS_CACHE)))
        _MEMBER_RANKS_CACHE[key] = cache
    member_entry = cache.get(member_id)
    if member_entry is None:
        return {field: None for field in RANK_FIELDS}
    return dict(member_entry)
