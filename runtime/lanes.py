"""Lane → Discord channel resolution, and the meta-copy guard.

Both were carried out of ``engine.recognition.compose`` when the deterministic
recognition/proactive stack was retired (#207). They are the only parts of that
module production still uses, and neither is about recognition:

* ``channels()`` resolves a lane key to a Discord channel from
  ``prompts/DISCORD.md`` — a delivery concern, used by the live awareness
  delivery path.
* ``looks_like_meta()`` catches a composer narrating its own reasoning instead
  of writing copy — a posting-quality guard.

Keeping them here means the recognition package has no production importer, so
it can be deleted without touching anything live.
"""

from __future__ import annotations

import os
import re

_DISCORD_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "DISCORD.md"
)


def channels(path: str = _DISCORD_MD) -> dict[str, dict]:
    """Parse prompts/DISCORD.md: lane key → {channel_id, channel_name,
    leadership}. Read at call time so channel config can't drift into code."""
    lanes: dict[str, dict] = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return lanes
    for m in re.finditer(r"^## #(?P<name>[\w-]+)\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S):
        body = m.group("body")
        cid = re.search(r"^ID:\s*(\d+)", body, re.M)
        lane = re.search(r"^Lane:\s*([\w-]+)", body, re.M)
        scope = re.search(r"^MemoryScope:\s*(\w+)", body, re.M)
        if cid and lane:
            lanes[lane.group(1)] = {
                "channel_id": int(cid.group(1)),
                "channel_name": m.group("name"),
                "leadership": bool(scope and scope.group(1) == "leadership"),
            }
    return lanes


# The full meta-marker list, verbatim — each entry comes from a live incident
# where a composer's commentary about the signal got posted as the copy itself
# (2026-07-03: "card milestone signal lacks card names" went out as a message).
# Matching any one means "use the deterministic fallback".
META_MARKERS = (
    "skipping post",
    "skip this post",
    "would be stale",
    "is stale",
    "signal is from",
    "signal data",
    "signal lacks",
    "lacks card names",
    "data inconsistent",
    "inconsistent with",
    "live race is now",
    "as an ai",
    "unable to compose",
    "cannot compose",
)


def looks_like_meta(copy: str) -> bool:
    """True when copy reads as a model talking ABOUT the post rather than being
    the post. Cheap, deterministic, and deliberately over-inclusive."""
    low = (copy or "").lower()
    return any(m in low for m in META_MARKERS)


__all__ = ["META_MARKERS", "channels", "looks_like_meta"]
