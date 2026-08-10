"""The awareness read surfaces per-mode battle activity (mode_pulse) so the brain
sees ongoing activity in EVERY mode — ranked / 2v2 / events / ladder — not just
one-off pol_promotion events. Covers runtime/awareness/read.py:_mode_pulse: the
aggregate mode_mix + top-3 named members per mode (top_by_mode)."""

from __future__ import annotations

import json
from unittest.mock import patch

from runtime.awareness import read as read_mod

_CAPABILITY = {
    "capability": "clan_game_modes",
    "contract_version": 1,
    "window_days": 7,
    "modes": {
        "ranked": {
            "mode_group": "ranked",
            "label": "Ranked",
            "members_active": 8,
            "battles": 254,
            "wins": 125,
            "losses": 129,
            "win_rate": 0.49,
            "trophy_delta": 540,
            "top_members": [
                {
                    "member_ref": "Fullboat",
                    "battles": 57,
                    "wins": 33,
                    "losses": 24,
                    "win_rate": 0.579,
                    "trophy_delta": 990,
                    "league": 5,
                }
            ],
        },
        "two_v_two": {
            "mode_group": "two_v_two",
            "label": "2v2",
            "members_active": 14,
            "battles": 332,
            "wins": 161,
            "losses": 171,
            "win_rate": 0.485,
            "trophy_delta": 0,
            "top_members": [
                {
                    "member_ref": "bonus",
                    "battles": 151,
                    "wins": 83,
                    "losses": 68,
                    "win_rate": 0.55,
                    "trophy_delta": 0,
                }
            ],
        },
        "special_event": {
            "mode_group": "special_event",
            "label": "Events",
            "members_active": 9,
            "battles": 185,
            "wins": 32,
            "losses": 153,
            "win_rate": 0.173,
            "trophy_delta": 0,
            "top_members": [
                {
                    "member_ref": "sikander sidhu",
                    "battles": 185,
                    "wins": 32,
                    "losses": 153,
                    "win_rate": 0.173,
                    "trophy_delta": 0,
                }
            ],
        },
    },
    "game_modes": [{"x": "y"}] * 50,
    "events": {
        "activity": [
            {
                "event_name": "Draft Festival",
                "event_tag": "#EVENT_A",
                "members_active": 9,
                "battles": 180,
                "wins": 95,
                "losses": 85,
                "win_rate": 0.5278,
                "share_of_clan_battles": 0.36,
                "share_of_special_event_battles": 0.9,
                "previous_window_battles": 0,
                "previous_window_members_active": 0,
                "battle_change": 180,
                "current_to_previous_ratio": None,
                "new_in_window": True,
                "latest_battle": "2026-08-09T12:00:00Z",
                "top_members": [
                    {
                        "member_ref": "Alpha",
                        "event_battles": 42,
                        "wins": 24,
                        "losses": 18,
                        "win_rate": 0.5714,
                        "latest_event_battle": "2026-08-09T11:00:00Z",
                    }
                ],
            },
            {
                "event_name": "Mirror Festival",
                "event_tag": "#EVENT_B",
                "members_active": 3,
                "battles": 20,
                "wins": 8,
                "losses": 12,
                "win_rate": 0.4,
                "share_of_clan_battles": 0.04,
                "share_of_special_event_battles": 0.1,
                "previous_window_battles": 10,
                "previous_window_members_active": 2,
                "battle_change": 10,
                "current_to_previous_ratio": 2.0,
                "new_in_window": False,
                "latest_battle": "2026-08-09T10:00:00Z",
                "top_members": [],
            },
        ]
    },
    "side_modes": {"leaderboards": {"big": "blob"}},
}


def test_mode_pulse_surfaces_named_activity_across_all_modes():
    with patch.object(
        read_mod.game_mode_capability, "get_clan_game_modes", return_value=_CAPABILITY
    ):
        mp = read_mod._mode_pulse(conn=None)

    assert mp["window_days"] == 7
    # aggregate mode mix — all modes present, summary fields only.
    modes = {m["mode"] for m in mp["mode_mix"]}
    assert "Ranked" in modes and "2v2" in modes

    # named members for EVERY mode, not just ranked.
    tbm = mp["top_by_mode"]
    assert set(tbm) == {"Ranked", "2v2", "Events"}
    assert tbm["Events"][0]["member_ref"] == "sikander sidhu"
    assert tbm["2v2"][0]["battles"] == 151
    # ranked entries carry PoL league; non-ranked don't.
    assert tbm["Ranked"][0]["league"] == 5
    assert "league" not in tbm["2v2"][0]

    # Event-tagged activities stay distinct and carry the comparative facts
    # that let awareness notice the clan shifting into one event.
    assert [event["name"] for event in mp["special_events"]] == [
        "Draft Festival",
        "Mirror Festival",
    ]
    assert mp["special_events"][0]["share_of_clan_battles"] == 0.36
    assert mp["special_events"][0]["previous_window_battles"] == 0
    assert mp["special_events"][0]["new_in_window"] is True
    assert mp["special_events"][0]["top_members"][0]["member_ref"] == "Alpha"

    # compact; no heavy summary keys leak.
    blob = json.dumps(mp, default=str)
    assert len(blob) < 3000
    assert "by_game_mode" not in mp and "leaderboards" not in mp


def test_mode_pulse_degrades_to_empty_shape_on_error():
    # _mode_pulse raising is caught by build_read's _load → default shape + _degraded.
    with patch.object(
        read_mod.game_mode_capability,
        "get_clan_game_modes",
        side_effect=RuntimeError("boom"),
    ):
        r = read_mod.build_read()
    assert r["mode_pulse"] == {
        "mode_mix": [],
        "top_by_mode": {},
        "special_events": [],
        "window_days": read_mod._MODE_PULSE_DAYS,
    }
    assert "mode_pulse" in r.get("_degraded", [])
