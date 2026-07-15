"""#leader-actions is cards-only: compose_and_post must refuse narrative there.

Action cards reach the channel via post_leader_action_card (channel.send);
composed narrative (digests, reminders) must not — it belongs in #leaders
(leader-lounge). Guards the invariant Jamie set on 2026-07-06.
"""

from __future__ import annotations

import asyncio

import prompts
import runtime.discord_posting as dp


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.name = "c"


def test_narrative_refused_to_leader_actions(monkeypatch):
    called = []
    import elixir_agent

    monkeypatch.setattr(
        elixir_agent,
        "generate_channel_update",
        lambda *a, **k: called.append(1) or "hello",
    )
    la_id = prompts.discord_singleton_lane("arena-relay")["id"]  # #leader-actions
    ok = asyncio.run(dp.compose_and_post(_Chan(la_id), lane="arena-relay", context="x"))
    assert ok is False
    assert called == [], "guard must short-circuit before composing narrative"


def test_narrative_allowed_to_leaders_channel(monkeypatch):
    called = []
    import elixir_agent

    # empty copy → compose_and_post returns False AFTER composing; that the
    # composer ran at all proves the guard let a non-leader-actions channel pass.
    monkeypatch.setattr(
        elixir_agent, "generate_channel_update", lambda *a, **k: called.append(1) or ""
    )
    ll_id = prompts.discord_singleton_lane("leader-lounge")["id"]  # #leaders
    asyncio.run(dp.compose_and_post(_Chan(ll_id), lane="leader-lounge", context="x"))
    assert called == [1], "guard must not block #leaders"
