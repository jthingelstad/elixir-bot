"""Shared formatting helpers that avoid circular imports in the storage layer.

Functions here use late imports to access identity lookups, providing a single
shared wrapper instead of duplicated late-import wrappers in each storage module.
"""

import re
import unicodedata


_CALLABLE_DROP_CATEGORIES = frozenset({"Mn", "So", "Sk", "Cf"})
_CALLABLE_WHITESPACE = re.compile(r"\s+")

# --- Injection / display-name safety (normalize-at-source) -------------------
# Player names are untrusted, member-controlled input that flows into LLM
# prompts. callable_name() is cosmetic (it folds unicode tricks) but keeps Latin
# instruction text + structural chars, so it is NOT a security boundary. These
# guard the ONE value everything shows / feeds the model: players.display_name.
_MAX_DISPLAY_NAME_LEN = 32
# Characters that could break out of a prompt/JSON structure. Ordinary name
# punctuation (. - _ spaces, most symbols callable_name already dropped) is fine.
_STRUCTURE_CHARS = frozenset('{}[]<>"`\\|')
# Instruction-like tokens — a name matching one gets a benign generated nickname
# upstream instead of reaching the model verbatim.
_INJECTION_TOKEN_RE = re.compile(
    r"(?i)(?:^|\W)(system|assistant|ignore|disregard|instructions?|prompt|you\s+are|override)(?:\W|$)"
)


def _has_ascii_alnum(text: str) -> bool:
    return any(ch.isascii() and ch.isalnum() for ch in text)


def injection_safe(name: str | None) -> str | None:
    """Return a name (already callable_name-cleaned) if it is safe to hand an
    LLM verbatim, else None. Rejects control chars, prompt-structure characters,
    and instruction-like tokens, and caps length. A rejected name falls through
    to a generated nickname / safe fallback — it never reaches the model raw."""
    if not name:
        return None
    text = name.strip()
    if not text:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):  # control chars
        return None
    if any(ch in _STRUCTURE_CHARS for ch in text):
        return None
    if _INJECTION_TOKEN_RE.search(text):
        return None
    text = text[:_MAX_DISPLAY_NAME_LEN].strip()
    return text or None


def compute_display_name(conn, tag: str | None, raw_name: str | None = None) -> str:
    """The single materialized, LLM-safe display name for a player. Three tiers:
      1. stored preferred_nickname (leader override or generated residual);
      2. else callable_name(current_name) IF readable and injection-safe;
      3. else a safe fallback `Player <tag suffix>` — NEVER the raw name.
    Pure read (no writes); used both at materialization time and as the live
    fallback in preferred_display_name when the column is unset."""
    nick = stored_nickname(conn, tag)
    if nick:
        return nick.strip()[:_MAX_DISPLAY_NAME_LEN].strip() or (f"Player {tag[-4:]}" if tag else "Player")
    if raw_name is None and conn is not None and tag:
        row = conn.execute(
            "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
        ).fetchone()
        if row:
            raw_name = row[0] if not hasattr(row, "keys") else row["current_name"]
    if raw_name:
        cleaned = callable_name(raw_name)
        safe = injection_safe(cleaned)
        if safe and _has_ascii_alnum(safe):
            return safe
    return f"Player {tag[-4:]}" if tag else "Player"


def _stored_display_name(conn, tag: str | None) -> str | None:
    """The materialized players.display_name, or None if unset/absent."""
    if conn is None or not tag:
        return None
    try:
        row = conn.execute(
            "SELECT display_name FROM players WHERE player_tag = ?", (tag,)
        ).fetchone()
    except Exception:
        return None  # column absent on a not-yet-migrated DB — fall through
    if not row:
        return None
    dn = row[0] if not hasattr(row, "keys") else row["display_name"]
    return dn if dn and str(dn).strip() else None


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
    """The one readable, LLM-safe name to address a player by, everywhere in text.

    Normalize-at-source: this now reads the materialized ``players.display_name``
    (computed + injection-sanitized when the name enters the system). It falls
    back to a live ``compute_display_name`` only when the column is unset/absent
    (a not-yet-materialized or not-yet-migrated row), so behaviour is unchanged
    for callers while the raw name never leaks.
    """
    dn = _stored_display_name(conn, tag)
    if dn:
        return dn
    # Live fallback for a not-yet-materialized / unmigrated row. Preserves the
    # historical contract: an UNRESOLVABLE member (no row, no usable name) yields
    # the tag/"" so callers can detect it — NOT the `Player <tag>` materialization
    # fallback (that would wrongly attribute events to unknown members).
    nick = stored_nickname(conn, tag)
    if nick:
        return nick.strip()[:_MAX_DISPLAY_NAME_LEN].strip() or (tag or "")
    if raw_name is None and conn is not None and tag:
        row = conn.execute(
            "SELECT current_name FROM players WHERE player_tag = ?", (tag,)
        ).fetchone()
        if row:
            raw_name = row[0] if not hasattr(row, "keys") else row["current_name"]
    if raw_name:
        safe = injection_safe(callable_name(raw_name))
        if safe and _has_ascii_alnum(safe):
            return safe
    return tag or ""


def format_member_reference(*args, **kwargs):
    """Format a member reference — delegates to storage.identity."""
    from storage.identity import format_member_reference as _impl
    return _impl(*args, **kwargs)
