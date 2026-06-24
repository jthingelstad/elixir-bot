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
    """One live reactive tick.

    `post_coro(channel_id, text, metadata=None) -> awaitable[bool]` is the bot's
    Discord send; it is awaited on `loop` from the worker thread.
    """
    from event_core.live.runtime import make_agent_poster

    def send(channel_id, text, scope, metadata=None) -> bool:
        del scope
        fut = asyncio.run_coroutine_threadsafe(post_coro(channel_id, text, metadata=metadata), loop)
        try:
            return bool(fut.result(timeout=45))
        except Exception:
            log.exception("v5 send bridge failed for channel %s", channel_id)
            return False

    return await asyncio.to_thread(_run_tick_sync, make_agent_poster(send))


_CUTOVER_MARKER = "cutover:v5"
_POST_CUTOVER_FOLLOWERS = ("detector:collection_level_milestone",)


def _cutover_done(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM projection_tracking WHERE projection_name=?", (_CUTOVER_MARKER,)
    ).fetchone() is not None


def _mark_cutover(conn, position: int) -> None:
    conn.execute(
        "INSERT INTO projection_tracking(projection_name,last_global_position,updated_at) "
        "VALUES(?,?,?) ON CONFLICT(projection_name) DO UPDATE SET "
        "last_global_position=excluded.last_global_position, updated_at=excluded.updated_at",
        (_CUTOVER_MARKER, position, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _fast_forward_missing_post_cutover_followers(conn, position: int) -> list[str]:
    fast_forwarded: list[str] = []
    for follower_name in _POST_CUTOVER_FOLLOWERS:
        row = conn.execute(
            "SELECT 1 FROM projection_tracking WHERE projection_name=?", (follower_name,)
        ).fetchone()
        if row is not None:
            continue
        conn.execute(
            "INSERT INTO projection_tracking(projection_name,last_global_position,updated_at) "
            "VALUES(?,?,?)",
            (follower_name, position, datetime.now(timezone.utc).isoformat()),
        )
        fast_forwarded.append(follower_name)
    if fast_forwarded:
        conn.commit()
    return fast_forwarded


def catch_up(force: bool = False) -> dict:
    """ONE-TIME go-live drain — runs only at the true first cutover (or with
    force=True). Ingests current CR state, advances (raising intents for the whole
    history), then fast-forwards the consumer past it all WITHOUT posting, so the
    first go-live never floods Discord with the entire historical backlog. A
    durable marker (`cutover:v5` in projection_tracking) records that this ran.

    On every SUBSEQUENT restart this is a NO-OP: the consumer resumes from its
    tracked position and the next reactive tick posts the (small) downtime backlog
    — so a restart can no longer silently fast-forward past unposted events. The
    consumer's own staleness policy bounds a long-outage backlog (see
    IntentConsumer), so skipping the drain here can't flood."""
    from event_core import config, db
    from event_core.application import ObservedWorld
    from event_core.live.engine import advance, apply_payloads
    from event_core.live.runtime import go_live_drain
    from event_core.live.tick import fetch_payloads

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    try:
        if _cutover_done(conn) and not force:
            head = app.recorder.max_notification_id() or 0
            fast_forwarded = _fast_forward_missing_post_cutover_followers(conn, head)
            return {
                "skipped": "cutover already complete; consumer resumes from tracked position",
                "event_log_head": head,
                "fast_forwarded_followers": fast_forwarded,
            }
        payloads = fetch_payloads()
        ts = datetime.now(timezone.utc).isoformat()
        ingested = apply_payloads(app, conn, payloads, ts)
        advanced = advance(app, conn)
        drained = go_live_drain(app, conn)
        _mark_cutover(conn, drained)
        return {"ingested": ingested, "advanced": advanced, "drained_to_position": drained}
    finally:
        conn.close()
