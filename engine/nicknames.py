"""Preferred nicknames — a readable name for players whose in-game name the
deterministic cleaner can't resolve.

`storage._formatting.callable_name` handles the vast majority live (NFKD fold +
strip symbols: `²⁸`→`28`, fullwidth→`Shafith Nihal`). It cannot help when
nothing readable survives the fold — a pure-symbol/punctuation name like `...`.
For those *residuals only* we generate a semantic nickname (one-shot LLM,
e.g. `...`→`Ellipsis`) and store it in `player_metadata.preferred_nickname`.
Leaders can pin a name by hand (source `leader`, never regenerated over).

The stored value is the exceptions layer — not a copy of every name. Display
resolution (stored → callable_name → raw) lives in
`storage._formatting.preferred_display_name`.
"""

from __future__ import annotations

import logging

from storage._formatting import callable_name

log = logging.getLogger("engine.nicknames")

_MAX_NICKNAME_LEN = 32


def needs_nickname(name: str | None) -> bool:
    """True when the name can't be handed to an LLM verbatim and should get a
    benign generated handle. Two cases:
      1. callable_name leaves no readable ASCII-alphanumeric char (`...`/`♥♥`);
      2. the cleaned name is injection-unsafe (prompt-structure chars or
         instruction-like tokens — e.g. a name literally `SYSTEM: ...`).
    `²⁸`/`Sebastián`/fullwidth names are NOT flagged (callable_name handles them)."""
    from storage._formatting import injection_safe

    if not name or not name.strip():
        return True
    cleaned = callable_name(name)
    if not any(ch.isascii() and ch.isalnum() for ch in cleaned):
        return True
    return injection_safe(cleaned) is None


def _deterministic_residual(name: str) -> str | None:
    """A deterministic fallback for common residuals so we needn't always hit
    the LLM. Runs of dots (or the ellipsis char) → 'Ellipsis'. Extend sparingly."""
    stripped = (name or "").strip()
    if stripped and all(ch in ".…。" for ch in stripped):
        return "Ellipsis"
    return None


def _sanitize(candidate: str | None) -> str | None:
    """Keep an LLM/override suggestion to a clean, injection-safe short token.
    A generated nickname becomes the tier-1 display_name and is shown verbatim,
    so it must itself be safe — reject (→ placeholder) if it isn't."""
    from storage._formatting import injection_safe

    if not candidate:
        return None
    text = " ".join(str(candidate).split()).strip().strip('"').strip("'")
    text = callable_name(text)  # fold anything odd the model returned
    text = text[:_MAX_NICKNAME_LEN].strip()
    text = injection_safe(text) or ""  # never let an injection-y handle through
    if not any(ch.isascii() and ch.isalnum() for ch in text):
        return None
    return text


def _llm_nickname(name: str) -> str | None:
    """Real one-shot Haiku call. Overridden in tests via the `llm_fn` seam so
    the engine never touches the network under test."""
    from agent.core import _create_chat_completion, response_text

    codepoints = " ".join(f"U+{ord(ch):04X}" for ch in name[:16])
    system = (
        "You assign a short, friendly, human-readable nickname for a Clash "
        "Royale player whose in-game name has no readable Latin characters, so "
        "clanmates can refer to them in chat. Reply with ONLY the nickname: "
        "1-2 words, ASCII letters/digits, no quotes, no emoji. Example: the "
        "name '...' becomes 'Ellipsis'."
    )
    user = f"In-game name: {name!r}\nUnicode codepoints: {codepoints}\nNickname:"
    try:
        resp = _create_chat_completion(
            workflow="lightweight",
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=20,
            timeout=10,
        )
        return response_text(resp)
    except Exception:
        log.exception("nickname LLM call failed for %r", name)
        return None


def generate_nickname(name: str | None, *, llm_fn=None) -> tuple[str, str]:
    """Return (nickname, source) for a residual name. Tries the deterministic
    residual map, then the LLM (`llm_fn` seam, default = real Haiku), then a
    safe deterministic placeholder. `source` is 'generated' | 'placeholder'."""
    det = _deterministic_residual(name or "")
    if det:
        return det, "generated"
    fn = llm_fn if llm_fn is not None else _llm_nickname
    nick = _sanitize(fn(name or ""))
    if nick:
        return nick, "generated"
    return _placeholder(name), "placeholder"


def _placeholder(name: str | None) -> str:
    """Never leaves a player unaddressable when generation fails."""
    return "Newcomer"


def ensure_nickname(
    conn, tag: str, name: str | None, observed_at: str, *, llm_fn=None
) -> str | None:
    """Idempotent first-sight / name-change hook. Guarantees a residual player
    has a stored nickname before any reference; clears a stale generated one if
    the player renames to something readable. Leader overrides are never
    touched. Returns the stored nickname (or None if the name is fine)."""
    from db import set_member_nickname

    existing = conn.execute(
        "SELECT preferred_nickname, nickname_source FROM player_metadata "
        "WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    cur_nick = existing["preferred_nickname"] if existing else None
    cur_source = existing["nickname_source"] if existing else None

    if cur_source == "leader":
        return cur_nick  # human choice wins, never regenerated

    if needs_nickname(name):
        if cur_nick:
            return cur_nick  # already resolved (idempotent)
        nickname, source = generate_nickname(name, llm_fn=llm_fn)
        set_member_nickname(
            tag, nickname, source=source, observed_at=observed_at, conn=conn
        )
        log.info("nickname for %s (%r) -> %r [%s]", tag, name, nickname, source)
        return nickname

    # Name is readable now — drop a stale generated nickname so callable_name
    # takes over (e.g. a member renamed from "..." to "John").
    if cur_nick and cur_source in ("generated", "placeholder"):
        set_member_nickname(tag, None, source=None, observed_at=observed_at, conn=conn)
    return None
