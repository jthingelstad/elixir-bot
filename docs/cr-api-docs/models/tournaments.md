# Tournament Models

Tournament field shapes verified against live API responses (March-April 2026).

## TournamentHeader

Used by `GET /tournaments`.

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

Fields:

- `tag`
- `type`
- `status`
- `creatorTag`
- `name`
- `levelCap`
- `firstPlaceCardPrize`
- `capacity`
- `maxCapacity`
- `preparationDuration`
- `duration`
- `createdTime`
- `gameMode`

Search results do not include `membersList`, `description`, `startedTime`, or `endedTime`.

## Tournament

Used by `GET /tournaments/{tournamentTag}`.

Includes all `TournamentHeader` fields plus:

- `membersList`
- `startedTime?`
- `endedTime?`
- `description?`

Optional fields are absent when not applicable, not present as null.

## TournamentMember

```json
{
  "tag": "#2RG0GRJ0U",
  "name": "ziikadaBalada",
  "score": 8,
  "rank": 2,
  "clan": { "tag": "#GC02QYJ", "name": "MaLibu JJ", "badgeId": 16000163 }
}
```

Fields:

- `tag`
- `name`
- `score`
- `rank`
- `clan?`

`clan` is absent when the player has no clan.

## Enums

Observed tournament type values:

- `open`
- `passwordProtected`

Observed tournament status values:

- `inPreparation`
- `inProgress`
- `ended`

`ended` was confirmed in April 2026 via direct tag fetch. `startedTime` and `endedTime` are present on ended
tournaments.

## Global Tournament Models

`GET /globaltournaments` returns `LadderTournamentList`.

Official Swagger describes each `LadderTournament` item with these fields:

- `tag`
- `title`
- `startTime`
- `endTime`
- `gameMode`
- `maxLosses`
- `tournamentLevel`
- `minExpLevel`
- `milestoneRewards`
- `freeTierRewards`
- `topRankReward`
- `maxTopRewardRank`

Reward arrays use `SurvivalMilestoneReward` items with official fields: `rarity`, `chest`, `resource`, `type`, `amount`,
`card`, `consumableName`, and `wins`.

When no global tournaments are active, the endpoint returns:

```json
{ "items": [] }
```

The populated `LadderTournament` shape was not re-verified in the March 2026 pass because no global tournaments were
active. Treat the field list above as official Swagger surface until confirmed live.
