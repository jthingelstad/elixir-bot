"""The Observatory — Elixir's admin-only web interface.

In-process aiohttp app (same asyncio loop as the Discord bot) so it can see
live internal state: the war clock, the poll plan, tick-in-progress counters.
Bound to loopback, exposed tailnet-only via `tailscale serve`; auth is the
Tailscale identity header (same model as studio-thing's workshop webapp).
"""
