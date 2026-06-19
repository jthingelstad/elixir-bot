# Agent Guide

This repository is an agent-first reference for the public Clash Royale API.

## Start Here

1. Read [index.md](index.md) for global API rules, response-shape patterns, pagination, errors, caching, and endpoint
   discovery.
2. Read the endpoint file for the route you are implementing.
3. Read only the focused model file(s) in [models/](models/) needed for that route.
4. Use [data/endpoints.json](data/endpoints.json) for machine-readable endpoint routing.

## Important Rules

- All internal links must be relative. Do not add filesystem-specific absolute links.
- Tags in path parameters start with `#` and must be URL-encoded as `%23`.
- Do not assume `{ items: [...] }` means pagination. Presence of `paging` is the reliable signal.
- Optional fields are usually absent, not present as `null`.
- Nullable fields are explicitly called out where observed.
- Treat endpoints marked broken, disabled, removed, or undocumented as operational constraints.
- Do not add notes about specific downstream consumers of this repo.

## Official Docs Comparison

The official Swagger UI is a useful baseline, but observed live API behavior is higher-confidence when the two conflict.
Keep both signals explicit instead of silently replacing one with the other.

The official Swagger UI currently lists these endpoint groups:

- `clans`
- `players`
- `cards`
- `tournaments`
- `locations`
- `events`
- `leaderboards`
- `globaltournaments`

The local [challenges.md](challenges.md) file documents historical/observed behavior for an endpoint that is not
currently shown in the official Swagger UI.
