# Player Models

Player-related field shapes verified against live API responses (March-April 2026).

## Player

Used by `GET /players/{playerTag}`.

Verified fields:

| Field                                                                                             | Notes                                           |
| ------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `tag`, `name`                                                                                     | Player identity                                 |
| `expLevel`, `expPoints`, `totalExpPoints`, `starPoints`                                           | Account progression                             |
| `trophies`, `bestTrophies`                                                                        | Trophy Road values                              |
| `arena`                                                                                           | [Arena](common.md#arena)                        |
| `role`                                                                                            | Optional clan role                              |
| `wins`, `losses`, `battleCount`, `threeCrownWins`                                                 | Battle totals                                   |
| `donations`, `donationsReceived`, `totalDonations`                                                | Donation counters                               |
| `challengeCardsWon`, `challengeMaxWins`, `tournamentCardsWon`, `tournamentBattleCount`            | Challenge and tournament counters               |
| `warDayWins`, `clanCardsCollected`                                                                | Legacy war counters                             |
| `currentWinLoseStreak`                                                                            | Optional signed streak counter                  |
| `clan`                                                                                            | Optional [PlayerClan](common.md#playerclan)     |
| `leagueStatistics`                                                                                | Optional `PlayerLeagueStatistics`               |
| `currentDeck`, `cards`, `currentDeckSupportCards`, `supportCards`                                 | Player card arrays                              |
| `currentFavouriteCard`                                                                            | Catalog-like `Item` object                      |
| `badges`, `achievements`                                                                          | Progress and account markers                    |
| `currentPathOfLegendSeasonResult`, `lastPathOfLegendSeasonResult`, `bestPathOfLegendSeasonResult` | Nullable `PathOfLegendSeasonResult`             |
| `legacyTrophyRoadHighScore`                                                                       | Nullable integer                                |
| `progress`                                                                                        | Map of side-mode season IDs to progress objects |

Optional Player fields, absent when not applicable:

- `clan`
- `role`
- `leagueStatistics`
- `currentWinLoseStreak`

Nullable Player fields, always present but null when not applicable:

- `currentPathOfLegendSeasonResult`
- `lastPathOfLegendSeasonResult`
- `bestPathOfLegendSeasonResult`
- `legacyTrophyRoadHighScore`

## PlayerLeagueStatistics

```json
{
  "currentSeason": { "trophies": 12530, "bestTrophies": 6650 },
  "previousSeason": { "id": "2026-02", "rank": 3288, "trophies": 7163, "bestTrophies": 7250 },
  "bestSeason": { "id": "2021-02", "rank": 926, "trophies": 7506 }
}
```

Notes:

- `currentSeason` has no `id` or `rank`.
- `currentSeason.bestTrophies` is optional and can be absent early in a season.
- `previousSeason.bestTrophies` is optional.
- `previousSeason` and `bestSeason` include `id` in `YYYY-MM` format and optional `rank`.

## PathOfLegendSeasonResult

Fields:

- `leagueNumber` - integer
- `trophies` - integer
- `rank` - integer or null

The parent season-result field itself can be null for players without Path of Legend history.

## PlayerItemLevel

Used by `currentDeck`, `cards`, `currentDeckSupportCards`, and `supportCards`.

Fields:

- `name`
- `id`
- `level`
- `starLevel?`
- `evolutionLevel?`
- `maxLevel`
- `maxEvolutionLevel?`
- `rarity`
- `count`
- `elixirCost?`
- `iconUrls`

`count` is copies currently held in inventory. It is volatile and can be `0` for maxed or currently equipped cards.

## Item

Used by the card catalog and `currentFavouriteCard`.

Fields:

- `name`
- `id`
- `maxLevel`
- `maxEvolutionLevel?`
- `elixirCost?`
- `iconUrls`
- `rarity`

## Evolution Fields

`maxEvolutionLevel` describes static card capability:

| Value  | Meaning                          |
| ------ | -------------------------------- |
| `1`    | Evo-capable                      |
| `2`    | Hero-capable                     |
| `3`    | Supports both Evo and Hero modes |
| absent | No alternate mode                |

`evolutionLevel` is context-sensitive:

| Appears in                                       | Meaning                                                                 |
| ------------------------------------------------ | ----------------------------------------------------------------------- |
| `cards[]`                                        | Ownership: the player has this mode unlocked                            |
| `currentDeck[]`                                  | Deployment: the card is currently slotted to play as the indicated mode |
| battle-log `team[*].cards` / `opponent[*].cards` | Played-as state in that battle                                          |

Value mapping:

| Value  | Meaning                                                                                                          |
| ------ | ---------------------------------------------------------------------------------------------------------------- |
| `1`    | Evo                                                                                                              |
| `2`    | Hero                                                                                                             |
| `3`    | Evo + Hero, observed only in `cards[]`                                                                           |
| absent | No unlocked alternate mode in `cards[]`, or not configured/played as an alternate mode in deck and battle arrays |

Verified empirically across 15,442 live battles (April 2026): `evolutionLevel` appears on only 2-3 slots per battle,
slot positions match evo/hero slot mechanics, and `evolutionLevel=3` was not observed in deck or battle arrays.

For ownership checks, read `cards[]`. For deployment or played-as checks, read `currentDeck[]` or battle-log card
arrays.

## Card Level Interpretation

`level` and `maxLevel` use the API's rarity-relative scale, not a universal cross-rarity scale.

| Rarity      | API levels | Normalized levels |
| ----------- | ---------: | ----------------: |
| `common`    |     `1-16` |            `1-16` |
| `rare`      |     `1-14` |            `3-16` |
| `epic`      |     `1-11` |            `6-16` |
| `legendary` |      `1-8` |            `9-16` |
| `champion`  |      `1-6` |           `11-16` |

Conversion:

- `common`: `normalized = level`
- `rare`: `normalized = level + 2`
- `epic`: `normalized = level + 5`
- `legendary`: `normalized = level + 8`
- `champion`: `normalized = level + 10`

## Badges

Progress badge:

```json
{ "name": "Grand12Wins", "level": 5, "maxLevel": 8, "progress": 150, "target": 250, "iconUrls": { "large": "..." } }
```

One-time badge:

```json
{ "name": "Crl20Wins2021", "progress": 20, "iconUrls": { "large": "..." } }
```

One-time badges omit `level`, `maxLevel`, and `target` entirely. They are not present as `null`.

Badge categories observed:

- Mastery badges, such as `MasteryKnight`
- Challenge badges, such as `Classic12Wins`
- Mode badges, such as `2v2`, `RampUp`, `SuddenDeath`, `Draft`, `2xElixir`
- Collection badges, such as `EmoteCollection`, `BannerCollection`, `CollectionLevel`, `ClanDonations`
- Seasonal badges, such as `SeasonalBadge_202507_v2`
- Event badges, such as `CrlSpectator2022` and `EasterEgg`
- Career badges, such as `YearsPlayed`, `BattleWins`, `ClanWarsVeteran`, `LadderTop1000`

## Achievements

```json
{ "name": "Team Player", "stars": 3, "value": 1717, "target": 1, "info": "Join a Clan", "completionInfo": null }
```

Fields:

- `name`
- `stars` - integer `0-3`
- `value`
- `target`
- `info`
- `completionInfo` - typically null

Known achievements: Team Player, Friend in Need, Road to Glory, Gatherer, TV Royale, Tournament Rewards, Tournament
Host, Tournament Player, Challenge Streak, Practice with Friends, Special Challenge, Friend in Need II.

## Chest

Used by `GET /players/{playerTag}/upcomingchests`.

```json
{ "index": 0, "name": "Gold Crate" }
```

Indices are non-contiguous because only notable chests are listed.

Observed names include Gold Crate, Plentiful Gold Crate, Overflowing Gold Crate, Golden Chest, Magical Chest, Giant
Chest, Epic Chest, Legendary Chest, Mega Lightning Chest, Royal Wild Chest, and Tower Troop Chest.

## Progress

```json
{
  "": {
    "arena": { "id": 168000059, "name": "Diamond", "rawName": "AutoChessArena10_2025_Oct" },
    "trophies": 4257,
    "bestTrophies": 4337
  },
  "AutoChess_2026_Mar": {
    "arena": { "id": 168000059, "name": "Diamond", "rawName": "AutoChessArena10_2025_Oct" },
    "trophies": 3460,
    "bestTrophies": 3593
  }
}
```

`progress` is a map of opaque mode-season IDs to arena/trophy data. The empty string key is a legacy/default bucket. Do
not treat the key names as a stable enum.
