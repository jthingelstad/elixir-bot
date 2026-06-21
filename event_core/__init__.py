"""Elixir Event Core (v5).

A bounded event-sourcing core built on the `eventsourcing` library. The event
store (`elixir-v5-events.db`) is the authoritative write model; projections live
in `elixir-v5.db`. See docs/tasks/elixir-event-sourcing-migration.md.

Status: foundation slice (Player profile observation -> current-profile
projection -> exact parity vs frozen legacy). Built with Elixir stopped; nothing
here touches production until an explicit cutover.
"""
