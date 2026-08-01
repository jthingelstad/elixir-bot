"""Clash Royale deck share links — generate one, or read one a member pasted.

The game's "Copy Deck" button puts a URL on the clipboard, and the same URL opens
the deck in anyone else's client. That makes it the natural handoff in both
directions: Elixir can hand a suggestion over as something tappable rather than a
list to retype, and a member can paste their deck in instead of naming eight cards.

Format confirmed against a real 2026 client link (King Thing's, 2026-08-01):

    https://link.clashroyale.com/en?clashroyale://copyDeck
        ?deck=26000007;28000015;26000059;26000012;28000001;26000037;27000000;26000106
        &slots=0;0;0;0;0;0;0;0
        &tt=159000000
        &id=20JJJ2CCRU

  deck   eight card ids, semicolon separated, in deck order
  slots  eight values, positionally aligned with deck (see below)
  tt     the tower troop's card id (159000000 = Tower Princess)
  id     the SHARING player's tag without the '#'. Identity, not deck content.

The ids are the same ones the official API returns, so they join straight to
card_catalog with no mapping table — verified by resolving all eight of the above
plus the tower troop against our own catalog.

**slots does NOT encode Evolution or Hero form, and no parameter does.** The deck
above was shared as all-zero slots, and the same eight cards in the same order in
that player's battle log ran Evo Witch, Hero Barbarian Barrel and Evo Royal Hogs.
Three special forms, eight zeros. So a link is a base-card list: sharing a deck
that depends on an Evo or Hero LOSES that information, and anything built on top of
this module has to say so in words instead. (Independently corroborated: deck tools
that accept these links make you set evolutions by hand after importing.)
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

DECK_SIZE = 8

# The shape a 2026 client emits. The deep link is nested inside the web fallback so
# a single string works whether or not the game is installed.
_SHARE_BASE = "https://link.clashroyale.com/en?clashroyale://copyDeck"

# Read the parameters wherever they appear rather than parsing a URL structurally:
# the payload arrives in at least three shapes (the nested form above, the older
# https://link.clashroyale.com/deck/en?deck=..., and a bare clashroyale:// URI), and
# it is usually embedded in a sentence — the client prefixes "<name> wants to share a
# Clash Royale deck: ". Matching the parameters directly handles all of them and any
# future wrapper, which structural parsing would not.
_DECK_RE = re.compile(r"[?&]deck=([0-9]+(?:;[0-9]+)*)")
_TT_RE = re.compile(r"[?&]tt=([0-9]+)")
_ID_RE = re.compile(r"[?&]id=([0-9A-Za-z]+)")


# Every account has Tower Princess, so it is the safe stand-in when we do not know
# which tower troop a member runs. It is a fallback, never a preference: 21 members
# run Dagger Duchess, Cannoneer or Royal Chef, and handing one of them a Tower
# Princess link silently changes their deck.
DEFAULT_TOWER_TROOP = 159000000


def build_deck_link(
    card_ids: Iterable[int],
    *,
    tower_troop_id: Optional[int] = None,
) -> Optional[str]:
    """A shareable link for these eight cards, or ``None`` if that is not 8 ids.

    ``tt`` is ALWAYS emitted. Links generated without it reached members intact and
    then did nothing when tapped, and a tower troop is part of a deck in 2026 — an
    eight-card payload with no tower troop is an incomplete deck, so the client
    appears to reject it. Both real client links captured from the game carry it.

    ``slots`` is emitted as all zeros because that is the only value ever observed
    and its meaning is not established; it is NOT a place to smuggle Evo/Hero forms.
    ``id`` is deliberately omitted — it identifies the human doing the sharing, and
    Elixir is not one. If a link with ``tt`` still fails to open, ``id`` is the only
    remaining difference from a known-good payload.
    """
    ids = [int(c) for c in card_ids]
    if len(ids) != DECK_SIZE or any(c <= 0 for c in ids):
        return None
    link = f"{_SHARE_BASE}?deck={';'.join(str(c) for c in ids)}"
    link += "&slots=" + ";".join(["0"] * DECK_SIZE)
    link += f"&tt={int(tower_troop_id or DEFAULT_TOWER_TROOP)}"
    return link


def parse_deck_link(text: Optional[str]) -> Optional[dict]:
    """Pull a deck out of pasted text, or ``None`` when there isn't a complete one.

    Returns ``card_ids`` (8, in deck order), ``tower_troop_id`` and ``shared_by_tag``
    — the last as a '#'-prefixed tag, since that is the form every other tag in the
    system takes and a caller comparing it to a member tag should not have to know
    the link omits the '#'.

    Anything other than exactly eight ids returns None. A partial deck is not a deck,
    and guessing at the remainder would put cards in a member's hands that they never
    shared.
    """
    if not text:
        return None
    match = _DECK_RE.search(text)
    if not match:
        return None
    ids = [int(part) for part in match.group(1).split(";") if part]
    if len(ids) != DECK_SIZE:
        return None
    tt = _TT_RE.search(text)
    who = _ID_RE.search(text)
    return {
        "card_ids": ids,
        "tower_troop_id": int(tt.group(1)) if tt else None,
        "shared_by_tag": f"#{who.group(1).upper()}" if who else None,
        # Stated on every parse so no caller can forget it: the form data is not in
        # the link, so a resolved deck is base cards even when the sharer runs Evos.
        "forms_known": False,
    }


def looks_like_a_deck_link(text: Optional[str]) -> bool:
    """Cheap pre-check for routing — does this message carry a deck at all?"""
    return parse_deck_link(text) is not None
