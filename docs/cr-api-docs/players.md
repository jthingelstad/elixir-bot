# Clash Royale API – Players Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header
Tag encoding: `#2ABC` → `%232ABC` in path

---

## Endpoints

### GET /players/{playerTag}
Get full player profile.

**Path:** `playerTag` (required) — URL-encoded player tag

**Returns:** `Player` object with fields:

| Field | Type | Notes |
|-------|------|-------|
| `tag` | string | e.g. `#PU9RCVYUG` |
| `name` | string | |
| `expLevel` | integer | Player's King Level |
| `expPoints` | integer | XP within current level |
| `totalExpPoints` | integer | Lifetime XP earned |
| `starPoints` | integer | Star points for card cosmetics |
| `trophies` | integer | Current trophy count |
| `bestTrophies` | integer | All-time best trophies |
| `arena` | Arena | `{ id, name, rawName }` |
| `role` | string | Clan role: `member`, `elder`, `coLeader`, `leader` |
| `wins` | integer | Total wins |
| `losses` | integer | Total losses |
| `battleCount` | integer | Total battles played |
| `threeCrownWins` | integer | |
| `donations` | integer | Current-season donations |
| `donationsReceived` | integer | Current-season donations received |
| `totalDonations` | integer | Lifetime donations |
| `challengeCardsWon` | integer | |
| `challengeMaxWins` | integer | Best challenge run |
| `tournamentCardsWon` | integer | |
| `tournamentBattleCount` | integer | |
| `warDayWins` | integer | |
| `clanCardsCollected` | integer | |
| `clan` | PlayerClan | `{ tag, name, badgeId }` — **absent** if not in a clan |
| `leagueStatistics` | object | See below — **absent** for some players (not all players have this) |
| `currentDeck` | array | 8 cards — each is a PlayerItemLevel (see below) |
| `currentDeckSupportCards` | array | Tower Troops in current deck |
| `cards` | array | Full card collection with levels |
| `supportCards` | array | Tower Troops collection with levels |
| `currentFavouriteCard` | Item | Full card object for favourite card |
| `badges` | array | See below |
| `achievements` | array | See below |
| `currentPathOfLegendSeasonResult` | object | `{ leagueNumber, trophies, rank }` — `rank` can be null |
| `lastPathOfLegendSeasonResult` | object | Same shape |
| `bestPathOfLegendSeasonResult` | object | Same shape |
| `legacyTrophyRoadHighScore` | integer | Pre-rework trophy high |
| `progress` | object | Merge Tactics / side-mode progress — see below |

**leagueStatistics shape:**
```json
{
  "currentSeason": { "trophies": 12530, "bestTrophies": 6650 },
  "previousSeason": { "id": "2026-02", "rank": 3288, "trophies": 7163, "bestTrophies": 7250 },
  "bestSeason": { "id": "2021-02", "rank": 926, "trophies": 7506 }
}
```
- `currentSeason` has no `id` or `rank`
- `previousSeason` and `bestSeason` include `id` (YYYY-MM format) and optional `rank`

**badge shape:**
```json
{ "name": "Classic12Wins", "level": 1, "maxLevel": 8, "progress": 2, "target": 10, "iconUrls": { "large": "..." } }
```

**achievement shape:**
```json
{ "name": "Team Player", "stars": 3, "value": 1717, "target": 1, "info": "Join a Clan", "completionInfo": null }
```

**Player card (in `cards` / `currentDeck`) vs catalog card:**
Player cards include additional fields beyond the catalog:
- `level` (integer) — current card level
- `starLevel` (integer, optional) — cosmetic star level
- `evolutionLevel` (integer, optional) — observed in both `currentDeck` and `cards` in live March 2026 payloads
- `count` (integer) — cards owned (0 for maxed / equipped cards)

