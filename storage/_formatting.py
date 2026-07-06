"""Shared formatting helpers that avoid circular imports in the storage layer.

Functions here use late imports to access identity lookups, providing a single
shared wrapper instead of duplicated late-import wrappers in each storage module.
"""

import re
import unicodedata


_CALLABLE_DROP_CATEGORIES = frozenset({"Mn", "So", "Sk", "Cf"})
_CALLABLE_WHITESPACE = re.compile(r"\s+")


def callable_name(value: str | None) -> str:
    """Strip ornamentation from a player name so the bot can use a readable form.

    NFKD compatibility decomposition collapses fullwidth Latin (Ｓ→S),
    superscripts (²⁸→28), and ligatures (ﬁ→fi). Then drop characters in
    categories Mn (combining marks), So (other symbols — ⚡♥⚜), Sk
    (modifier symbols), and Cf (format chars like the emoji variation
    selector U+FE0F). Letters, digits, punctuation, and whitespace stay,
    so "L-Drxgo⚡" becomes "L-Drxgo" rather than "Ldrxgo". Whitespace is
    collapsed; the player's own casing is preserved.

    Empty input or names that are entirely ornamentation/non-Latin
    (e.g. "ﾑ尺ﾑ乃ﾑｲん") are returned unchanged so the literal name is still
    available as a fallback.
    """
    if not value:
        return value or ""
    nfkd = unicodedata.normalize("NFKD", value)
    cleaned = "".join(
        ch for ch in nfkd if unicodedata.category(ch) not in _CALLABLE_DROP_CATEGORIES
    )
    cleaned = _CALLABLE_WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or value


def stored_nickname(conn, tag: str | None) -> str | None:
    """The bot's stored preferred nickname for a player, or None.

    Populated only for the exceptions callable_name can't resolve (residual
    symbol-only names, LLM-named) and leader-assigned overrides — NOT a copy
    of every member's name. See engine/nicknames.py.
    """
    if conn is None or not tag:
        return None
    try:
        row = conn.execute(
            "SELECT preferred_nickname FROM player_metadata WHERE player_tag = ?",
            (tag,),
        ).fetchone()
    except Exception:
        return None  # column absent on a not-yet-migrated DB — fall through
    if not row:
        return None
    nick = row[0] if not hasattr(row, "keys") else row["preferred_nickname"]
    return nick if nick and str(nick).strip() else None


def preferred_display_name(conn, tag: str | None, raw_name: str | None = None) -> str:
    """The one readable name to address a player by, everywhere in text.

    Resolution order (recognition.md §7):
      1. stored preferred nickname (leader override or LLM-named residual);
      2. else callable_name(current_name) — the live deterministic cleaner;
      3. else the raw literal / tag.
    For the vast majority of members step 1 is empty and step 2 handles it
    live — nothing is stored and nothing can go stale.
    """
    nick = stored_nickname(conn, tag)
    if nick:
        return nick
    if raw_name is None and conn is not None and tag:
        row = conn.execute(
            "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
        ).fetchone()
        if row:
            raw_name = row[0] if not hasattr(row, "keys") else row["current_name"]
    if raw_name:
        return callable_name(raw_name)
    return tag or ""


def format_member_reference(*args, **kwargs):
    """Format a member reference — delegates to storage.identity."""
    from storage.identity import format_member_reference as _impl
    return _impl(*args, **kwargs)
