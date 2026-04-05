# Clash Royale API – Model Reference

Field shapes verified against live API responses (March–April 2026).

---

## Common Objects

### Arena
```json
{ "id": 54000142, "name": "Ultimate Clash Pit", "rawName": "Arena_L16" }
```
Used in: Player, ClanMember, Battle. Arena IDs observed in range 54000xxx.

### Location
```json
{ "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" }
```
`countryCode` absent for regions (`isCountry: false`). 262 total locations (8 regions + 254 countries). IDs in range 57000xxx.

### PlayerClan
```json
{ "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 }
```
Used in: Player, PlayerBattleData, TournamentMember, ranking entries. **Absent** (not null) when player has no clan.

### GameMode
```json
{ "id": 72000006, "name": "Ladder" }
```
In tournament contexts, `name` may be absent (only `id`). This list is non-exhaustive; see `players.md` for the fuller observed game-mode table. Known IDs:
- `72000006` = Ladder, `72000007` = Friendly, `72000051` = TeamVsTeam_Touchdown_Draft
- `72000232` = 7xElixir_Friendly, `72000266` = ClanWar_BoatBattle
- `72000267` = CW_Duel_1v1, `72000268` = CW_Battle_1v1
- `72000464` = Ranked1v1_NewArena2, `72000502` = Crazy_Arena

---

## Players

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `Player` | `GET /players/{playerTag}` | tag, name, expLevel, expPoints, totalExpPoints, starPoints, trophies, bestTrophies, arena, role?, wins, losses, battleCount, threeCrownWins, donations, donationsReceived, totalDonations, challengeCardsWon, challengeMaxWins, tournamentCardsWon, tournamentBattleCount, warDayWins, clanCardsCollected, clan?, leagueStatistics?, currentDeck, currentDeckSupportCards, cards, supportCards, currentFavouriteCard, badges, achievements, currentPathOfLegendSeasonResult, lastPathOfLegendSeasonResult, bestPathOfLegendSeasonResult, legacyTrophyRoadHighScore, progress |

**Optional Player fields** (absent when not applicable):
- `clan`, `role` — absent if player is not in a clan
- `leagueStatistics` — absent for some players (not all have league history)

**Nullable Player fields** (always present, null when not applicable):
- `currentPathOfLegendSeasonResult`, `lastPathOfLegendSeasonResult`, `bestPathOfLegendSeasonResult` — null for players without Path of Legend history
- `legacyTrophyRoadHighScore` — null for players without pre-rework trophy history

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `PlayerLeagueStatistics` | nested in Player | currentSeason: {trophies, bestTrophies}, previousSeason: {id, rank, trophies, bestTrophies}, bestSeason: {id, rank, trophies} |
| `PathOfLegendSeasonResult` | nested in Player | leagueNumber (int), trophies (int), rank (int, nullable) |
| `PlayerItemLevel` | currentDeck, cards, supportCards | name, id, level, starLevel?, evolutionLevel?, maxLevel, maxEvolutionLevel?, rarity, count, elixirCost?, iconUrls |
| `Item` | currentFavouriteCard, card catalog | name, id, maxLevel, maxEvolutionLevel?, elixirCost?, iconUrls, rarity |

**Observed mode-field interpretation:**
- `starLevel` is separate from `evolutionLevel`
- `evolutionLevel` has been observed in both `currentDeck` and `cards`
- `maxEvolutionLevel=1` aligns with Evo-capable cards
- `maxEvolutionLevel=2` aligns with Hero-capable cards
- `maxEvolutionLevel=3` aligns with cards that support both Evo and Hero modes
- `evolutionLevel=1` maps to `Evo unlocked`
- `evolutionLevel=2` maps to `Hero unlocked`
- `evolutionLevel=3` maps to `Evo + Hero unlocked`
- This is an observed interpretation from live payloads and local stored data, suitable for Elixir UX but not proof of slot-based activation behavior

**Card level interpretation:**
- `level` and `maxLevel` use the API's rarity-relative scale, not a universal cross-rarity scale
- `common`: API `1-16` = normalized `1-16`
- `rare`: API `1-14` = normalized `3-16`
- `epic`: API `1-11` = normalized `6-16`
- `legendary`: API `1-8` = normalized `9-16`
- `champion`: API `1-6` = normalized `11-16`
- Practical implication: a champion at API `level: 1` is already at normalized level 11, and all rarities cap out at normalized level 16

### Badges
Two categories:

**Progress badges** (leveled):
```json
{ "name": "Grand12Wins", "level": 5, "maxLevel": 8, "progress": 150, "target": 250, "iconUrls": { "large": "..." } }
```