**Observed interpretation for Elixir UX:**
- `starLevel` and `evolutionLevel` are distinct fields
- `maxEvolutionLevel=1` has only been observed on Evo-capable cards
- `maxEvolutionLevel=2` has only been observed on Hero-capable cards
- `maxEvolutionLevel=3` has only been observed on cards that support both Evo and Hero modes
- `evolutionLevel=1` maps cleanly to `Evo unlocked` in stored player data
- `evolutionLevel=2` maps cleanly to `Hero unlocked` in stored player data
- `evolutionLevel=3` maps cleanly to `Evo + Hero unlocked` in stored player data
- Missing `evolutionLevel` appears to mean no mode unlocked yet
- This is an observed interpretation from live payloads plus local stored data; it does not prove slot-based activation behavior in decks
- Player-facing output should prefer `Evo`, `Hero`, and `Evo + Hero` over raw `evolutionLevel` numbers

**progress shape:**
```json
{
  "": { "arena": { "id": 168000059, "name": "Diamond", "rawName": "AutoChessArena10_2025_Oct" }, "trophies": 4257, "bestTrophies": 4337 },
  "AutoChess_2026_Mar": { "arena": { ... }, "trophies": 3460, "bestTrophies": 3593 }
}
```
Keys are opaque mode-season identifiers. The empty string key `""` is a legacy/default bucket. Clients should not hardcode specific key names beyond treating them as labels.

---

### GET /players/{playerTag}/battlelog
Get recent battle history.

**Path:** `playerTag` (required)

**Returns:** bare JSON array of `Battle` objects (not paginated, not wrapped in `{ items: [...] }`)

Observed: returns up to ~48 battles.

**Battle object fields:**

| Field | Type | Notes |
|-------|------|-------|
| `type` | string | See battle types below |
| `battleTime` | string | Format: `20260309T135844.000Z` |
| `isLadderTournament` | boolean | |
| `tournamentTag` | string | Optional — present on `type=tournament` battles; links to the tournament via `/tournaments/{tag}` |
| `eventTag` | string | Optional — links to event from `/events` |
| `arena` | Arena | `{ id, name, rawName }` |
| `gameMode` | object | `{ id, name }` — see game modes below |
| `deckSelection` | string | See deck selections below |
| `team` | array | Array of PlayerBattleData (1 entry for 1v1, 2 for 2v2) |
| `opponent` | array | Same structure |
| `modifiers` | array | Optional — CHAOS mode modifiers, see below |
| `isHostedMatch` | boolean | |
| `leagueNumber` | integer | Path of Legend league number |
| `boatBattleSide` | string | Optional — `defender` or `attacker` (boat battles only) |
| `boatBattleWon` | boolean | Optional — boat battles only |
| `newTowersDestroyed` | integer | Optional — boat battles only |
| `prevTowersDestroyed` | integer | Optional — boat battles only |
| `remainingTowers` | integer | Optional — boat battles only |

**Battle types observed:**

| `type` | Description | Game Modes |
|--------|-------------|------------|
| `PvP` | Ladder / trophy battles | `Ladder` |
| `pathOfLegend` | Ranked Path of Legend | `Ranked1v1_NewArena2` |
| `trail` | Event/challenge battles | `Crazy_Arena`, `Challenge_AllCards_EventDeck_NoSet` |
| `clanMate` | Friendly battle within clan (1v1) | `Friendly` |
| `clanMate2v2` | 2v2 with clanmate | `TeamVsTeam_Touchdown_Draft` |
| `friendly` | Friendly battle (not clanmate) | `Crazy_Arena`, `7xElixir_Friendly` |
| `riverRacePvP` | River race 1v1 battle | `CW_Battle_1v1` |
| `riverRaceDuel` | River race duel (best-of-3) | `CW_Duel_1v1` |
| `riverRaceDuelColosseum` | Colosseum duel variant | `CW_Duel_1v1` |
| `tournament` | Player-created tournament battle | `Tournament` (72000009, bring-your-own-deck), `Draft_Competitive` (72000194, Triple Draft) |
| `boatBattle` | River race boat attack/defense | `ClanWar_BoatBattle` |
| `unknown` | Rare fallback value seen on some friendlies | `Friendly` |

**Deck selection values:**

