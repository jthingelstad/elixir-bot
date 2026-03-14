# Elixir Versions

This file tracks Elixir's major release eras.

The dates below are anchored to the git history using the representative commit that best marks the shift into that release. They are not meant to be semver package tags. They are project-era markers.

## v1.0 — The First Drop

**Date:** 2026-03-04  
**Representative commit:** `3a55e28` — `Add heartbeat, history store, game knowledge, and agentic tool use`

This is where Elixir first felt like an agent instead of a collection of scripts. The bot gained a real heartbeat loop, early memory/history, prompt-grounded game knowledge, and the first meaningful agentic tool-use behavior.

Notable changes:
- introduced the heartbeat-driven observation loop
- added game knowledge and stronger prompt grounding
- added agentic tool use for richer responses
- established the first real shape of Elixir as a living Discord bot

## v2.0 — King Tower Online

**Date:** 2026-03-07  
**Representative commit:** `6ee539a` — `Refactor Elixir onto the V2 data model`

This release grounded Elixir in structured game and clan state. It was the shift from prompt reconstruction and sparse snapshots toward a coherent, queryable operational model for roster, war, player, and conversation state.

Notable changes:
- refactored Elixir onto the structured database model
- improved roster, war, and player state normalization
- added stronger clanops status and runtime telemetry
- hardened channel routing and the internal package structure

## v2.1 — Mastery Path

**Date:** 2026-03-12  
**Representative commit:** `28026cb` — `Celebrate player badges and achievements`

This was the progression-expansion release. Elixir became much better at seeing and talking about how players grow over time, not just their current status.

Notable changes:
- added badge and achievement celebration
- expanded progression and battle-pulse style signals
- enriched roster payloads with richer player presentation
- improved player-intel refresh and progression coverage

## v2.5 — Royal Dispatch

**Date:** 2026-03-11  
**Representative commit:** `a4489c0` — `Announce formal POAP KINGS integration`

This is when Elixir clearly became more than a Discord bot. It became the publishing authority for poapkings.com and started dispatching structured content outward into the broader POAP KINGS ecosystem.

Notable changes:
- formalized the POAP KINGS website integration
- moved site publishing into a dedicated integration layer
- unified daily site content generation and publishing
- expanded recruiting and outward-facing content workflows

## v2.6 — Mirror Memory

**Date:** 2026-03-13  
**Representative commit:** `ec9059a` — `Store curated contextual memories`

This release made Elixir much more introspective and operationally inspectable. Memory, prompt-failure review, system signals, and admin observability became first-class concerns.

Notable changes:
- added curated contextual memory and scoped memory visibility
- improved prompt failure logging and review workflows
- expanded admin/status/db inspection surfaces
- strengthened operational signals and internal observability

## v3.0 — Three-Lane Elixir

**Date:** 2026-03-14  
**Representative anchor:** current working release on top of git history dated 2026-03-14

This is the multi-agent release. Elixir stopped behaving like one overloaded stream and became a set of channel-native subagents with distinct missions, shared identity, scoped memory, and a central activity model.

Notable changes:
- introduced channel-named subagents like `river-race`, `player-progress`, `clan-events`, `ask-elixir`, and `poapkings-com`
- replaced one-signal-one-post assumptions with multi-outcome routing
- added a central recurring activity registry
- formalized memory boundaries for public, leadership, and notification-only lanes
- gave Elixir a first-class release identity: `v3.0 "Three-Lane Elixir"`
