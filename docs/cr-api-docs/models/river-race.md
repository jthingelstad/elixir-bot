# River Race Models

River race field shapes verified against live API responses (March-May 2026).

## CurrentRiverRace

Used by `GET /clans/{clanTag}/currentriverrace`.

Verified fields:

- `state`
- `sectionIndex`
- `periodIndex`
- `periodType`
- `clan`
- `clans`
- `periodLogs`

Observed `state`: `full`.

Observed `periodType` values:

- `training`
- `warDay`
- `colosseum`

`state` does not change per war day. Use `periodType` for the daily phase.

## RiverRaceClan

```json
{
  "tag": "#J2RGCRVG",
  "name": "Sample Clan",
  "badgeId": 16000146,
  "fame": 0,
  "repairPoints": 0,
  "participants": [],
  "periodPoints": 0,
  "clanScore": 160
}
```

Verified fields:

- `tag`
- `name`
- `badgeId`
- `fame`
- `repairPoints`
- `participants`
- `periodPoints`
- `clanScore`
- `finishTime?`

`finishTime` can appear in live current-river-race payloads after a clan finishes. The sentinel value
`19691231T235959.000Z` should not be treated as a usable completion timestamp.

`trophyChange` appears in `/riverracelog` standings, not in the live `currentriverrace` payload.

## RiverRaceParticipant

```json
{
  "tag": "#RCCY80VG2",
  "name": "Ram",
  "fame": 0,
  "repairPoints": 0,
  "boatAttacks": 0,
  "decksUsed": 0,
  "decksUsedToday": 0
}
```

Verified fields:

- `tag`
- `name`
- `fame`
- `repairPoints`
- `boatAttacks`
- `decksUsed`
- `decksUsedToday`

Participant counts can exceed current clan member count because players who leave during the race can remain in the race
data.

## PeriodLog

```json
{
  "periodIndex": 3,
  "items": []
}
```

Fields:

- `periodIndex`
- `items`

## PeriodLogEntry

Verified fields:

- `clan`
- `pointsEarned`
- `progressStartOfDay`
- `progressEndOfDay`
- `endOfDayRank`
- `progressEarned`
- `numOfDefensesRemaining`
- `progressEarnedFromDefenses`

`endOfDayRank` is 0-indexed: `0` means 1st place, up to `4` for 5th place. `-1` is a sentinel for not yet ranked or day
not finished.

## RiverRaceLogEntry

Used by `GET /clans/{clanTag}/riverracelog`.

```json
{
  "seasonId": 130,
  "sectionIndex": 0,
  "createdDate": "20260309T095606.000Z",
  "standings": []
}
```

Verified fields:

- `seasonId`
- `sectionIndex`
- `createdDate`
- `standings`

`seasonId` is a sequential integer, not the `YYYY-MM` format used for league seasons.

## RiverRaceStanding

Verified fields:

- `rank`
- `trophyChange`
- `clan`

The embedded `clan` object uses the `RiverRaceClan` shape and includes `finishTime` in log entries.

Season and section notes:

- Races always have 5 clans.
- Most seasons are 4 weeks, but some are 5 weeks.
- Colosseum is always the final section, but do not infer it from `sectionIndex` alone.
- Use `trophyChange` from the log or `periodType` from current river race to identify colosseum context.
- Regular weeks use ±20 trophy changes; colosseum uses ±100.
