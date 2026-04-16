"""Compose horizontal card-icon strips for the quiz embed.

The multi-card quiz question types (cycle_total, cycle_back,
positive_trade) involve 2–4 cards the learner needs to reason about. One
Discord embed can only carry one image, so we fetch each card's icon
from the CR CDN, resize to a uniform height, and composite them into a
single PNG. The caller attaches the PNG as a Discord file.

Fetches are short-lived (200ms typical per icon) and in-process; we don't
cache, because catalog icon URLs are stable and the composite is only
built at question-generation time — a handful of seconds once per quiz
session start.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Sequence

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("elixir.card_training.images")

_ICON_HEIGHT = 96
_PADDING = 16
_LABEL_PAD = 6
_LABEL_HEIGHT = 22
_BG = (33, 37, 43, 255)   # Discord dark-background-ish
_FG = (220, 221, 222, 255)
_FETCH_TIMEOUT = 5.0


@dataclass(frozen=True)
class CardArt:
    name: str
    icon_url: str | None


def _load_font(size: int = 14) -> ImageFont.ImageFont:
    for candidate in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _fetch_icon(url: str) -> Image.Image | None:
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("card icon fetch failed url=%s err=%s", url, exc)
        return None
    try:
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        log.warning("card icon decode failed url=%s err=%s", url, exc)
        return None


def _placeholder_tile(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (60, 66, 74, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, size - 1, size - 1), outline=(90, 96, 106, 255), width=2)
    draw.line((8, 8, size - 9, size - 9), fill=(90, 96, 106, 255), width=2)
    draw.line((8, size - 9, size - 9, 8), fill=(90, 96, 106, 255), width=2)
    return img


def build_card_strip(cards: Sequence[CardArt]) -> bytes | None:
    """Return PNG bytes for a horizontal strip of card icons with labels.

    Returns ``None`` when the input is empty. Icons that fail to fetch or
    decode are rendered as placeholder tiles so a transient network hiccup
    doesn't tank the whole quiz post.
    """
    if not cards:
        return None

    font = _load_font()
    icons: list[Image.Image] = []
    for card in cards:
        tile = None
        if card.icon_url:
            raw = _fetch_icon(card.icon_url)
            if raw is not None:
                ratio = _ICON_HEIGHT / raw.height
                tile = raw.resize(
                    (max(1, int(raw.width * ratio)), _ICON_HEIGHT),
                    Image.LANCZOS,
                )
        if tile is None:
            tile = _placeholder_tile(_ICON_HEIGHT)
        icons.append(tile)

    widths = [img.width for img in icons]
    total_width = sum(widths) + _PADDING * (len(icons) + 1)
    total_height = _PADDING + _ICON_HEIGHT + _LABEL_PAD + _LABEL_HEIGHT + _PADDING

    canvas = Image.new("RGBA", (total_width, total_height), _BG)
    draw = ImageDraw.Draw(canvas)

    x = _PADDING
    for card, icon in zip(cards, icons):
        canvas.paste(icon, (x, _PADDING), icon)
        label = card.name
        # Truncate overly long names to fit the tile width
        max_label_width = icon.width + _PADDING - 4
        while label and _text_width(draw, label, font) > max_label_width and len(label) > 4:
            label = label[:-2] + "…"
        text_width = _text_width(draw, label, font)
        label_x = x + (icon.width - text_width) // 2
        label_y = _PADDING + _ICON_HEIGHT + _LABEL_PAD
        draw.text((label_x, label_y), label, font=font, fill=_FG)
        x += icon.width + _PADDING

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except AttributeError:
        return len(text) * 7  # rough fallback for ancient PIL


__all__ = ["CardArt", "build_card_strip"]
