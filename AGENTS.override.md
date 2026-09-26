# Elixir Codex instructions

> **Retired 2026-09-26.** Elixir Bot no longer runs. It was POAP KINGS' clan
> agent from February to September 2026, and the projects that replaced it grew
> out of it:
> [Elixir MCP](https://github.com/jthingelstad/elixir-mcp) records the game for
> many clans,
> [elixir-mcp-collector](https://github.com/jthingelstad/elixir-mcp-collector)
> fetches from the API,
> [elixir-mcp-discord](https://github.com/jthingelstad/elixir-mcp-discord) is
> the Discord agent,
> [Elixir Clan](https://github.com/jthingelstad/clan.poapkings.com) handles
> clan management and awards, and
> [Elixir Drop](https://github.com/jthingelstad/drop.poapkings.com) is the game.
> The code and history are kept here as they were on the last day. Nothing in
> this repository is operated or maintained any more; do not restart it.

This concise Codex entry point keeps the automatically loaded instruction chain within
Codex's project-document budget. `AGENTS.md` remains the full architecture reference;
read only the sections relevant to the current objective or change.

## Start here

- Objective-owner runs read `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and
  the selected objective file completely.
- `AGENTS.md` owns architecture, schema, runtime, testing, and implementation details.
- Repository skills are procedures, not workers. Read a selected skill completely.

## Working agreements

- Measure live or persisted evidence before changing anything. A healthy no-op is a
  complete result.
- Fix clear gaps at the source with proportionate regression coverage. Prompt guards,
  warnings, and ticket handoffs are last resorts.
- GitHub Issues are an exception ledger for multi-run work, external blockers, durable
  audits, and Jamie decisions. Same-run safe work needs no issue.
- Run `AGENT-TEAM/scripts/preflight.sh` first. Dirty, behind, diverged, detached, or
  unexpectedly ahead state makes mutation read-only.
- Acquire the objective lease only before mutation. Never stash, reset, clean, overwrite,
  or publish another worker's changes or pre-existing commits.
- Run focused checks plus `scripts/gates.sh` before committing directly to `main`. Verify
  deployment and natural acceptance where applicable.
- Jamie retains member-visible direction, early or synthetic member activity, leadership
  decisions, mass communication, and irreversible actions. Ask one concrete yes/no
  question when that boundary is reached.
- End objective runs using the common `HEALTHY`, `CHANGED`, `WATCHING`, `BLOCKED`, or
  `NEEDS JAMIE` closeout contract in `AGENT-TEAM/WORKFLOW.md`.

## High-risk invariants

- `elixir-v51.db` is the live operational database. Rehearse schema changes on a copy.
- `elixir-telemetry.db` is admin-only evidence; product behavior must never depend on it.
- `cr_api.py` is the sole Clash Royale API ingress.
- Never manufacture Discord, clan-chat, or member-facing traffic for acceptance.
- Never expose credentials, environment contents, member-private evidence, or complete
  sensitive logs.