| `deckSelection` | Used in |
|-----------------|---------|
| `collection` | PvP, pathOfLegend, riverRacePvP, clanMate, friendly |
| `eventDeck` | trail, some friendlies |
| `draft` | clanMate2v2 (draft modes) |
| `warDeckPick` | riverRaceDuel |
| `pick` | pick-mode friendlies |
| `draftCompetitive` | competitive draft friendlies, Triple Draft tournaments |
| `predefined` | preset-deck friendlies (e.g. Mirror Deck) |

**Known game mode IDs:**

| ID | Name |
|----|------|
| 72000006 | Ladder |
| 72000007 | Friendly |
| 72000005 | DraftMode |
| 72000009 | (tournament mode) |
| 72000013 | (tournament mode) |
| 72000014 | TeamVsTeam |
| 72000032 | TripleElixir_Friendly |
| 72000042 | PickMode |
| 72000050 | Touchdown_Draft |
| 72000051 | TeamVsTeam_Touchdown_Draft |
| 72000060 | Overtime_Ladder |
| 72000194 | Draft_Competitive |
| 72000232 | 7xElixir_Friendly |
| 72000254 | MirrorDeck_Friendly |
| 72000266 | ClanWar_BoatBattle |
| 72000267 | CW_Duel_1v1 |
| 72000268 | CW_Battle_1v1 |
| 72000464 | Ranked1v1_NewArena2 |
| 72000469 | DraftMode_Princess |
| 72000474 | Challenge_AllCards_EventDeck_NoSet |
| 72000486 | Touchdown_Event |
| 72000500 | RampUp_Friendly_EventDeck_4Card |
| 72000502 | Crazy_Arena |

Note: `gameMode.name` may be absent — tournament game modes often only have `id`.

**Determining battle winner:** There is no explicit `winner` field. Use this order:
1. If `boatBattleWon` exists, use it.
2. Else if `team[0].trophyChange` exists, positive = win, negative = loss, zero = unresolved/draw.
3. Else if both sides have crowns, compare `team[0].crowns` vs `opponent[0].crowns`.
4. Else treat the outcome as unresolved.

For 2v2 battles, the outcome is still determined from the first team entry because both teammates share the same result.

**PlayerBattleData shape:**
```json
{
  "tag": "#PU9RCVYUG",
  "name": "FJ21",
  "crowns": 3,
  "kingTowerHitPoints": 9201,
  "princessTowersHitPoints": [6104, 6104],
  "clan": { "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 },
  "cards": [ /* 8 card objects */ ],
  "supportCards": [ /* Tower Troop cards, may be empty array */ ],
  "elixirLeaked": 3.33,
  "globalRank": null,
  "startingTrophies": 12286,
  "trophyChange": 26
}
```

**Conditional PlayerBattleData fields:**
- `startingTrophies` — present on PvP, pathOfLegend, riverRacePvP, riverRaceDuel, friendly, clanMate
- `trophyChange` — only on PvP and pathOfLegend (positive=win, negative=loss)
- `globalRank` — present on all battles, null unless player is in top global rankings (then integer)
- `elixirLeaked` — float, present on all battles
- `supportCards` — array (may be empty `[]`)
- `rounds` — array, only on riverRaceDuel (best-of-3 duel rounds)
- `clan` — absent if player has no clan

**Duel rounds (riverRaceDuel):**
Both `team[0]` and `opponent[0]` have a `rounds` array (typically 2-3 rounds):
```json
{
  "crowns": 3,
  "kingTowerHitPoints": 7032,
  "princessTowersHitPoints": [4424, 3959],
  "elixirLeaked": 2.1,
  "cards": [ /* 8 cards, each has an additional 'used': true/false field */ ]
}
```
The `used` boolean on each card in a round indicates if that card was played. Each round has a different deck (3 decks total for duels).

**CHAOS mode modifiers (type=trail with Crazy_Arena):**
```json
[
  { "tag": "#PU9RCVYUG", "modifiers": ["Pekka3", "Graveyard2", "Rage1"] },
  { "tag": "#2JVGV9CG9", "modifiers": ["Fireball3", "GoblinHut2", "Berserker1"] }
]
```
Each entry maps a player tag to their chosen modifiers. Only present in CHAOS mode battles.

