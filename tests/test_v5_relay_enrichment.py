"""The clan-chat relay is a REDRAFT of the posted Discord copy (single-pipeline
rule, 2026-07-04): same facts, in-game voice — never independent fact-gathering."""

from __future__ import annotations

from runtime.app import _v5_event_clan_chat_context


def test_relay_context_redrafts_the_posted_discord_copy():
    spec = {
        "objective": "career_wins_milestone",
        "target_player_name": "Th15_Guy",
        "target_player_tag": "#QLGYYG0Q",
        "copy": "Th15_Guy hit 2,000 career wins. Huge POAP KINGS milestone.",
    }
    meta = {"summary": {"detection_type": "career_wins_milestone", "milestone": 2000}}
    discord_copy = (
        "**Th15_Guy** just crossed 2,000 career wins — steady grinding since the "
        "spring, and the clan's donation leader last week too."
    )

    ctx = _v5_event_clan_chat_context(spec, meta, discord_copy)
    # The posted Discord copy is embedded verbatim as the redraft source…
    assert discord_copy in ctx
    assert "redraft" in ctx.lower()
    # …with the no-new-facts rule, and none of the old independent enrichment.
    assert "introduce nothing new" in ctx
    assert "CR READ TOOLS" not in ctx
    assert "Recent win JSON" not in ctx
    assert "Player profile facts JSON" not in ctx


def test_relay_context_without_posted_copy_stays_payload_only():
    spec = {"objective": "member_joined", "target_player_name": "Newcomer",
            "target_player_tag": "#NEW1", "copy": "Welcome to POAP KINGS, Newcomer!"}
    meta = {"summary": {"detection_type": "member_joined", "trophies": 4200}}
    ctx = _v5_event_clan_chat_context(spec, meta, None)
    # No fetched copy → facts JSON + fallback only; still no independent sources.
    assert "4200" in ctx or "4,200" in ctx
    assert "Discord post (source)" not in ctx
    assert "CR READ TOOLS" not in ctx
