"""Live service bridge — run the v5 reactive tick on the bot's event loop.

Used by runtime/app.py. The ingest + advance + intent consumption run in a worker
thread (sync v5 code); the consumer's poster bridges each Discord send back to the
bot's asyncio loop via run_coroutine_threadsafe and blocks for the result, so the
IntentConsumer's at-least-once semantics hold (fulfil only after a confirmed send).

This is additive: the v5 stores (elixir-v5*.db) are independent of the bot's
operational elixir.db, so no DB repoint is needed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("elixir.event_core")


def _run_tick_sync(poster) -> dict:
    from event_core import config, db
    from event_core.application import ObservedWorld
    from event_core.live.tick import fetch_payloads, run_tick

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    try:
        payloads = fetch_payloads()  # live CR API
        ts = datetime.now(timezone.utc).isoformat()
        return run_tick(app, conn, payloads, ts, poster)
    finally:
        conn.close()


async def reactive_tick(loop, post_coro) -> dict:
    """One live reactive tick. `post_coro(channel_id, text) -> awaitable[bool]` is
    the bot's Discord send; it is awaited on `loop` from the worker thread."""
    from event_core.live.runtime import make_agent_poster

    def send(channel_id, text, scope) -> bool:
        fut = asyncio.run_coroutine_threadsafe(post_coro(channel_id, text), loop)
        try:
            return bool(fut.result(timeout=45))
        except Exception:
            log.exception("v5 send bridge failed for channel %s", channel_id)
            return False

    return await asyncio.to_thread(_run_tick_sync, make_agent_poster(send))


def catch_up() -> dict:
    """Go-live, run ONCE at startup: ingest current CR state and advance (raising
    intents for everything that happened during downtime), then drain ALL intents
    WITHOUT posting. Reactive posting then begins fresh from the next tick, so
    go-live never floods Discord with the historical/downtime backlog."""
    from event_core import config, db
    from event_core.application import ObservedWorld
    from event_core.live.engine import advance, apply_payloads
    from event_core.live.runtime import go_live_drain
    from event_core.live.tick import fetch_payloads

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    try:
        payloads = fetch_payloads()
        ts = datetime.now(timezone.utc).isoformat()
        ingested = apply_payloads(app, conn, payloads, ts)
        advanced = advance(app, conn)
        drained = go_live_drain(app, conn)
        return {"ingested": ingested, "advanced": advanced, "drained_to_position": drained}
    finally:
        conn.close()
