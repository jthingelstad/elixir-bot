"""runtime.emoji — Idempotent guild emoji sync from assets/emoji/."""

import logging
from functools import cache
from pathlib import Path

import discord

log = logging.getLogger(__name__)

EMOJI_DIR = Path(__file__).resolve().parent.parent / "assets" / "emoji"


@cache
def available_emoji_names() -> tuple[str, ...]:
    """Names of custom emojis Elixir ships — the source of truth is the asset dir,
    which is sync'd one-way into the guild at startup."""
    names = [p.stem for p in EMOJI_DIR.glob("*.png")]
    names.extend(p.stem for p in EMOJI_DIR.glob("*.gif"))
    return tuple(sorted(set(names)))


async def sync_emoji(guild: discord.Guild) -> None:
    """Upload any emoji from assets/emoji/ that don't already exist in the guild."""
    existing = {e.name for e in guild.emojis}
    uploaded = 0
    skipped = 0

    for path in sorted(EMOJI_DIR.glob("*.png")) + sorted(EMOJI_DIR.glob("*.gif")):
        name = path.stem
        if name in existing:
            skipped += 1
            continue
        try:
            data = path.read_bytes()
            await guild.create_custom_emoji(name=name, image=data)
            uploaded += 1
            log.info("Uploaded emoji :%s:", name)
        except (discord.HTTPException, OSError) as exc:
            log.error("Failed to upload emoji :%s:: %s", name, exc)

    log.info("Emoji sync complete: %d uploaded, %d already existed", uploaded, skipped)
