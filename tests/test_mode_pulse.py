"""The awareness read surfaces per-mode battle activity (mode_pulse) so the brain
sees ongoing activity in EVERY mode — ranked / 2v2 / events / ladder — not just
one-off pol_promotion events. Covers runtime/awareness/read.py:_mode_pulse: the
aggregate mode_mix + top-3 named members per mode (top_by_mode)."""
from __future__ import annotations

import json
from unittest.mock import patch

from runtime.awareness import read as read_mod


_SUMMARY = {
    "window_days": 7,
    "by_group": [
        {"mode_group": "ranked", "label": "Ranked", "members_active": 8,
         "battles": 254, "wins": 125, "losses": 129, "win_rate": 0.49, "trophy_delta": 540},
        {"mode_group": "two_v_two", "label": "2v2", "members_active": 14,
         "battles": 332, "wins": 161, "losses": 171, "win_rate": 0.485, "trophy_delta": 0},
    ],
    "by_game_mode": [{"x": "y"}] * 50,   # heavy keys that must NOT leak into the block
    "leaderboards": {"big": "blob"},
}

# What get_clan_mode_top_members returns: named members per mode label; ranked
# rows carry the PoL league.
_TOP_BY_MODE = {
    "Ranked": [{"member_ref": "Fullboat", "battles": 57, "wins": 33, "losses": 24,
                "win_rate": 0.579, "trophy_delta": 990, "league": 5}],
    "2v2": [{"member_ref": "bonus", "battles": 151, "wins": 83, "losses": 68,
             "win_rate": 0.55, "trophy_delta": 0}],
    "Events": [{"member_ref": "sikander sidhu", "battles": 185, "wins": 32,
                "losses": 153, "win_rate": 0.173, "trophy_delta": 0}],
}


def test_mode_pulse_surfaces_named_activity_across_all_modes():
    with (
        patch.object(read_mod.db, "get_clan_game_mode_summary", return_value=_SUMMARY),
        patch.object(read_mod.db, "get_clan_mode_top_members", return_value=_TOP_BY_MODE),
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

    # compact; no heavy summary keys leak.
    blob = json.dumps(mp, default=str)
    assert len(blob) < 3000
    assert "by_game_mode" not in mp and "leaderboards" not in mp


def test_mode_pulse_degrades_to_empty_shape_on_error():
    # _mode_pulse raising is caught by build_read's _load → default shape + _degraded.
    with patch.object(read_mod.db, "get_clan_game_mode_summary", side_effect=RuntimeError("boom")):
        r = read_mod.build_read()
    assert r["mode_pulse"] == {"mode_mix": [], "top_by_mode": {}, "window_days": read_mod._MODE_PULSE_DAYS}
    assert "mode_pulse" in r.get("_degraded", [])
