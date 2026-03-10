# Clash Royale API – Global Tournaments Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header

---

## Endpoints

### GET /globaltournaments
Get list of global tournaments.

**No parameters**

**Returns:** `{ items: [...] }` — array of `LadderTournament` objects

When no global tournaments are active, returns:
```json
{ "items": [] }
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
- Returns `LadderTournament` type — distinct from the player-created `Tournament` type returned by `/tournaments/{tag}`
- To get player rankings for a global tournament, use `tournamentTag` from results with `/locations/global/rankings/tournaments/{tournamentTag}` (see Locations reference)
- Returns empty `items` array when no global tournaments are active (not a 404)
- Global tournaments are Supercell-run events that appear on a schedule — they may not always be active