**One-time badges** (no levels):
```json
{ "name": "Crl20Wins2021", "progress": 20, "iconUrls": { "large": "..." } }
```
`level`, `maxLevel`, and `target` are null for one-time badges.

**Badge name categories:**
- Mastery badges: per-card, e.g. `MasteryKnight`, `MasteryArrows` (121 possible, maxLevel=10)
- Challenge badges: `Classic12Wins`, `Grand12Wins` (maxLevel=8)
- Mode badges: `2v2`, `RampUp`, `SuddenDeath`, `Draft`, `2xElixir`
- Collection badges: `EmoteCollection`, `BannerCollection`, `CollectionLevel`, `ClanDonations`
- Seasonal badges: `SeasonalBadge_202507_v2`, `MergeTacticsBadge_202506`, etc.
- Event badges: `Crl20Wins2021`, `CrlSpectator2022`, `EasterEgg`, etc.
- Career badges: `YearsPlayed`, `BattleWins`, `ClanWarsVeteran`, `LadderTop1000`

### Achievements
Fixed set of 12 achievements:
```json
{ "name": "Team Player", "stars": 3, "value": 1717, "target": 1, "info": "Join a Clan", "completionInfo": null }
```
- `stars` (0-3): completion tier
- `value`: current progress
- `target`: threshold for completion
- `completionInfo`: typically null

Known achievements: Team Player, Friend in Need, Road to Glory, Gatherer, TV Royale, Tournament Rewards, Tournament Host, Tournament Player, Challenge Streak, Practice with Friends, Special Challenge, Friend in Need II

### Battle (from battlelog)

```json
{
  "type": "PvP",
  "battleTime": "20260309T025623.000Z",
  "isLadderTournament": false,
  "arena": { "id": 54000141, "name": "Magic Academy", "rawName": "Arena_L15" },
  "gameMode": { "id": 72000006, "name": "Ladder" },
  "deckSelection": "collection",
  "team": [ /* PlayerBattleData */ ],
  "opponent": [ /* PlayerBattleData */ ],
  "isHostedMatch": false,
  "leagueNumber": 1
}
```

**Battle types:** `PvP`, `pathOfLegend`, `trail`, `clanMate`, `clanMate2v2`, `friendly`, `riverRacePvP`, `riverRaceDuel`, `riverRaceDuelColosseum`, `boatBattle`, `unknown`

**Conditional battle fields:**
- `eventTag` — present on trail/event battles, links to `/events`
- `modifiers` — CHAOS mode only (trail with Crazy_Arena)
- `boatBattleSide` (`defender`/`attacker`), `boatBattleWon`, `newTowersDestroyed`, `prevTowersDestroyed`, `remainingTowers` — boat battles only

**Deck selection values:** `collection`, `eventDeck`, `draft`, `warDeckPick`, `pick`, `draftCompetitive`, `predefined`

### PlayerBattleData
```json
{
  "tag": "#PU9RCVYUG",
  "name": "FJ21",
  "crowns": 3,
  "kingTowerHitPoints": 9201,
  "princessTowersHitPoints": [6104, 6104],
  "clan": { "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 },
  "cards": [ /* 8 card objects */ ],
  "supportCards": [ /* Tower Troops, may be empty [] */ ],
  "elixirLeaked": 3.33,
  "globalRank": null,
  "startingTrophies": 12286,
  "trophyChange": 26
}
```

**Conditional fields:**
- `startingTrophies` — present on most battle types except some boat/trail battles
- `trophyChange` — only on PvP and pathOfLegend (positive=win, negative=loss)
- `globalRank` — integer for globally-ranked players, null otherwise. Present on all battles.
- `rounds` — only on riverRaceDuel (best-of-3)
- `clan` — absent if player has no clan

**2v2 battles:** `team` and `opponent` each have 2 entries.

### Duel Rounds (riverRaceDuel)
```json
{
  "crowns": 3,
  "kingTowerHitPoints": 7032,
  "princessTowersHitPoints": [4424, 3959],
  "elixirLeaked": 2.1,
  "cards": [ /* 8 cards with additional 'used': true/false */ ]
}
```
Typically 2-3 rounds. Each round has a different deck. Cards in rounds include a `used` boolean.

### Chest (from upcomingchests)
```json
{ "index": 0, "name": "Gold Crate" }
```
Indices are non-contiguous — only notable chests shown (skips standard chests between).

Observed chest names: Gold Crate, Plentiful Gold Crate, Overflowing Gold Crate, Golden Chest, Magical Chest, Giant Chest, Epic Chest, Legendary Chest, Mega Lightning Chest, Royal Wild Chest, Tower Troop Chest

