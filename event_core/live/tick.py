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


def fetch_payloads(member_tags: list[str]) -> dict:
    """Production fetch seam (cr_api). Returns the payloads dict run_tick expects.

    Wired at go-live; not run offline. cr_api.get_clan() uses the configured clan.
    """
    import cr_api

    clan = cr_api.get_clan()
    profiles = [cr_api.get_player(tag) for tag in member_tags]
    battlelogs = {tag: cr_api.get_player_battle_log(tag) for tag in member_tags}
    return {
        "player_profiles": profiles,
        "clan": clan,
        "battlelogs": battlelogs,
        "currentriverrace": cr_api.get_current_war(),
    }
