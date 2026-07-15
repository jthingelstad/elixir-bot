"""Shared domain capabilities consumed by every Elixir surface.

Capabilities own structured, audience-neutral answers derived from the engine's
events and projections. Agent tools, awareness, reports, and admin surfaces are
adapters over these contracts; they do not reimplement the underlying facts.
"""

from capabilities.game_modes import get_clan_game_mode_windows, get_clan_game_modes

__all__ = ["get_clan_game_mode_windows", "get_clan_game_modes"]
