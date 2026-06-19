# Location And Ranking Models

Location and ranking field shapes verified against live API responses (March-April 2026).

## Location

Used by `GET /locations` and `GET /locations/{locationId}`.

```json
{ "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" }
```

See [common.md](common.md#location).

## ClanRanking

Used by:

- `GET /locations/{locationId}/rankings/clans`
- `GET /locations/{locationId}/rankings/clanwars`

```json
{
  "tag": "#9LGR9PYY",
  "name": "War Knights",
  "rank": 1,
  "previousRank": 1,
  "location": { "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" },
  "clanScore": 132637,
  "members": 50,
  "badgeId": 16000038
}
```

Fields:

- `tag`
- `name`
- `rank`
- `previousRank`
- `location`
- `clanScore`
- `members`
- `badgeId`

`previousRank: -1` means the clan was not previously ranked. For `/rankings/clanwars`, `clanScore` reflects war
performance rather than overall trophies.

## PlayerPathOfLegendRanking

Used by:

- `GET /locations/{locationId}/pathoflegend/players`
- `GET /locations/global/pathoflegend/{seasonId}/rankings/players`

```json
{
  "tag": "#99GU92P0",
  "name": "TT-shadow.cr29",
  "expLevel": 78,
  "eloRating": 2247,
  "rank": 1,
  "clan": { "tag": "#R99R8G8J", "name": "Skyline", "badgeId": 16000134 }
}
```

Fields:

- `tag`
- `name`
- `expLevel`
- `eloRating`
- `rank`
- `clan?`

`clan` is absent when the player has no clan.

## PlayerRanking

Used by trophy-ranking endpoints.

Exact field availability varies by endpoint and season state. Current global trophy rankings may return an empty `items`
array early in a season.

## LeagueSeason

Used by:

- `GET /locations/global/seasons`
- `GET /locations/global/seasons/{seasonId}`

```json
{ "id": "2025-01" }
```

Season IDs use `YYYY-MM` format. Early seasons from 2016-2017 can contain duplicate month IDs.

## Broken Season Models

`GET /locations/global/seasonsV2` returns item counts but null data:

```json
{ "code": null, "uniqueId": null, "endTime": null }
```

Use `/locations/global/seasons` instead.

`GET /locations/global/seasons/{seasonId}/rankings/players` returned `notFound` for all tested seasons from 2024
through 2026. Use Path of Legend season rankings instead.
