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

## Scoring: fame vs period points (and boat defenses)

A River Race exposes two distinct scores. They are **not interchangeable**, and comparing one clan's period points
against another clan's fame is a category error.

- **`periodPoints`** (on `clan`) — the clan's score for the **current period (the day)**. It **resets to `0` at each
  daily reset**. On a Battle Day this is the number that climbs as members complete their attacks. (Observed: a clan
  sitting at ~11,000 `periodPoints` late on a Battle Day showed `0` for every clan at the next day's start.)
- **`fame`** (on `clan`) — the clan's **cumulative score for the whole race/week**: the boat's position along the river
  and the value that **decides the winner**. It is `0` during the first Battle Day (nothing has been banked yet) and
  accumulates as each day closes. This is why a live `currentriverrace` payload can show `fame: 0` mid-race on day 1
  while `periodPoints` is large — the day's points have not yet been converted to fame.

**Daily → cumulative conversion (`periodLogs` / `PeriodLogEntry`).** At each day's close the result is recorded per
clan:

- `pointsEarned` — the points the clan earned that period.
- `progressStartOfDay` / `progressEndOfDay` — the clan's cumulative race progress (fame / boat position) before and
  after the day; `progressEarned` is the gain.
- `endOfDayRank` — the clan's placement at day end (0-indexed).

**Boat defenses.** Each clan's boat has defenses; `numOfDefensesRemaining` tracks how many are still standing, and
`progressEarnedFromDefenses` is the portion of that day's `progressEarned` contributed by surviving defenses (as opposed
to offensive attacks). Defensive contribution is part of the clan's daily progress, so a clan can gain fame at day close
from defenses even beyond its members' attack points.

**Finish line / Colosseum.** A standard River Race week runs until a clan reaches the end of the river (a
cumulative-fame threshold — commonly 10,000 in a normal week). **Colosseum** (the season's final section,
`periodType: "colosseum"`) is a multi-day period-point contest rather than a weekly fame race, and uses ±100 trophy
stakes (vs ±20 for regular weeks — see `RiverRaceStanding`).

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
