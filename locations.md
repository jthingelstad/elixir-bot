# Clash Royale API – Locations Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header
Tag encoding: `#2ABC` → `%232ABC` in path

---

## Endpoints

### GET /locations
List all available locations (regions + countries).

**Query:** `limit`, `after`, `before` (pagination cursors — mutually exclusive)

**Returns:** `{ items: [...], paging: { cursors: { ... } } }`

**Location shape:**
```json
{ "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" }
```

Locations include both regions (continents) and countries:
- Regions: `{ id: 57000000, name: "Europe", isCountry: false }` — no `countryCode`
- Countries: `{ id: 57000249, name: "United States", isCountry: true, countryCode: "US" }`
- Special: `{ id: 57000006, name: "International", isCountry: false }`

262 total locations (8 regions + 254 countries). All IDs in range 57000000-57000261.

**Region IDs:**
| ID | Name |
|----|------|
| 57000000 | Europe |
| 57000001 | North America |
| 57000002 | South America |
| 57000003 | Asia |
| 57000004 | Oceania |
| 57000005 | Africa |
| 57000006 | International |
| 57000261 | Unknown |

**Common country IDs:**
| ID | Name | Code |
|----|------|------|
| 57000021 | Australia | AU |
| 57000038 | Brazil | BR |
| 57000047 | Canada | CA |
| 57000056 | China | CN |
| 57000087 | France | FR |
| 57000094 | Germany | DE |
| 57000113 | India | IN |
| 57000120 | Italy | IT |
| 57000122 | Japan | JP |
| 57000153 | Mexico | MX |
| 57000193 | Russia | RU |
| 57000216 | South Korea | KR |
| 57000218 | Spain | ES |
| 57000248 | United Kingdom | GB |
| 57000249 | United States | US |

---

### GET /locations/{locationId}
Get a single location by ID.

**Path:** `locationId` (required, integer)

**Returns:** `Location` object

```json
{ "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" }
```

Note: No `localizedName` field was observed in responses. `countryCode` is absent for regions where `isCountry: false`.

---

## Location Rankings

### GET /locations/{locationId}/rankings/players
Get trophy leaderboard for players in a location.

**Path:** `locationId` (required)
**Query:** `limit`, `after`, `before`

**Returns:** `{ items: [...], paging: { ... } }`

Note: May return empty `items` array if no ranking data is available for the current season yet.

---

### GET /locations/{locationId}/rankings/clans
Get trophy leaderboard for clans in a location.

**Path:** `locationId` (required)
**Query:** `limit`, `after`, `before`

**Returns:** `{ items: [...], paging: { ... } }`

**ClanRanking shape:**
```json
{
  "tag": "#9LGR9PYY",
  "name": "War Knights",
  "rank": 1,
  "previousRank": 1,
  "location": { "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" },
  "clanScore": 132637,
  "members": 50,
  "badgeId": 16000038
}
```

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | Clan tag |
| `name` | string | |
| `rank` | integer | Current rank |
| `previousRank` | integer | Previous rank (-1 if new/unranked) |
| `location` | Location | Full location object |
| `clanScore` | integer | |
| `members` | integer | Member count |
| `badgeId` | integer | |

---

### GET /locations/{locationId}/rankings/clanwars
Get clan war (river race) leaderboard for a location.

**Path:** `locationId` (required)
**Query:** `limit`, `after`, `before`

**Returns:** Same shape as clan rankings. The `clanScore` here reflects river race/war performance, not overall trophies.

---

### GET /locations/{locationId}/pathoflegend/players
Get Path of Legend player rankings for a location (current season).

**Path:** `locationId` (required)
**Query:** `limit`, `after`, `before`

**Returns:** `{ items: [...], paging: { ... } }`