---

### GET /players/{playerTag}/upcomingchests
Get the player's upcoming chest sequence.

**Path:** `playerTag` (required)

**Returns:** `UpcomingChests` — `{ items: [...] }`

**Chest shape:**
```json
{ "index": 0, "name": "Gold Crate" }
```

- `index` — position in upcoming sequence (0 = next chest)
- `name` — chest type name (e.g. `Golden Chest`, `Magical Chest`, `Mega Lightning Chest`, `Legendary Chest`, `Epic Chest`, `Royal Wild Chest`, `Giant Chest`, `Tower Troop Chest`, `Gold Crate`, `Plentiful Gold Crate`, `Overflowing Gold Crate`)
- Indices are **not contiguous** — only notable/special chests are listed (skips standard Silver/Gold chests in between)

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Player not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. `message` may be absent on some `404` responses, and `type`/`detail` were not observed.

---

## Agent Notes
- **Optional fields:** `clan`, `role`, and `leagueStatistics` are completely absent (not null) when the player has no clan / no league history. Always check for key existence.
- `currentDeck` (8 cards) vs `cards` (full collection): both have been observed with `evolutionLevel`; `cards` also includes `count` of owned copies
- `role` values: `member`, `elder`, `coLeader`, `leader`
- Path of Legend `rank` field is null when the player hasn't achieved a rank yet
- Battlelog returns a bare array (like `/events`), not a paginated response — no `paging` object. Returns up to ~48 battles.
- `progress` is a map of side-mode season results (Merge Tactics / AutoChess) — keys are mode season identifiers. Empty string key `""` = legacy/default season.
- `progress` keys should be treated as opaque identifiers, not a stable enum. Parse the nested values, not the key naming pattern.
- `battleTime` format is `YYYYMMDDTHHmmss.sssZ` — parse carefully, no dashes or colons
- `leagueStatistics.currentSeason` has no `id` field (it's the current season)
- **2v2 battles:** `team` and `opponent` each contain 2 entries instead of 1
- **Battle winner detection:** Apply the explicit precedence above: `boatBattleWon` -> `trophyChange` -> crowns -> unresolved
- Additional battle variants observed in March 2026 sampling: `riverRaceDuelColosseum` and an occasional `unknown` type on friendlies
- For player-facing text, avoid raw `Evolution Level N` wording; Elixir should translate to `Evo`, `Hero`, or `Evo + Hero`
- Additional `deckSelection` values observed in March 2026 sampling: `pick`, `draftCompetitive`, `predefined`
- **Tournament battles:** `type=tournament` battles include a `tournamentTag` field that links back to `/tournaments/{tag}`. This allows matching battles to specific tournaments. The `gameMode` distinguishes tournament format: `72000009`/`Tournament` for bring-your-own-deck, `72000194`/`Draft_Competitive` for Triple Draft. In draft tournaments, each battle has different cards (drafted per match); in standard tournaments, players use their `collection` deck.
- **Tournament battle dedup:** Both players in a match see the same `battleTime`. Dedup key: `battleTime` + sorted pair of `(team[0].tag, opponent[0].tag)`. For tournament winner detection, use crowns comparison (no `trophyChange` field on tournament battles). The `startingTrophies` field on tournament battles reflects tournament score, not ladder trophies.
- **Tournament battle log retention:** Battle logs are not permanent (~48 battles). Tournament battles will rotate out as players play more games. To capture tournament battle data reliably, poll player battle logs shortly after the tournament ends. Battles from a 13-player tournament were partially lost within ~24h due to active players' logs rotating.
- **Badges:** Two categories — progress badges (with `level`/`maxLevel`/`progress`/`target`) and one-time badges (level=null, just `progress` and `iconUrls`). Mastery badges are per-card (e.g. `MasteryKnight`).
- **Achievements:** Fixed set of 12 achievements. `stars` (0-3) indicates completion tier. `completionInfo` is typically null.
