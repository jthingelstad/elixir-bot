# Cards, Events, And Challenge Models

Card and event field shapes verified against live API responses (March-April 2026).

## Items

Used by `GET /cards`.

Fields:

- `items` - standard cards
- `supportItems` - Tower Troops

Observed counts:

- `items`: 121 standard cards
- `supportItems`: 4 Tower Troops

## Item

Catalog item shape:

```json
{
  "name": "Knight",
  "id": 26000000,
  "maxLevel": 16,
  "maxEvolutionLevel": 3,
  "elixirCost": 3,
  "iconUrls": {
    "medium": "https://api-assets.clashroyale.com/cards/300/...",
    "heroMedium": "https://api-assets.clashroyale.com/cardheroes/300/...",
    "evolutionMedium": "https://api-assets.clashroyale.com/cardevolutions/300/..."
  },
  "rarity": "common"
}
```

Support item shape:

```json
{
  "name": "Tower Princess",
  "id": 159000000,
  "maxLevel": 16,
  "iconUrls": { "medium": "https://api-assets.clashroyale.com/cards/300/..." },
  "rarity": "common"
}
```

Support items lack `elixirCost` and `maxEvolutionLevel`.

Card ID ranges:

- `26000xxx` - troops
- `27000xxx` - buildings
- `28000xxx` - spells
- `159000xxx` - Tower Troops

Rarity max levels:

| Rarity      | maxLevel |
| ----------- | -------: |
| `common`    |       16 |
| `rare`      |       14 |
| `epic`      |       11 |
| `legendary` |        8 |
| `champion`  |        6 |

## TrailEvent

Used by `GET /events`, which returns a bare JSON array.

```json
{ "eventTag": "#R8U2RCJ", "title": "C.H.A.O.S", "description": "Choose different modifiers during battle..." }
```

Fields:

- `eventTag`
- `title`
- `description`

`description` can be null.

`eventTag` values can appear on battle-log entries as `Battle.eventTag`.

## Challenge Models

`GET /challenges` is not currently shown in the official Swagger UI and returned `notFound` in observed live behavior.

| Model                                    | Status                                                                 |
| ---------------------------------------- | ---------------------------------------------------------------------- |
| `ChallengeChain` / `ChallengeChainsList` | Endpoint returning `notFound` in March 2026                            |
| `Challenge` / `ChallengeList`            | Not currently accessible                                               |
| `ChallengeGameMode`                      | Not currently accessible                                               |
| `SurvivalMilestoneReward`                | Officially used by `LadderTournament`; not live-verified in March 2026 |

Challenge-like active events can still appear through `GET /events`.

## Inaccessible Related Models

| Model                 | Status                        |
| --------------------- | ----------------------------- |
| `Emote` / `EmoteList` | No documented public endpoint |
