"""Elixir's outbound email — Fastmail JMAP, ported from Oliver (rwbookclub.com).

Elixir sends from elixir@poapkings.com and files sent mail into Sent/Elixir.
Send-only for now (no inbound polling): the release-notes flow is the first
caller. Token + address come from the environment (.env); nothing here reads a
central config module, matching elixir-bot's os.getenv convention.
"""
