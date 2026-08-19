"""Deterministic checks between a composed post and a member's screen.

Agentic Loop v2 (docs/plans/agentic-loop.md). When posting became a tool call,
the tool boundary became the place to enforce what a prompt can only request.
Every rule here was **measured**, not imagined: the 2026-08-04 scoped-composer
experiment ran seven real hard-posts through Haiku and Sonnet, and these are the
ways the output was wrong.

The contract is a bounce, not a rewrite. A rejected post returns the reason to
the model, which fixes it and calls again — the author stays the author. Silent
repair would hide a systematic problem behind a clean-looking post, which is how
"the model handles it" becomes untrue without anyone noticing.

**One measured exception: the literal `\\n`.** It bounced 22 times in 41 wakes
over the Phase 2 gate window — 56% of episodes paid an extra model round — and
the model rewrote it correctly every single time. A bounce that is always
followed by the same successful fix is not surfacing a defect, it is renting
one. So this one is repaired rather than rejected, and every repair is COUNTED
into the turn's episode. That keeps the rule the docstring above is really
about: the problem stays visible. Repair without the counting would be the
silent rewrite this module refuses to do.

What this does NOT do is judge quality. Whether a welcome sounds like Elixir
looked at the newcomer is the job file's problem and the reflection loop's; this
layer only catches the mechanical failures that a member would see as a bug.
"""

from __future__ import annotations

import re

# Haiku emitted literal backslash-n sequences inside the tool argument in 4 of 7
# experiment cases — the JSON string contained the characters `\` and `n`, so
# Discord would have rendered them verbatim mid-sentence.
_LITERAL_ESCAPE_RE = re.compile(r"\\[nrt]")

# Two of those same cases also carried a trailing `"` — the model closing a
# quotation it opened in its own head. It reads as a typo at the end of a post.
_TRAILING_QUOTE_RE = re.compile(r'["\']\s*$')

# A 2026-08-15 scoped-responder post embedded the model's tool-call parameter
# syntax in the member-visible content while leaving ``covers_signal_keys``
# empty. The trailing junk ended in ``]``, so the stray-quote check above did
# not catch it and the durable delivery path faithfully sent it to Discord.
_TOOL_PARAMETER_MARKUP_RE = re.compile(r"</?parameter\b[^>]*>", re.IGNORECASE)

# `:name:` shortcodes. An unknown one renders as literal text, so a hallucinated
# emoji is a visible defect rather than a missing decoration.
_EMOJI_RE = re.compile(r":([a-z0-9_]+):", re.IGNORECASE)

# Discord's own hard limit is 2000; the lanes want far less than that, but the
# limit is the thing that would actually fail the send.
DISCORD_HARD_LIMIT = 2000


class PostRejected(Exception):
    """A composed post failed a mechanical check. The message is shown to the
    model verbatim, so it must say what is wrong AND what to do about it."""


def _reject(problem: str, fix: str) -> None:
    raise PostRejected(f"{problem} {fix} Rewrite the content and call the tool again.")


def _repair_literal_escapes(text: str, *, newline: str, repairs: list | None) -> str:
    """Turn the characters ``\\`` + ``n`` into what the model meant, and say so.

    ``newline`` differs by surface: Discord wants a real line break, clan chat is
    a single plain line and wants a space. Both beat shipping ``\\n`` mid-sentence.
    """
    if not _LITERAL_ESCAPE_RE.search(text):
        return text
    repaired = _LITERAL_ESCAPE_RE.sub(
        lambda m: {"n": newline, "r": newline, "t": " "}[m.group(0)[1]], text
    )
    # Collapse a run of them (a `\n\n` paragraph break on a surface that has no
    # line breaks would otherwise leave a double space).
    if newline == " ":
        repaired = re.sub(r" {2,}", " ", repaired)
    if repairs is not None:
        repairs.append("literal_escape_sequences")
    return repaired