**PlayerPathOfLegendRanking shape:**
```json
{
  "tag": "#99GU92P0",
  "name": "TT-shadow.cr29",
  "expLevel": 78,
  "eloRating": 2247,
  "rank": 1,
  "clan": { "tag": "#R99R8G8J", "name": "Skyline", "badgeId": 16000134 }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | Player tag |
| `name` | string | |
| `expLevel` | integer | King level |
| `eloRating` | integer | Path of Legend ELO rating |
| `rank` | integer | |
| `clan` | object | Optional — absent if not in a clan |

---

## Global Tournament Rankings

### GET /locations/global/rankings/tournaments/{tournamentTag}
Get global player rankings for a specific tournament.

**Path:** `tournamentTag` (required, URL-encoded)
**Query:** `limit`, `after`, `before`

**Returns:** `LadderTournamentRankingList`

Status note:
- Endpoint is documented and appears active
- Success shape was not re-verified in the March 2026 pass because `/globaltournaments` returned no active tournaments
- For agentic use, treat this endpoint as requiring fresh live validation before depending on exact field-level schema

---

## League Seasons (Global)

### GET /locations/global/seasons
List all historical league seasons.

**No parameters**

**Returns:** `{ items: [...], paging: { cursors: {} } }`

Items are `{ id: "YYYY-MM" }` objects. Note: early seasons (2016-2017) have duplicate entries for the same month. Season IDs go from `2016-02` through the most recent completed season.

---

### GET /locations/global/seasonsV2
List league seasons with extended detail.

**No parameters**

**Returns:** `{ items: [...], paging: { cursors: {} } }`

**Current status:** Returns 137 items with all null fields (`{ code: null, uniqueId: null, endTime: null }`). This endpoint is broken as of March 2026 — returns the correct count of seasons but with no data. Use `/seasons` (V1) instead.

---

### GET /locations/global/seasons/{seasonId}
Get a single league season by ID.

**Path:** `seasonId` (required) — format `YYYY-MM`

**Returns:** `LeagueSeason` — `{ id: "YYYY-MM" }`

---

### GET /locations/global/seasons/{seasonId}/rankings/players
Get top trophy player rankings for a completed league season.

**Path:** `seasonId` (required)
**Query:** `limit`, `after`, `before`

**Returns:** `PlayerRankingList`

**Current status:** Returns `{"reason":"notFound"}` for all tested seasons (2024 through 2026). In the March 2026 pass the body contained only `reason` (no `message`). This endpoint appears to be permanently broken. Use Path of Legend season rankings instead.

---

### GET /locations/global/pathoflegend/{seasonId}/rankings/players
Get top Path of Legend player rankings for a specific season.

**Path:** `seasonId` (required) — format `YYYY-MM`
**Query:** `limit`, `after`, `before`

**Returns:** `{ items: [...], paging: { ... } }` — same shape as location PoL rankings

**Example:** `/locations/global/pathoflegend/2025-01/rankings/players?limit=2` returns:
```json
{
  "items": [
    { "tag": "#G9YV9GR8R", "name": "Mohamed Light", "expLevel": 70, "eloRating": 3874, "rank": 1, "clan": { ... } },
    { "tag": "#U8RYGC8GU", "name": "Polaris✨DEE", "expLevel": 57, "eloRating": 3844, "rank": 2, "clan": { ... } }
  ]
}
```

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Resource not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. Invalid `locationId` values return `400 badRequest` with a message such as `Unknown value for parameter locationId`. `type`/`detail` were not observed.

---

## Agent Notes
- `locationId` for global endpoints is the literal string `global` — e.g. `/locations/global/seasons`
- `/seasonsV2` is broken (all null fields) — use `/seasons` (V1) to get season IDs
- `seasonId` format is `YYYY-MM` (e.g. `2025-01`). Seasons go back to `2016-02`. Early seasons (2016-2017) have duplicate entries.
- Trophy rankings (`/rankings/players`) and Path of Legend rankings (`/pathoflegend/players`) are separate leaderboards for the same location
- `/rankings/clanwars` reflects river race performance, not classic war
- To get a `locationId` for a known country, fetch `/locations` and match by `countryCode` or `name`
- **Season trophy rankings are broken** — `/seasons/{id}/rankings/players` returns notFound for all seasons. Use PoL season rankings.
- `previousRank` of `-1` in clan rankings means the clan was not previously ranked
- **Global player trophy rankings** may return empty results early in a season. PoL global rankings and clan rankings work consistently.
- `/locations` returns all 262 locations with no limit by default. No pagination needed for the full list.
- `/locations?limit=0` returns `400 badRequest`
- Cache duration: location data is cached ~10 minutes server-side; rankings ~1 minute