### Progress (side modes)
```json
{
  "": { "arena": { "id": 168000059, "name": "Diamond", "rawName": "AutoChessArena10_2025_Oct" }, "trophies": 4257, "bestTrophies": 4337 },
  "AutoChess_2026_Mar": { "arena": { ... }, "trophies": 3460, "bestTrophies": 3593 }
}
```
Map of opaque mode-season IDs to arena/trophy data. Empty string key = legacy/default season. Clients should not rely on the key naming pattern as a stable enum. Arena IDs for side modes use 168000xxx range.

---

## Clans

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `Clan` | `GET /clans/{clanTag}`, `GET /clans` | tag, name, description, type, badgeId, clanScore, clanWarTrophies, requiredTrophies, donationsPerWeek, clanChestStatus, clanChestLevel, clanChestMaxLevel, members, memberList, location |
| `ClanMember` | memberList, `/members` | tag, name, role, lastSeen, expLevel, trophies, arena, clanRank, previousClanRank, donations, donationsReceived, clanChestPoints |

**Clan type values:** `open`, `inviteOnly`, `closed`
**Member role values:** `member`, `elder`, `coLeader`, `leader`

Note: `badgeUrls` is NOT present in responses — only `badgeId` (integer).

**Clan search results** include a subset: tag, name, type, badgeId, clanScore, clanWarTrophies, location, requiredTrophies, donationsPerWeek, clanChestLevel, clanChestMaxLevel, members. No `memberList` or `description`.

### ~~Classic War Models~~ DEPRECATED
| Model | Status |
|-------|--------|
| `CurrentClanWar` | Endpoint permanently removed (410 Gone) |
| `ClanWarClan` / `ClanWarParticipant` | No longer accessible |
| `ClanWarLog` / `ClanWarLogEntry` | Endpoint disabled (404) |
| `ClanWarStanding` | No longer accessible |

---

## River Race

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `CurrentRiverRace` | `GET /clans/{clanTag}/currentriverrace` | state, sectionIndex, periodIndex, periodType, clan, clans (array of 5), periodLogs |
| `RiverRaceClan` | nested in CurrentRiverRace | tag, name, badgeId, fame, repairPoints, participants, periodPoints, clanScore, finishTime (observed live after completion) |
| `RiverRaceParticipant` | nested in RiverRaceClan | tag, name, fame, repairPoints, boatAttacks, decksUsed, decksUsedToday |
| `PeriodLog` | periodLogs array | periodIndex, items (array of PeriodLogEntry) |
| `PeriodLogEntry` | nested in PeriodLog | clan: {tag}, pointsEarned, progressStartOfDay, progressEndOfDay, endOfDayRank, progressEarned, numOfDefensesRemaining, progressEarnedFromDefenses |
| `RiverRaceLogEntry` | `/riverracelog` items | seasonId (sequential int), sectionIndex, createdDate, standings |
| `RiverRaceStanding` | standings array | rank, trophyChange, clan (RiverRaceClan with finishTime) |

**River race state observed:** `full`
**River race periodType observed:** `training`
**Note:** `collectionEndTime` and `warEndTime` were NOT observed in responses — these fields may only appear during active war periods, or may be deprecated.

**Season/section structure:**
- `seasonId` is a sequential integer (e.g. 127, 128, 129, 130)
- Most seasons are 4 weeks (sections 0-3) but some are 5 weeks (sections 0-4). Supercell varies the war season length to stay roughly aligned with Pass Royale seasons. Colosseum is always the last section.
- Do not infer colosseum from `sectionIndex` alone — use `trophyChange` (±100 = colosseum) or `periodType` from currentriverrace
- `trophyChange` is verified on `/riverracelog` standings, not on the live `currentriverrace` payload
- `finishTime` = `19691231T235959.000Z` (epoch 0 sentinel) for colosseum weeks
- observed repo behavior: a non-sentinel live `clan.finishTime` means the race is already finished, even if battle time remains
- Races always have 5 clans

---

## Rankings & Locations

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `Location` | `/locations`, `/locations/{id}` | id, name, isCountry, countryCode? |
| `ClanRanking` | `/rankings/clans`, `/rankings/clanwars` | tag, name, rank, previousRank, location, clanScore, members, badgeId |
| `PlayerPathOfLegendRanking` | `/pathoflegend/players` | tag, name, expLevel, eloRating, rank, clan? |
| `LeagueSeason` | `/seasons` | id (string, "YYYY-MM") |

**ClanRanking notes:**
- `previousRank: -1` means the clan was not previously ranked (new entry)
- `/rankings/clanwars` uses the same ClanRanking shape but `clanScore` reflects war performance

**Global player trophy rankings** may return empty results early in a season. PoL and clan rankings are consistently populated.

---

