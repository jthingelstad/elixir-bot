# Clan Models

Clan field shapes verified against live API responses (March-April 2026).

## Clan

Used by `GET /clans/{clanTag}`.

Verified fields:

- `tag`
- `name`
- `description`
- `type`
- `badgeId`
- `clanScore`
- `clanWarTrophies`
- `requiredTrophies`
- `donationsPerWeek`
- `clanChestStatus`
- `clanChestLevel`
- `clanChestMaxLevel`
- `members`
- `memberList`
- `location`

`type` values:

- `open`
- `inviteOnly`
- `closed`

`badgeUrls` is not present in clan responses. Only `badgeId` is returned.

## ClanMember

Used by `memberList` and `GET /clans/{clanTag}/members`.

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

Verified fields:

- `tag`
- `name`
- `role`
- `lastSeen`
- `expLevel`
- `trophies`
- `arena`
- `clanRank`
- `previousClanRank`
- `donations`
- `donationsReceived`
- `clanChestPoints`

Role values:

- `member`
- `elder`
- `coLeader`
- `leader`

## Clan Search Result

Used by `GET /clans`.

Clan search results include a subset of `Clan` fields:

- `tag`
- `name`
- `type`
- `badgeId`
- `clanScore`
- `clanWarTrophies`
- `location`
- `requiredTrophies`
- `donationsPerWeek`
- `clanChestLevel`
- `clanChestMaxLevel`
- `members`

Search results do not include `memberList` or `description`.

## Classic War Models

Classic clan war endpoints are not usable.

| Model                                | Status                                         |
| ------------------------------------ | ---------------------------------------------- |
| `CurrentClanWar`                     | Endpoint permanently removed, returns 410 Gone |
| `ClanWarClan` / `ClanWarParticipant` | No longer accessible                           |
| `ClanWarLog` / `ClanWarLogEntry`     | Endpoint disabled, returns 404                 |
| `ClanWarStanding`                    | No longer accessible                           |
