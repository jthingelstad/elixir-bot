# Clash Royale API – Leaderboards Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header

---

## Endpoints

### GET /leaderboards
List all available leaderboards (game modes / trophy roads).

**No parameters**

**Returns:** `{ items: [...] }` — array of leaderboard metadata objects

**Leaderboard metadata shape:**
```json
{ "id": 170000019, "name": "Merge Tactics" }
```

**Example response (subset, observed):**
```json
{
  "items": [
    { "id": 170000019, "name": "Merge Tactics" },
    { "id": 170000018, "name": "Touchdown" },
    { "id": 170000017, "name": "Merge Tactics" },
    { "id": 170000015, "name": "Mega Draft Challenge" },
    { "id": 170000004, "name": "2v2 League" },
    { "id": 170000005, "name": "Retro Royale" },
    { "id": 170000001, "name": "Goblin Queen's Journey" },
    { "id": 270849, "name": "Merge Tactics" }
  ]
}
```

Note: Multiple leaderboards can share the same `name` (e.g. "Merge Tactics" appears multiple times with different IDs — likely different seasons or variants). The March 2026 response contained 15 items.

---

### GET /leaderboard/{leaderboardId}
Get players ranked on a specific leaderboard.

**Path:** `leaderboardId` (required, integer) — obtain from `GET /leaderboards`
**Query:** `limit`, `after`, `before` (pagination cursors — mutually exclusive)

**Returns:** `{ items: [...], paging: { cursors: { ... } } }`

**Ranking entry shape:**
```json
{
  "tag": "#PU9RCVYUG",
  "name": "FJ21",
  "rank": 1,
  "score": 4047,
  "clan": { "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | Player tag |
| `name` | string | Player name |
| `rank` | integer | Position on leaderboard |
| `score` | integer | Leaderboard score (mode-specific trophies/points) |
| `clan` | object | Optional — `{ tag, name, badgeId }` — absent if player has no clan |

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. Invalid `leaderboardId` values currently return `500 {"reason":"unknownException"}` rather than a clean `404`. `type`/`detail` were not observed.

---

## Agent Notes
- Two-step pattern: fetch `/leaderboards` first to get valid `leaderboardId` values, then fetch `/leaderboard/{id}` for player rankings
- `leaderboardId` is an integer (unlike most CR API identifiers which are string tags)
- Distinct from `/locations/{locationId}/rankings` — these leaderboards cover specific game modes (Merge Tactics, Touchdown, etc.), not geographic trophy rankings
- `/leaderboards` (plural) lists metadata; `/leaderboard/{id}` (singular) returns players — note the inconsistent singular/plural naming
- Multiple leaderboards may share a name but have different IDs — likely representing different seasons of the same mode
- `clan` field on ranking entries is optional — omitted for clanless players
- **No default limit:** `/leaderboard/{id}` returns ALL entries (up to 10,000 observed) when no `limit` is specified. Set a limit if you don't need the full list.
- `/leaderboard/{id}?limit=0` returns `400 badRequest`
- Observed leaderboard names: Merge Tactics, Touchdown, Mega Draft Challenge, 2v2 League, Retro Royale, Goblin Queen's Journey
- IDs in range 170000xxx for newer leaderboards; some older ones have smaller IDs (e.g. 270849)
