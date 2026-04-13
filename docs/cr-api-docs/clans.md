# Clash Royale API – Clans Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header
Tag encoding: `#2ABC` → `%232ABC` in path

---

## Endpoints

### GET /clans/{clanTag}
Get full clan info including member list, scores, description, badge.

**Path:** `clanTag` (required) — URL-encoded clan tag

**Returns:** `Clan` object with fields:

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | e.g. `#J2RGCRVG` |
| `name` | string | |
| `description` | string | Clan description text |
| `type` | string | `open`, `inviteOnly`, or `closed` |
| `badgeId` | integer | Badge identifier (no `badgeUrls` in response) |
| `clanScore` | integer | Combined trophy score |
| `clanWarTrophies` | integer | War trophy count |
| `requiredTrophies` | integer | Min trophies to join |
| `donationsPerWeek` | integer | |
| `clanChestStatus` | string | e.g. `inactive` (legacy field) |
| `clanChestLevel` | integer | Legacy field |
| `clanChestMaxLevel` | integer | Legacy field |
| `members` | integer | Member count |
| `memberList` | array | Full member list (see ClanMember below) |
| `location` | Location | `{ id, name, isCountry, countryCode }` |

**ClanMember shape:**
```json
{
  "tag": "#UL2V9QRG0",
  "name": "raquaza",
  "role": "coLeader",
  "lastSeen": "20260309T125029.000Z",
  "expLevel": 66,
  "trophies": 12312,
  "arena": { "id": 54000141, "name": "Magic Academy", "rawName": "Arena_L15" },
  "clanRank": 1,
  "previousClanRank": 1,
  "donations": 30,
  "donationsReceived": 0,
  "clanChestPoints": 0
}
```

- `role` values: `member`, `elder`, `coLeader`, `leader`
- `lastSeen` uses the same `YYYYMMDDTHHmmss.sssZ` format as battlelog
- `arena` is an Arena object with `id`, `name`, `rawName`

---

### GET /clans/{clanTag}/members
List clan members (paginated).

**Path:** `clanTag` (required)
**Query:** `limit`, `after`, `before` (pagination cursors — mutually exclusive)

**Returns:** `ClanMemberList` — `{ items: [...], paging: { cursors: { after?, before? } } }`

Same ClanMember shape as above.

---

### GET /clans/{clanTag}/currentriverrace
Get the clan's active river race state.

**Path:** `clanTag` (required)

**Returns:** `CurrentRiverRace` with fields:

| Field | Type | Notes |
|-------|------|-------|
| `state` | string | e.g. `full` |
| `sectionIndex` | integer | Current section (week) |
| `periodIndex` | integer | Current period within section |
| `periodType` | string | Observed: `training`, `trainingDay`, `warDay`, `battleDay`, `colosseum` (see notes) |
| `clan` | RiverRaceClan | This clan's data (see below) |
| `clans` | array | All 5 clans in the race |
| `periodLogs` | array | Historical period data for current race |

Note: `collectionEndTime` and `warEndTime` may not always be present (not observed in testing — may only appear during active war periods).

**RiverRaceClan shape:**
```json
{
  "tag": "#J2RGCRVG",
  "name": "POAP KINGS",
  "badgeId": 16000146,
  "fame": 0,
  "repairPoints": 0,
  "participants": [ /* RiverRaceParticipant array */ ],
  "periodPoints": 0,
  "clanScore": 160
}
```

Observed in live payloads after a clan finishes:
- `clan.finishTime` can appear on `GET /clans/{clanTag}/currentriverrace` and is usable as a live "race already finished" signal
- the sentinel value `19691231T235959.000Z` should not be treated as a usable live completion timestamp
- `trophyChange` is **not** present in the live `currentriverrace` payload; it appears in `/riverracelog` standings

**RiverRaceParticipant shape:**
```json
{ "tag": "#RCCY80VG2", "name": "Ram", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0 }
```

**PeriodLog shape:**
```json
{
  "periodIndex": 3,
  "items": [
    {
      "clan": { "tag": "#J2RGCRVG" },
      "pointsEarned": 3800,
      "progressStartOfDay": 0,
      "progressEndOfDay": 3311,
      "endOfDayRank": 0,
      "progressEarned": 3000,
      "numOfDefensesRemaining": 7,
      "progressEarnedFromDefenses": 311
    }
  ]
}
```

---

### GET /clans/{clanTag}/riverracelog
Historical river race results (paginated).

**Path:** `clanTag` (required)
**Query:** `limit`, `after`, `before` (pagination cursors — mutually exclusive)

**Returns:** `{ items: [...], paging: { cursors: { ... } } }`