def validate_discord_post(
    content: str,
    *,
    lane: str,
    known_emoji: set[str] | None = None,
    max_chars: int = DISCORD_HARD_LIMIT,
    repairs: list | None = None,
) -> str:
    """Return the post to send, or raise :class:`PostRejected`.

    The return value is now load-bearing — literal escape sequences come back
    repaired — so a caller MUST use it. ``repairs`` is the sink that keeps those
    fixes visible; pass the turn's list so the episode records them.
    """
    text = content or ""
    if not text.strip():
        _reject("The post content was empty.", "A delivered post must have text.")

    text = _repair_literal_escapes(text, newline="\n", repairs=repairs)

    if _TRAILING_QUOTE_RE.search(text.strip()):
        _reject(
            "The content ends with a stray quotation mark.",
            "Send only the post itself, with no wrapping quotes.",
        )

    if _TOOL_PARAMETER_MARKUP_RE.search(text):
        _reject(
            "The content contains leaked tool-call parameter markup.",
            "Put tool arguments in their structured fields and send only the post text as content.",
        )

    if len(text) > max_chars:
        _reject(
            f"The content is {len(text)} characters, over the {max_chars} limit for "
            f"the {lane} lane.",
            "Cut the framing (trophies, arena, tenure) before cutting the specific "
            "detail that makes the post worth reading.",
        )

    if known_emoji is not None:
        # Only the `elixir_` namespace is checkable. Standard Unicode shortcodes
        # (:wave:, :crossed_swords:) are explicitly allowed by the emoji guidance
        # and Discord renders them natively — the brain's real posts use them, so
        # a check that rejected them would reject correct output. What CANNOT be
        # allowed is an invented custom name, which posts as literal text.
        used = {name.lower() for name in _EMOJI_RE.findall(text)}
        unknown = sorted(
            name for name in used if name.startswith("elixir") and name not in known_emoji
        )
        if unknown:
            _reject(
                f"The content uses emoji shortcodes that do not exist in this server: "
                f"{', '.join(':' + name + ':' for name in unknown)}. "
                "They would render as literal text.",
                "Use only the documented Elixir emoji, or none at all.",
            )
    return text


def validate_clan_chat_post(content: str, *, limit: int = 200, repairs: list | None = None) -> str:
    """Clan chat is stricter than Discord and fails differently: the game's own
    filter silently blanks handle-like tokens, and there is no formatting.

    The censor-safe rewrite and the sentence-aware clip are applied by
    ``runtime.clan_chat_copy`` — this only rejects what those cannot fix.
    """
    text = (content or "").strip()
    if not text:
        _reject("The clan-chat line was empty.", "A join must carry an in-game welcome.")

    # A space, not a newline: clan chat has no line breaks, so the repair has to
    # produce the single plain line the surface actually renders.
    text = _repair_literal_escapes(text, newline=" ", repairs=repairs).strip()

    if _EMOJI_RE.search(text):
        _reject(
            "The clan-chat line contains a :shortcode: emoji. The game renders "
            "none of them, so it would appear as literal text.",
            "Write plain text with no shortcodes, markdown, links, or @mentions.",
        )

    if any(marker in text for marker in ("**", "__", "](", "<@", "<#")):
        _reject(
            "The clan-chat line contains Discord markup (bold, links, or mentions). "
            "The game renders none of it.",
            "Write one plain sentence.",
        )

    # Over-length is not a rejection: the clip is sentence-aware and the signature
    # reserve is computed downstream. But a line so long that clipping would
    # destroy it is a composition failure, not a formatting one.
    if len(text) > limit * 2:
        _reject(
            f"The clan-chat line is {len(text)} characters — more than double the "
            f"{limit}-character surface.",
            "Write it at length for the surface, not as a paragraph to be truncated.",
        )
    return text


__all__ = [
    "DISCORD_HARD_LIMIT",
    "PostRejected",
    "validate_clan_chat_post",
    "validate_discord_post",
]
