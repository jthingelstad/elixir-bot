# Leaderboard Models

Leaderboard field shapes verified against live API responses (March 2026).

## Leaderboard Metadata

Used by `GET /leaderboards`.

```json
{ "id": 170000019, "name": "Merge Tactics" }
```

Fields:

- `id` - integer
- `name` - string

Multiple leaderboards can share the same name with different IDs.

## Leaderboard Ranking Entry

Used by `GET /leaderboard/{leaderboardId}`.

```json
{
  "tag": "#PU9RCVYUG",
  "name": "FJ21",
  "rank": 1,
  "score": 4047,
  "clan": { "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 }
}
```

Fields:

- `tag`
- `name`
- `rank`
- `score`
- `clan?`

`clan` is absent when the player has no clan.

`GET /leaderboard/{leaderboardId}` can return up to 10,000 entries when no `limit` is specified.