## Leaderboards

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `Leaderboard` (metadata) | `GET /leaderboards` | id (int), name (string) |
| `Leaderboard` (ranking) | `GET /leaderboard/{id}` | tag, name, rank, score, clan? |

Multiple leaderboards can share the same name (different seasons/variants of the same mode). Up to 10,000 entries returned with no limit specified.

---

## Tournaments

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `TournamentHeader` | `GET /tournaments` (search) | tag, type, status, creatorTag, name, levelCap, firstPlaceCardPrize, capacity, maxCapacity, preparationDuration, duration, createdTime, gameMode |
| `Tournament` | `GET /tournaments/{tag}` | all TournamentHeader fields + membersList, startedTime?, endedTime?, description? |
| `TournamentMember` | nested in Tournament | tag, name, score, rank, clan? |
| `GameMode` | nested in Tournament/Battle | id (always), name (sometimes absent in tournament context) |
| `LadderTournament` / `LadderTournamentList` | `GET /globaltournaments` | (returns empty when no global tournaments active) |

**Tournament type values:** `open`, `passwordProtected`
**Tournament status values:** `inPreparation`, `inProgress`
**Note:** `description`, `startedTime`, `endedTime` are absent (not null) when not applicable.

---

## Challenges

| Model | Status |
|-------|--------|
| `ChallengeChain` / `ChallengeChainsList` | Endpoint returning notFound (March 2026) |
| `Challenge` / `ChallengeList` | Not currently accessible |
| `ChallengeGameMode` | Not currently accessible |
| `SurvivalMilestoneReward` | Not currently accessible |

---

## Cards & Events

| Model | Used By | Verified Fields |
|-------|---------|-----------------|
| `Items` | `GET /cards` | items (121 standard cards), supportItems (4 Tower Troops) |
| `Item` (catalog) | items/supportItems arrays | name, id, maxLevel, maxEvolutionLevel?, elixirCost? (absent on supportItems), rarity, iconUrls |
| `TrailEvent` | `GET /events` (bare array) | eventTag, title, description (nullable) |
| `Emote` / `EmoteList` | no documented endpoint | |

**Card rarity → maxLevel mapping:**

| Rarity | maxLevel | Cards | Evolutions |
|--------|----------|-------|------------|
| common | 16 | 29 items + 1 supportItem | 17/29 have evolutions (levels 1-3) |
| rare | 14 | 30 items | 12/30 have evolutions (levels 1-3) |
| epic | 11 | 33 items + 1 supportItem | 12/33 have evolutions (levels 1-2) |
| legendary | 8 | 21 items + 2 supportItems | 5/21 have evolutions (levels 1-2) |
| champion | 6 | 8 items | 0/8 have evolutions |

**Card ID ranges:**
- `26000xxx` — troops
- `27000xxx` — buildings
- `28000xxx` — spells
- `159000xxx` — Tower Troops (supportItems)

---

## Utility & Primitives

| Model | Notes |
|-------|-------|
| `ClientError` | Usually `{ reason, message? }` — many `404`/`500` responses omit `message`; `type` and `detail` were not observed |
| `Version` | API version metadata |
| `Fingerprint` | Device/session fingerprint |
| `JsonNode` | Generic untyped JSON node — treat as `any` |
| `Match` / `RegisterMatchRequest` / `RegisterMatchResponse` / `CancelMatchResponse` | No public endpoints |
| `VerifyTokenRequest` / `VerifyTokenResponse` | Token verification |

**Known error `reason` values:** `accessDenied`, `notFound`, `gone`, `badRequest`

---

## Agent Notes
- **Optional vs absent:** Many fields are absent (key not present) rather than null when not applicable. Always check for key existence, not just null. Examples: `clan`, `role`, `leagueStatistics` on Player; `clan` on TournamentMember; `startedTime`, `endedTime`, `description` on Tournament. **Exception:** Player fields `currentPathOfLegendSeasonResult`, `lastPathOfLegendSeasonResult`, `bestPathOfLegendSeasonResult`, and `legacyTrophyRoadHighScore` are always present but use `null` — check for both.
- `List` suffix types (e.g. `ClanMemberList`) are always arrays of their singular counterpart
- Classic war models are deprecated — `currentwar` permanently removed, `warlog` disabled
- `challenges` endpoint currently returning notFound
- `seasonsV2` returns null data — use V1 `/seasons` for season IDs
- All datetime strings use format `YYYYMMDDTHHmmss.sssZ` (no dashes or colons)
- `badgeUrls` does not exist in API responses — only `badgeId` (integer)
- Pagination cursors are base64-encoded `{"pos": N}` — empty `cursors: {}` means no more pages
- `leagueNumber` in battles: 1 = default, higher values (e.g. 7) indicate Path of Legend league
