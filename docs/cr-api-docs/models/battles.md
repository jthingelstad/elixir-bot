# Battle Models

Battle-log field shapes verified against live API responses (March-April 2026).

## Battle

Used by `GET /players/{playerTag}/battlelog`, which returns a bare JSON array.

```json
{
  "type": "PvP",
  "battleTime": "20260309T025623.000Z",
  "isLadderTournament": false,
  "arena": { "id": 54000141, "name": "Magic Academy", "rawName": "Arena_L15" },
  "gameMode": { "id": 72000006, "name": "Ladder" },
  "deckSelection": "collection",
  "team": [],
  "opponent": [],
  "isHostedMatch": false,
  "leagueNumber": 1
}
```

Verified fields:

- `type`
- `battleTime`
- `isLadderTournament`
- `tournamentTag?`
- `eventTag?`
- `arena`
- `gameMode`
- `deckSelection`
- `team`
- `opponent`
- `modifiers?`
- `isHostedMatch`
- `leagueNumber`
- `boatBattleSide?`
- `boatBattleWon?`
- `newTowersDestroyed?`
- `prevTowersDestroyed?`
- `remainingTowers?`

Official Swagger also lists `challengeWinCountBefore`, `challengeId`, and `challengeTitle` on `Battle`. They were not
observed in the March-April 2026 live-call pass; treat them as optional official-only fields until seen in payloads.

Observed battle types:

- `PvP`
- `pathOfLegend`
- `trail`
- `clanMate`
- `clanMate2v2`
- `friendly`
- `riverRacePvP`
- `riverRaceDuel`
- `riverRaceDuelColosseum`
- `tournament`
- `boatBattle`
- `unknown`

Observed deck selections:

- `collection`
- `eventDeck`
- `draft`
- `warDeckPick`
- `pick`
- `draftCompetitive`
- `predefined`
- `quadDeckPick`

## Winner Inference

There is no explicit `winner` field. Use this order:

1. If `boatBattleWon` exists, use it.
2. Else if `team[0].trophyChange` exists, positive means win, negative means loss, zero means unresolved/draw.
3. Else if both sides have crowns, compare `team[0].crowns` and `opponent[0].crowns`.
4. Else treat the outcome as unresolved.

For 2v2 battles, use the first team entry because teammates share the same result.

## PlayerBattleData

```json
{
  "tag": "#PU9RCVYUG",
  "name": "FJ21",
  "crowns": 3,
  "kingTowerHitPoints": 9201,
  "princessTowersHitPoints": [6104, 6104],
  "clan": { "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 },
  "cards": [],
  "supportCards": [],
  "elixirLeaked": 3.33,
  "globalRank": null,
  "startingTrophies": 12286,
  "trophyChange": 26
}
```

Verified fields:

- `tag`
- `name`
- `crowns`
- `kingTowerHitPoints`
- `princessTowersHitPoints`
- `clan?`
- `cards`
- `supportCards`
- `elixirLeaked`
- `globalRank`
- `startingTrophies?`
- `trophyChange?`
- `rounds?`

Conditional notes:

- `startingTrophies` appears on PvP, Path of Legend, river race PvP, river race duel, friendly, and clanmate battles.
- `trophyChange` appears only on PvP and Path of Legend battles.
- `globalRank` is present on all battles and is null unless the player is globally ranked.
- `supportCards` is always an array and may be empty.
- `clan` is absent if the player has no clan.
- `rounds` appears only on river race duel battles.

`cards[*].evolutionLevel` is played-as state for that battle, not collection ownership. See
[players.md](players.md#evolution-fields).

## Duel Rounds

Used in `riverRaceDuel` and `riverRaceDuelColosseum` battles.

```json
{
  "crowns": 3,
  "kingTowerHitPoints": 7032,
  "princessTowersHitPoints": [4424, 3959],
  "elixirLeaked": 2.1,
  "cards": []
}
```

Fields:

- `crowns`
- `kingTowerHitPoints`
- `princessTowersHitPoints`
- `elixirLeaked`
- `cards`

Cards in duel rounds include an additional `used` boolean. Each round has a different deck. Rounds arrays usually
contain 2-3 rounds.

## CHAOS Modifiers

`modifiers` appears on CHAOS mode battles, currently `type=trail` with `Crazy_Arena`.

```json
[
  { "tag": "#PU9RCVYUG", "modifiers": ["Pekka3", "Graveyard2", "Rage1"] },
  { "tag": "#2JVGV9CG9", "modifiers": ["Fireball3", "GoblinHut2", "Berserker1"] }
]
```
