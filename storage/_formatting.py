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


def format_member_reference(*args, **kwargs):
    """Format a member reference — delegates to storage.identity."""
    from storage.identity import format_member_reference as _impl
    return _impl(*args, **kwargs)