**RiverRaceLogEntry shape:**
```json
{
  "seasonId": 130,
  "sectionIndex": 0,
  "createdDate": "20260309T095606.000Z",
  "standings": [
    {
      "rank": 1,
      "trophyChange": 20,
      "clan": {
        "tag": "#J2RGCRVG",
        "name": "POAP KINGS",
        "badgeId": 16000146,
        "fame": 10000,
        "repairPoints": 0,
        "finishTime": "20260309T095604.000Z",
        "participants": [ /* RiverRaceParticipant array */ ]
      }
    }
  ]
}
```

- `seasonId` is a sequential integer (e.g. 127, 128, 129, 130) — NOT the YYYY-MM format used for league seasons
- `sectionIndex` = week within the season. Most seasons are 4 weeks (sections 0-3) but some are 5 weeks (sections 0-4). Supercell varies the war season length to keep it roughly aligned with Pass Royale seasons.
- `standings` contains all 5 clans ranked by finish position
- `trophyChange`: regular weeks = ±20, final week (colosseum) = ±100. Colosseum is always the last section of a season — section 3 in a 4-week season, section 4 in a 5-week season.
- `finishTime`: normal value for clans that finished; sentinel value `19691231T235959.000Z` (epoch 0) for the final week/colosseum
- Elixir should prefer `finishTime` over fame thresholds when telling players the race is finished

---

### ~~GET /clans/{clanTag}/currentwar~~ REMOVED
~~Get current classic clan war status.~~

**Status: Permanently removed.** Returns:
```json
{"reason":"gone","message":"This API endpoint has been permanently removed."}
```

---

### ~~GET /clans/{clanTag}/warlog~~ DISABLED
~~Historical classic clan war log.~~

**Status: Temporarily disabled.** Returns:
```json
{"reason":"notFound","message":"This API endpoint has been temporarily disabled, possibilities to bring it back are being investigated."}
```

---

### GET /clans
Search clans by filters. At least one filter required. If using `name`, it must be 3+ chars.

**Query:**
- `name` — wildcard match anywhere in name (case-insensitive). **Not required** — can search by other filters alone.
- `locationId` — filter by location (e.g. `57000249` for US)
- `minMembers`, `maxMembers` — member count range
- `minScore` — minimum clan score
- `limit`, `after`, `before`

**Returns:** `{ items: [...], paging: { cursors: { ... } } }`

Clan search results include a subset of clan fields:
```json
{
  "tag": "#J2RGCRVG",
  "name": "POAP KINGS",
  "type": "open",
  "badgeId": 16000146,
  "clanScore": 67536,
  "clanWarTrophies": 160,
  "location": { "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" },
  "requiredTrophies": 2000,
  "donationsPerWeek": 278,
  "clanChestLevel": 1,
  "clanChestMaxLevel": 0,
  "members": 22
}
```

Note: search results do not include `memberList` or `description` — fetch the full clan by tag for those.

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Clan not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. `type`/`detail` were not observed.

---

## Agent Notes
- **Classic war endpoints are dead:** `currentwar` is permanently removed; `warlog` is disabled. Only river race endpoints are functional.
- Pagination: use `paging.cursors` from response to get `after`/`before` values. Empty `cursors: {}` means no more pages.
- `clanChestStatus`, `clanChestLevel`, `clanChestMaxLevel` are legacy fields — clan chests no longer exist in-game
- The full clan response (`/clans/{tag}`) includes `memberList` with all members; the `/members` endpoint offers the same data with pagination
- `badgeUrls` is **not** present in responses — only `badgeId` (integer). Badge images must be resolved via the badge ID.
- **Clan search:** `name` is not required — you can filter by `locationId`, `minScore`, `minMembers`, `maxMembers` alone. Search is case-insensitive.
- **River race structure:** A season is mostly 4 weeks but sometimes 5 weeks (Supercell adjusts to align with Pass Royale seasons). Each section has multiple periods (days). The final section is always colosseum week with higher trophy stakes (±100 vs ±20). Do not hardcode which `sectionIndex` is colosseum — use `trophyChange` from the log or `periodType` from `currentriverrace` to identify it.
- **`periodType` lifecycle:** Practice days emit `training` (also `trainingDay` in older payloads). Battle days emit `warDay` / `battleDay` on regular weeks and `colosseum` on the final week. The colosseum value only appears once battle days begin — practice days of colosseum week still report `training`, so a "this is colosseum week" determination cannot be made from `periodType` alone during practice days.
- **Colosseum-week behavior:** The colosseum week has no boat battles and no boat defenses — only Colosseum duels and 1v1s. Participant fields like `boatAttacks` and `numOfDefensesRemaining` will not advance, and battlelog `boatBattle` entries do not occur during colosseum week. Avoid surfacing boat-defense or repair-point copy when `periodType=colosseum`.
- **River race default limit:** `/riverracelog` returns 10 entries by default.
- **Member roles observed:** `member`, `elder`, `coLeader`, `leader`
- **Participant counts:** River race participants can exceed the current member count (includes players who left the clan during the race)
