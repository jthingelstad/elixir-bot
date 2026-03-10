# Clash Royale API — Reference Index

This is a reference documentation set for the Clash Royale API (`https://api.clashroyale.com/v1`). Each file covers a domain of endpoints, their parameters, response types, and implementation notes.

Verified against live API: March 2026.

---

## Common Patterns

### Authentication

All endpoints require a Bearer token in the `Authorization` header. Tokens are created at [developer.clashroyale.com](https://developer.clashroyale.com) and are IP-restricted.

### Tag Encoding

Player, clan, and tournament tags start with `#` which must be URL-encoded as `%23` in path parameters:

`#2ABC` → `%232ABC`

This applies to all endpoints that accept a tag in the path (`playerTag`, `clanTag`, `tournamentTag`).

### Pagination

Endpoints that return lists support cursor-based pagination:

- `limit` — maximum number of items to return
- `after` — cursor for next page (from `paging.cursors.after` in response)
- `before` — cursor for previous page (from `paging.cursors.before` in response)

`after` and `before` are mutually exclusive. Omit both for the first page.

Paginated responses always wrap data in `{ items: [...], paging: { cursors: { after?, before? } } }`.

**Exceptions** — these endpoints return bare arrays (no wrapper, no pagination):
- `GET /players/{playerTag}/battlelog`
- `GET /events`

### Response Shapes

The API uses three distinct collection response patterns:

| Shape | Meaning | Examples |
|------|---------|----------|
| bare array | Non-paginated list response | `/events`, `/players/{playerTag}/battlelog` |
| `{ items: [...] }` | Non-paginated object wrapper | `/leaderboards`, `/globaltournaments`, `/players/{playerTag}/upcomingchests` |
| `{ items: [...], paging: { cursors: ... } }` | Paginated list response | `/locations`, `/clans/{tag}/members`, `/tournaments`, `/leaderboard/{id}` |

Do not assume that every response with `items` is paginated. Presence of `paging` is the reliable signal.

### Datetime Format

All datetime strings use: `YYYYMMDDTHHmmss.sssZ` (no dashes or colons)

Example: `20260309T135844.000Z`

### Error Responses

Error bodies are not fully consistent. `reason` is always present in observed responses, `message` is present on many `400` responses but often absent on `404`/`500` responses, and `type`/`detail` were not observed:

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope / IP mismatch |
| 404 | Resource not found |
| 410 | Endpoint permanently removed |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error shapes:
- `{ "reason": "badRequest", "message": "..." }`
- `{ "reason": "notFound" }`
- `{ "reason": "unknownException" }`

Notable `reason` values:
- `accessDenied` — IP doesn't match token or invalid token
- `notFound` — resource or endpoint not found
- `badRequest` — invalid parameter combinations or values
- `gone` — endpoint permanently removed
- `unknownException` — observed for invalid `leaderboardId`

### Optional vs Null

The API frequently omits optional fields entirely instead of returning `null`.

- Treat missing keys and nullable values as different cases
- Check key existence before reading optional fields
- Observed nullable fields are less common than absent fields

Examples:
- Player `clan`, `role`, `leagueStatistics`
- Tournament `description`, `startedTime`, `endedTime`
- Ranking entry `clan`
- Event `description` can be `null`

### Rate Limiting

The API has aggressive rate limiting. Observed behavior:
- No rate limit headers are returned in responses
- Exceeding limits results in 403 `accessDenied` (not always 429)
- Adding ~2 second delays between requests helps avoid rate limit issues
- Rate limits appear to be per-IP, not per-endpoint

### Caching

The API returns `cache-control: max-age=N` headers indicating server-side cache duration. Observed max-age values:

| Endpoint Category | Cache Duration |
|-------------------|---------------|
| `/events` | ~600s (10 min) |
| `/locations` | ~600s (10 min) |
| `/clans/{tag}`, `/currentriverrace` | ~120s (2 min) |
| `/players/{tag}`, `/battlelog`, `/upcomingchests` | ~60s (1 min) |
| `/leaderboards`, `/leaderboard/{id}` | ~60s (1 min) |
| `/cards` | ~30s |
| `/rankings/*`, `/pathoflegend/*` | ~60s (1 min) |

These are countdown timers — the actual value returned decreases as the cache ages. Responses are JSON with `Content-Type: application/json; charset=utf-8`.

### Pagination Details

- Cursors are base64-encoded JSON: `eyJwb3MiOjV9` decodes to `{"pos":5}`
- For paginated endpoints, `limit=0` usually returns `400 badRequest` with `Invalid 'limit' parameter used in the request`
- Some non-paginated endpoints ignore pagination params instead of rejecting them:
  - `/cards?limit=1` still returns the full catalog
  - `/cards?limit=0` still returns the full catalog
  - `/events?limit=5` still returns the full bare array
- No explicit maximum for `limit` — the API will return all results if no limit is set (observed 10,000 items from `/leaderboard/{id}`)
- When no more pages exist, `paging.cursors` is an empty object `{}`
- When more pages exist, `paging.cursors.after` contains the next cursor
- HEAD requests are not supported (return 404)

---

## Endpoint Reference

### Players — [players.md](players.md)
Player profiles, battle logs, and upcoming chests.

| Endpoint | Description |
|----------|-------------|
| `GET /players/{playerTag}` | Full player profile |
| `GET /players/{playerTag}/battlelog` | Recent battle history (bare array, ~48 battles) |
| `GET /players/{playerTag}/upcomingchests` | Upcoming chest sequence |

### Clans — [clans.md](clans.md)
Clan info, members, river race, and search.

| Endpoint | Description | Status |
|----------|-------------|--------|
| `GET /clans/{clanTag}` | Full clan info | Active |
| `GET /clans/{clanTag}/members` | Clan member list (paginated) | Active |
| `GET /clans/{clanTag}/currentriverrace` | Active river race state | Active |
| `GET /clans/{clanTag}/riverracelog` | Historical river race results | Active |
| ~~`GET /clans/{clanTag}/currentwar`~~ | ~~Classic clan war status~~ | **Removed** |
| ~~`GET /clans/{clanTag}/warlog`~~ | ~~Classic war log~~ | **Disabled** |
| `GET /clans` | Search clans by name/criteria | Active |

### Tournaments — [tournaments.md](tournaments.md)
Player-created tournaments.

| Endpoint | Description |
|----------|-------------|
| `GET /tournaments` | Search tournaments by name |
| `GET /tournaments/{tournamentTag}` | Full tournament details |

### Global Tournaments — [globaltournaments.md](globaltournaments.md)
Supercell-run global tournaments.

| Endpoint | Description |
|----------|-------------|
| `GET /globaltournaments` | List active global tournaments (may return empty) |

### Locations & Rankings — [locations.md](locations.md)
Location lookups, regional rankings, global tournament rankings, and league seasons.

| Endpoint | Description | Status |
|----------|-------------|--------|
| `GET /locations` | List all locations | Active |
| `GET /locations/{locationId}` | Single location by ID | Active |
| `GET /locations/{locationId}/rankings/players` | Player trophy rankings by location | Active (may be empty) |
| `GET /locations/{locationId}/rankings/clans` | Clan trophy rankings by location | Active |
| `GET /locations/{locationId}/rankings/clanwars` | Clan war rankings by location | Active |
| `GET /locations/{locationId}/pathoflegend/players` | Path of Legend rankings by location | Active |
| `GET /locations/global/rankings/tournaments/{tournamentTag}` | Global tournament player rankings | Active |
| `GET /locations/global/seasons` | List league seasons (YYYY-MM IDs) | Active |
| `GET /locations/global/seasonsV2` | List league seasons (extended) | **Broken** (null data) |
| `GET /locations/global/seasons/{seasonId}` | Single league season | Active |
| `GET /locations/global/seasons/{seasonId}/rankings/players` | Season trophy rankings | **May return notFound** |
| `GET /locations/global/pathoflegend/{seasonId}/rankings/players` | Season Path of Legend rankings | Active |

### Leaderboards — [leaderboards.md](leaderboards.md)
Game-mode-specific leaderboards (Merge Tactics, Touchdown, etc.).

| Endpoint | Description |
|----------|-------------|
| `GET /leaderboards` | List available leaderboards |
| `GET /leaderboard/{leaderboardId}` | Player rankings for a leaderboard |

### Cards — [cards.md](cards.md)
Game card catalog (121 standard + 4 Tower Troops).

| Endpoint | Description |
|----------|-------------|
| `GET /cards` | Full card list (standard + Tower Troops) |

### Challenges — [challenges.md](challenges.md)
Active and upcoming in-game challenges.

| Endpoint | Description | Status |
|----------|-------------|--------|
| `GET /challenges` | Current and upcoming challenges | **Returning notFound** |

### Events — [events.md](events.md)
Current in-game events (bare array response).

| Endpoint | Description |
|----------|-------------|
| `GET /events` | All active events |

---

## Supporting Reference

### Data Models — [models.md](models.md)
Complete reference of all API response types with verified field shapes and example data from live responses.

### Fan Content Policy — [fan-content-policy.md](fan-content-policy.md)
Supercell's rules for using their assets in fan-created content. Includes required disclaimer text, permitted/prohibited activities, and monetization guidelines.

---

## Cross-Reference Notes

- **Global tournaments → location rankings:** Use `tournamentTag` from `/globaltournaments` with `/locations/global/rankings/tournaments/{tournamentTag}` to get player rankings
- **Classic war endpoints are dead:** `currentwar` is permanently removed (410 Gone); `warlog` is disabled (404). Only river race endpoints work.
- **Trophy rankings vs Path of Legend:** These are separate leaderboards at the same location — `/rankings/players` for trophies, `/pathoflegend/players` for Path of Legend
- **Leaderboards vs location rankings:** `/leaderboards` cover game modes (Merge Tactics, Touchdown, etc.); `/locations/{id}/rankings` are geography-specific trophy rankings
- **Tournaments vs global tournaments:** `/tournaments` covers player-created tournaments; `/globaltournaments` covers Supercell-run events. Different model types (`Tournament` vs `LadderTournament`)
- **Season format:** League season IDs use `YYYY-MM` format (e.g. `2025-01`) — use `/seasons` endpoint to discover valid IDs
- **Bare array responses:** `/events` and `/battlelog` return bare JSON arrays, not the standard `{ items: [...] }` wrapper
- **Events ↔ Battles:** `Battle.eventTag` maps to `TrailEvent.eventTag` from `/events`
- **Challenges ↔ Events:** While `/challenges` is currently broken, active challenges still appear in `/events` (e.g. "Classic Challenge", "Grand Challenge")
- **Clan search flexibility:** `name` is not required — you can search by `locationId`, `minScore`, `minMembers`, or `maxMembers` alone. Search is case-insensitive.
- **River race seasons:** `seasonId` in river race log is a sequential integer (e.g. 127, 128, 129, 130) — not the YYYY-MM format used for league seasons
- **Global rankings:** Use `global` as locationId for worldwide rankings. Global player trophy rankings may be empty early in a season, but PoL and clan rankings are populated.
- **Season trophy rankings broken:** `/seasons/{id}/rankings/players` returns notFound for all tested seasons (2024-2026). PoL season rankings work. This appears to be a long-standing issue, not transient.
- **Odd error codes exist:** Invalid `locationId` values return `400 badRequest`, while invalid `leaderboardId` values currently return `500 unknownException` rather than `404`.
