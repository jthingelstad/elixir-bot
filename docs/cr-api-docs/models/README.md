# Clash Royale API - Models

This directory contains response model notes split by domain for agent-friendly retrieval.

Use endpoint files for request behavior, pagination, cache notes, and operational quirks. Use these model files for
field shape, optionality, nullability, and shared nested objects.

## Recommended Lookup Order

1. Open [../index.md](../index.md) for common API behavior.
2. Open the endpoint file for the target route.
3. Open only the model file(s) referenced by that endpoint.
4. Check [errors.md](errors.md) when implementing error handling, pagination, or generic client primitives.

## Files

| File                                           | Covers                                          |
| ---------------------------------------------- | ----------------------------------------------- |
| [common.md](common.md)                         | Shared objects used across domains              |
| [players.md](players.md)                       | Player profile and collection models            |
| [battles.md](battles.md)                       | Battle log models                               |
| [clans.md](clans.md)                           | Clan and member models                          |
| [river-race.md](river-race.md)                 | River race models                               |
| [locations-rankings.md](locations-rankings.md) | Locations, rankings, seasons                    |
| [leaderboards.md](leaderboards.md)             | Game-mode leaderboards                          |
| [tournaments.md](tournaments.md)               | Player-created and global tournaments           |
| [cards-events.md](cards-events.md)             | Card catalog, support items, events, challenges |
| [errors.md](errors.md)                         | Error and utility primitives                    |

## Cross-Domain Notes

- Optional fields are usually absent rather than present as `null`.
- Nullable fields are called out explicitly in the relevant model file.
- `PlayerClan`-shaped objects are reused for player, battle, tournament, and ranking clan references.
- `GameMode.name` is reliably present in battle logs but may be absent in tournament payloads.
- Official Swagger often names wrapper models such as `BattleList`, `ClanMemberList`, `TrailEventList`,
  `TournamentHeaderList`, and `LocationList`. Local endpoint docs describe the observed JSON shape directly, including
  bare arrays where the live API returns them.
