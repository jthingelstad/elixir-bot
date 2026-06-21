"""Tick orchestrator — one live cycle.

`run_tick` is the testable core: apply fetched payloads → advance Followers
incrementally → consume intents to the poster. `fetch_payloads` is the production
seam (cr_api); it is wired at go-live and not exercised offline.
"""
from __future__ import annotations


def run_tick(app, conn, payloads: dict, observed_at: str, poster) -> dict:
    """One cycle over already-fetched payloads. `poster` is callable(intent)->bool."""
    from event_core.live.discord_consumer import IntentConsumer
    from event_core.live.engine import advance, apply_payloads

    ingested = apply_payloads(app, conn, payloads, observed_at)
    advanced = advance(app, conn)
    consumer = IntentConsumer(app, conn, poster)
    posted = consumer.run()
    return {"ingested": ingested, "advanced": advanced, "posted": posted, "dropped": consumer.dropped}


def fetch_payloads(member_tags: list[str] | None = None) -> dict:
    """Production fetch seam (cr_api). Returns the payloads dict run_tick expects.
    Derives the member tag list from the live clan roster if not given.

    Wired at go-live. cr_api auto-persists raw payloads to ELIXIR_DB_PATH — point
    that at a throwaway during the offline rehearsal to keep live elixir.db pristine.
    """
    import cr_api

    clan = cr_api.get_clan()
    if member_tags is None:
        member_tags = [m["tag"] for m in (clan.get("memberList") or []) if m.get("tag")]
    profiles = [cr_api.get_player(tag) for tag in member_tags]
    battlelogs = {tag: cr_api.get_player_battle_log(tag) for tag in member_tags}
    return {
        "player_profiles": profiles,
        "clan": clan,
        "battlelogs": battlelogs,
        "currentriverrace": cr_api.get_current_war(),
    }


def run_once(poster, observed_at: str | None = None) -> dict:
    """One production tick against the configured v5 stores: fetch -> tick.

    The scheduler calls this on the heartbeat interval at go-live.
    """
    from datetime import datetime, timezone

    from event_core import config, db
    from event_core.application import ObservedWorld

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    try:
        payloads = fetch_payloads()
        ts = observed_at or datetime.now(timezone.utc).isoformat()
        return run_tick(app, conn, payloads, ts, poster)
    finally:
        conn.close()
