"""Lane-registration consistency — a lane must exist at ALL THREE registration
points or the bot breaks. The #battle-feed outage (2026-07-05) was a lane wired
in DISCORD.md but missing from CHANNEL_LANE_CONFIG's validator, which hard-failed
on_ready. These tests keep the three points in sync.

The three points:
  1. runtime.lanes.channels() — what the live delivery path can actually resolve
     from DISCORD.md. (Was engine.recognition.compose.PREFIX_LANE until the
     deterministic proactive stack was retired in #207; the awareness loop now
     posts through this resolver.)
  2. prompts.CHANNEL_LANE_CONFIG keys — the on_ready validator's allowlist.
  3. prompts/lanes/<lane>.md — the lane voice file (a missing one silently drops
     composition to the fallback).
"""

from __future__ import annotations

import os

import prompts
from runtime.lanes import channels

_LANES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", "lanes")


def _routed_lanes() -> set[str]:
    """Every lane the live delivery path can resolve to a Discord channel."""
    return set(channels())


def _voice_file_lanes() -> set[str]:
    return {
        os.path.splitext(filename)[0]
        for filename in os.listdir(_LANES_DIR)
        if filename.endswith(".md")
    }


def test_every_routed_lane_is_in_channel_lane_config():
    """A lane the delivery path can resolve but the validator doesn't know →
    on_ready crash (the #battle-feed class)."""
    missing = _routed_lanes() - set(prompts.CHANNEL_LANE_CONFIG)
    assert not missing, (
        f"lanes routed by PREFIX_LANE but absent from CHANNEL_LANE_CONFIG "
        f"(on_ready would crash): {sorted(missing)}"
    )


def test_every_routed_lane_has_a_voice_file():
    """A lane with no prompts/lanes/<lane>.md silently drops composition to the
    deterministic fallback (the third registration point)."""
    missing = {
        lane
        for lane in _routed_lanes()
        if not os.path.isfile(os.path.join(_LANES_DIR, f"{lane}.md"))
    }
    assert not missing, (
        f"routed lanes with no prompts/lanes/<lane>.md voice file: {sorted(missing)}"
    )


def test_every_config_lane_has_a_discord_md_section():
    """Reverse direction: no CHANNEL_LANE_CONFIG lane is orphaned — each must map
    to a #channel section in DISCORD.md (Lane: <lane>)."""
    discord_md = open(
        prompts._DISCORD_MD
        if hasattr(prompts, "_DISCORD_MD")
        else os.path.join(os.path.dirname(_LANES_DIR), "DISCORD.md")
    ).read()
    lanes_in_md = {
        line.split(":", 1)[1].strip()
        for line in discord_md.splitlines()
        if line.strip().lower().startswith("lane:")
    }
    orphans = set(prompts.CHANNEL_LANE_CONFIG) - lanes_in_md
    assert not orphans, (
        f"CHANNEL_LANE_CONFIG lanes with no `Lane:` mapping in DISCORD.md: {sorted(orphans)}"
    )


def test_every_config_lane_has_a_voice_file():
    """Every configured lane also needs its voice file (covers config lanes that
    aren't recognition-routed, e.g. ask-elixir/general)."""
    missing = {
        lane
        for lane in prompts.CHANNEL_LANE_CONFIG
        if not os.path.isfile(os.path.join(_LANES_DIR, f"{lane}.md"))
    }
    assert not missing, (
        f"CHANNEL_LANE_CONFIG lanes with no prompts/lanes/<lane>.md: {sorted(missing)}"
    )


def test_lane_voice_files_exactly_match_the_channel_registry():
    """An unreferenced lane prompt is dead because prompts are loaded by name,
    never globbed; keep the prompt corpus equal to the live registry."""
    assert _voice_file_lanes() == set(prompts.CHANNEL_LANE_CONFIG)


def test_every_discord_md_lane_is_in_channel_lane_config():
    """Forward direction the on_ready validator enforces: a `Lane:` in DISCORD.md
    with no CHANNEL_LANE_CONFIG entry hard-crashes on_ready. This is the exact
    gap that broke the #elixir deploy (2026-07-10): the channel was in DISCORD.md
    before its lane was registered in the config."""
    discord_md = open(
        prompts._DISCORD_MD
        if hasattr(prompts, "_DISCORD_MD")
        else os.path.join(os.path.dirname(_LANES_DIR), "DISCORD.md")
    ).read()
    lanes_in_md = {
        line.split(":", 1)[1].strip()
        for line in discord_md.splitlines()
        if line.strip().lower().startswith("lane:")
    }
    missing = lanes_in_md - set(prompts.CHANNEL_LANE_CONFIG)
    assert not missing, (
        f"DISCORD.md lanes absent from CHANNEL_LANE_CONFIG (on_ready would crash): "
        f"{sorted(missing)}"
    )
