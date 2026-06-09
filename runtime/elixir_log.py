"""Operational event logging to the private #elixir-log webhook."""

from __future__ import annotations

import asyncio
import logging
import os

import requests

log = logging.getLogger("elixir")

WEBHOOK_ENV = "ELIXIR_LOG_WEBHOOK_URL"
USERNAME_ENV = "ELIXIR_LOG_WEBHOOK_USERNAME"
DEFAULT_USERNAME = "Elixir"
DISCORD_WEBHOOK_LIMIT = 2000
DISCORD_WEBHOOK_CHUNK = 1900


def _webhook_url() -> str:
    return (os.getenv(WEBHOOK_ENV) or "").strip()


def enabled() -> bool:
    return bool(_webhook_url())


def _chunks(content: str) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    if len(text) <= DISCORD_WEBHOOK_LIMIT:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        chunks.append(remaining[:DISCORD_WEBHOOK_CHUNK].rstrip())
        remaining = remaining[DISCORD_WEBHOOK_CHUNK:].lstrip()
    return [chunk for chunk in chunks if chunk]


def post_event(content: str, *, username: str | None = None) -> bool:
    url = _webhook_url()
    if not url:
        return False
    sender = username or os.getenv(USERNAME_ENV) or DEFAULT_USERNAME
    ok = True
    for chunk in _chunks(content):
        try:
            response = requests.post(
                url,
                json={
                    "content": chunk,
                    "username": sender,
                    "allowed_mentions": {"parse": []},
                },
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("elixir-log webhook post failed: %s", exc)
            ok = False
            break
    return ok


async def post_event_async(content: str, *, username: str | None = None) -> bool:
    return await asyncio.to_thread(post_event, content, username=username)
