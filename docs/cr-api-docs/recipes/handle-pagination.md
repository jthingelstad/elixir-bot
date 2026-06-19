# Handle Pagination

Use this recipe for endpoints whose response includes `paging`.

## Pattern

1. Request the first page without `after` or `before`.
2. Read `paging.cursors.after`.
3. If `after` exists, request the next page with `?after=<cursor>`.
4. Stop when `paging.cursors` is `{}` or has no `after`.

## Rules

- `after` and `before` are mutually exclusive.
- Do not treat every `{ items: [...] }` response as paginated.
- Presence of `paging` is the reliable pagination signal.
- `limit=0` usually returns `400 badRequest` on paginated endpoints.
- Some non-paginated endpoints ignore pagination params instead of rejecting them.

## Examples

Paginated:

- `GET /clans/{clanTag}/members`
- `GET /clans/{clanTag}/riverracelog`
- `GET /locations`
- `GET /tournaments`
- `GET /leaderboard/{leaderboardId}`

Not paginated:

- `GET /events`
- `GET /players/{playerTag}/battlelog`
- `GET /cards`
- `GET /leaderboards`
- `GET /globaltournaments`
