# Clash Royale API – Events Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header

---

## Endpoints

### GET /events
Get all current in-game events.

**No parameters**

**Returns:** bare JSON array of `TrailEvent` objects (NOT wrapped in `{ items: [...] }`)

**TrailEvent shape:**
```json
{ "eventTag": "#R8U2RCJ", "title": "C.H.A.O.S", "description": "Choose different modifiers during battle..." }
```

| Field | Type | Notes |
|-------|------|-------|
| `eventTag` | string | Unique identifier, e.g. `#R8U2RCJ` |
| `title` | string | Localized event name |
| `description` | string, nullable | Localized event description — null for some events |

**Example response (observed):**
```json
[
  { "eventTag": "#R8U2RCJ", "title": "C.H.A.O.S", "description": "Choose different modifiers during battle..." },
  { "eventTag": "#R8UJQUU", "title": "Classic 1v1", "description": "Play a good old-fashioned Battle..." },
  { "eventTag": "#R8UJR98", "title": "Classic 2v2", "description": "Play a classic game of 2v2!..." },
  { "eventTag": "#R8UJCCJ", "title": "Mega Draft Challenge", "description": "Each win in a Challenge..." },
  { "eventTag": "#R8UJV80", "title": "Classic Challenge", "description": "Each win in a Challenge..." },
  { "eventTag": "#R8UC0LP", "title": "Grand Challenge", "description": "Each win in a Challenge..." },
  { "eventTag": "#R8UURVL", "title": "Merge Tactics", "description": null }
]
```

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. `type`/`detail` were not observed.

---

## Agent Notes
- Returns a **bare array**, not `{ items: [...] }` — this is one of two endpoints that do this (the other is `/players/{tag}/battlelog`)
- `title` and `description` are localized — locale is determined by the API token's configured region
- `description` can be null for some events (observed for "Merge Tactics")
- Returns only currently active events, not upcoming or historical
- Query params appear to be ignored — `/events?limit=5` still returned the full bare array in March 2026 testing
- `eventTag` values appear in battle log entries (`Battle.eventTag`) — can be used to cross-reference which event a battle was played in
- Observed ~7 concurrent active events (Classic 1v1, Classic 2v2, Challenges, special modes)
