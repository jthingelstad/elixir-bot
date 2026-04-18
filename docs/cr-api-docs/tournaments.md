# Clash Royale API – Tournaments Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header
Tag encoding: `#2ABC` → `%232ABC` in path

---

## Endpoints

### GET /tournaments
Search tournaments by name.

**Query:**
- `name` — wildcard match. At least one filtering parameter is required; in live March 2026 tests, two-character values like `ab` still returned results.
- `limit`, `after`, `before` (pagination cursors — mutually exclusive)

**Returns:** `{ items: [...], paging: { ... } }` — array of `TournamentHeader` objects (summary data, not full detail)

**TournamentHeader shape:**
```json
{
  "tag": "#2GP0RGGU",
  "type": "open",
  "status": "inProgress",
  "creatorTag": "#822GUJ92L",
  "name": "a a a clash elite a a b 1",
  "levelCap": 11,
  "firstPlaceCardPrize": 0,
  "capacity": 74,
  "maxCapacity": 1000,
  "preparationDuration": 3600,
  "duration": 14400,
  "createdTime": "20260309T222248.000Z",
  "gameMode": { "id": 72000013 }
}
```

Note: `TournamentHeader` does NOT include `membersList`, `description`, `startedTime`, or `endedTime`. Fetch by tag for full detail.

---

### GET /tournaments/{tournamentTag}
Get full tournament details.

**Path:** `tournamentTag` (required) — URL-encoded tournament tag

**Returns:** `Tournament` object with all fields:

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | |
| `name` | string | |
| `description` | string | Optional — not always present |
| `type` | string | `open`, `passwordProtected` |
| `status` | string | `inPreparation`, `inProgress` (see note) |
| `creatorTag` | string | Tag of the player who created it |
| `capacity` | integer | Current number of participants |
| `maxCapacity` | integer | Maximum allowed participants |
| `levelCap` | integer | Max card level allowed (observed: always 11) |
| `firstPlaceCardPrize` | integer | Card prize for 1st place (observed: 0) |
| `preparationDuration` | integer | Seconds before tournament starts |
| `duration` | integer | Tournament duration in seconds |
| `createdTime` | string | When tournament was created |
| `startedTime` | string | Optional — when tournament started (absent during `inPreparation`) |
| `endedTime` | string | Optional — when tournament ended (absent if not yet ended) |
| `gameMode` | GameMode | `{ id }` — note: `name` may be absent in tournament context |
| `membersList` | array | TournamentMember objects (see below) |

**TournamentMember shape:**
```json
{
  "tag": "#2RG0GRJ0U",
  "name": "ziikadaBalada",
  "score": 8,
  "rank": 2,
  "clan": { "tag": "#GC02QYJ", "name": "MaLibu JJ", "badgeId": 16000163 }
}
```

- `clan` is optional — absent if the player has no clan
- `score` = wins in the tournament
- `rank` = position on leaderboard

**Status enum (observed):**
- `inPreparation` — tournament created, waiting for start
- `inProgress` — tournament is active
- `ended` — tournament has finished (confirmed April 2026 via direct tag fetch; `startedTime` and `endedTime` are both present)

Note: Search may only return active tournaments (not ended ones). Tournaments in preparation may become unfindable by tag after they start. Ended tournaments *are* accessible via direct tag fetch.

**Type enum (observed):**
- `open` — anyone can join
- `passwordProtected` — requires password to join (this is what the in-game UI calls a "private" tournament)

Note: No separate `private` type was observed — in-game "private" tournaments map to `passwordProtected` in the API.

---

## Game Modes (player-facing names)

In-game UI exposes 16 tournament game modes. The API returns only
`gameMode.id` (an integer) in tournament context — `name` is typically
absent in the `Tournament` payload (it *is* present in
`/players/{tag}/battlelog` battle entries, but as an internal CR code
like `CW_Duel_1v1`, not the player-facing name below).

| Player-facing name | API id (when known) | Notes |
|---|---|---|
| Normal Battle | — | |
| Double Elixir Battle | — | |
| Triple Elixir Battle | — | |
| Sudden Death Battle | — | |
| Draft Battle | — | |
| Double Elixir Draft | — | |
| Triple Draft | 72000194 | Confirmed April 2026 — POAP KINGS Clan Tourney |
| Heist Draft | — | |
| Hog Race | — | |
| Lumberjack Rush | — | |
| Wall Breaker Party | — | |
| Ghost Parade | — | |
| Elixir Capture | — | |
| Dragon Hunt | — | |
| Duel | — | |
| Mega Draft Challenge | — | |

Mapping is empirical: observed ids carry over but the API does not
publish a stable public mapping. Build the table opportunistically as we
host or observe each mode. The `tournaments.deckSelection` field
sometimes hints at format — `draftCompetitive` corresponds to "Triple
Draft" in the in-game UI; `collection` is "Bring Your Own Deck"; plain
`draft` is "Draft" — but `deckSelection` does not distinguish between
the elixir-rate variants (Normal vs. Double vs. Triple Elixir Battle).

For Elixir-side mapping, the lookup belongs at the signal-emission
boundary (next to `deck_selection_label` in `storage/tournament.py`) so
all signals carry the player-facing name regardless of whether the LLM
ever sees the raw id.

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Tournament not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. `GET /tournaments` with no filters returns `400 badRequest` with `At least one filtering parameter must exist`. `type`/`detail` were not observed.

---

## Agent Notes
- Search returns `TournamentHeader` (summary) — fetch by tag to get `membersList` and full detail
- `GET /tournaments` requires at least one filter, but the older "3+ chars required" assumption for `name` was not reproduced in March 2026 testing
- `description` field is optional and may not be present on tournaments without one
- `gameMode` in tournament context typically only has `id` (no `name`) — unlike battle log where both are present
- Search only seems to return active tournaments (`inPreparation` or `inProgress`) — not ended ones. However, ended tournaments are accessible via direct tag fetch.
- Members can join during both `inPreparation` and `inProgress` phases
- No ordering guarantee on search results
- Treat search as discovery, not archival lookup. If search returns a tag you care about, fetch `/tournaments/{tag}` immediately.
- Do not assume search is complete, stable, or ordered by recency
- `levelCap` sets the max card level allowed — all observed tournaments had `levelCap: 11`
- `firstPlaceCardPrize` was always 0 in observations — may be a legacy field
- `preparationDuration` and `duration` are in seconds (e.g. 3600 = 1 hour, 14400 = 4 hours)
- `gameMode.id` values observed: 72000013, 72000194 — meaning varies; `name` field is absent in tournament context
- For ended tournaments, `startedTime` and `endedTime` are both reliably present alongside full `membersList` with final scores/ranks
