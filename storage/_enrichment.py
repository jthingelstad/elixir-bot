"""Member-reference enrichment shared across storage modules.

These helpers decorate row dicts with readable member references and rank
fields before they are surfaced to the LLM. They live in the storage layer
(not db) because they depend on identity formatting and rank computation,
which are storage concerns — keeping them out of db lets db stay a pure
connection/schema layer with no upward imports.
"""

import sqlite3

from storage._formatting import (
    callable_name,
    format_member_reference,
    preferred_display_name,
)
from storage.member_ranks import RANK_FIELDS, compute_member_ranks


def _member_reference_fields(conn: sqlite3.Connection, member_id, item: dict) -> dict:
    # v5.1: the second argument IS the player tag (§7 — the tag is the key).
    # The name is kept so seven storage modules' call sites read unchanged.
    tag = item.get("player_tag") or item.get("tag") or member_id
    if not tag:
        return item
    item["member_ref"] = format_member_reference(tag, conn=conn)
    # Substitute the preferred readable name for every name field surfaced to
    # the LLM: a stored nickname (leader/LLM-named residual like "..."→"Ellipsis")
    # wins, else callable_name folds ²⁸/Ｓｈａｆｉｔｈ-style names to "28"/"Shafith
    # Nihal". The DB columns stay literal — only the dict passed to callers
    # (and the LLM) is transformed.
    preferred = preferred_display_name(conn, tag)
    for name_field in ("current_name", "name", "player_name", "member_name"):
        if item.get(name_field):
            item[name_field] = preferred or callable_name(item[name_field])
    item.update(_member_ranks_for(conn, tag))
    # QA H6/M20/L18: annotate roster status so a departed member is never
    # reported as an active war no-show / current-roster player. active =
    # has an open membership; departed = only closed memberships (with the
    # last left_at); unknown = observed non-member never in clan_memberships.
    item.update(_membership_status_for(conn, tag))
    return item


_MEMBERSHIP_CACHE: dict[int, dict] = {}
_MEMBERSHIP_CACHE_MAX = 16


def _membership_status_for(conn: sqlite3.Connection, tag: str) -> dict:
    """Roster status for one member, batch-loaded and cached per connection
    (like the rank table) so this stays O(1) per enriched row."""
    key = id(conn)
    cache = _MEMBERSHIP_CACHE.get(key)
    if cache is None:
        cache = {}
        for row in conn.execute(
            "SELECT player_tag, "
            "MAX(CASE WHEN left_at IS NULL THEN 1 ELSE 0 END) AS has_open, "
            "MAX(left_at) AS last_left FROM clan_memberships GROUP BY player_tag"
        ):
            active = bool(row["has_open"])
            cache[row["player_tag"]] = {
                "roster_status": "active" if active else "departed",
                "left_at": None if active else row["last_left"],
            }
        if len(_MEMBERSHIP_CACHE) >= _MEMBERSHIP_CACHE_MAX:
            _MEMBERSHIP_CACHE.pop(next(iter(_MEMBERSHIP_CACHE)))
        _MEMBERSHIP_CACHE[key] = cache
    return dict(cache.get(tag, {"roster_status": "unknown", "left_at": None}))


# Member-rank cache keyed on id(conn). sqlite3.Connection rejects arbitrary
# attribute assignment, so we can't stash this on the conn itself. Bounded
# size keeps memory predictable; FIFO eviction keeps the policy simple.
# Connections are short-lived (managed_connection opens fresh per public
# call), so id-collision after close is theoretically possible but rare —
# tests can call _clear_member_ranks_cache() to reset between assertions.
_MEMBER_RANKS_CACHE: dict[int, dict] = {}
_MEMBER_RANKS_CACHE_MAX = 16


def _clear_member_ranks_cache() -> None:
    """Test hook to drop all cached rank + membership tables."""
    _MEMBER_RANKS_CACHE.clear()
    _MEMBERSHIP_CACHE.clear()


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
    member_entry = cache.get(member_id)  # keyed by player_tag in v5.1
    if member_entry is None:
        return {field: None for field in RANK_FIELDS}
    return dict(member_entry)
