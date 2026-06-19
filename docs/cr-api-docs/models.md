# Clash Royale API - Model Reference

Field shapes verified against live API responses (March-April 2026).

This file is a routing page. Detailed model notes live in the `models/` directory so agents can load only the schema
area they need.

## Model Files

| File                                                         | Covers                                                                            |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------- |
| [models/README.md](models/README.md)                         | Model index and recommended lookup order                                          |
| [models/common.md](models/common.md)                         | Shared objects such as `Arena`, `Location`, `PlayerClan`, and `GameMode`          |
| [models/players.md](models/players.md)                       | Player profile, cards, badges, achievements, progress, and upcoming chests        |
| [models/battles.md](models/battles.md)                       | Battle log, `PlayerBattleData`, and duel rounds                                   |
| [models/clans.md](models/clans.md)                           | Clan, clan members, clan search results, and deprecated classic war models        |
| [models/river-race.md](models/river-race.md)                 | Current river race and river race log models                                      |
| [models/locations-rankings.md](models/locations-rankings.md) | Locations, rankings, Path of Legend rankings, and league seasons                  |
| [models/leaderboards.md](models/leaderboards.md)             | Game-mode leaderboard metadata and ranking entries                                |
| [models/tournaments.md](models/tournaments.md)               | Player-created tournaments and global tournaments                                 |
| [models/cards-events.md](models/cards-events.md)             | Card catalog, support items, events, challenges, and inaccessible related models  |
| [models/errors.md](models/errors.md)                         | Error payloads, common reasons, pagination primitives, and generic utility models |

## Fast Lookup

- Need endpoint behavior first? Start with [index.md](index.md), then the endpoint file.
- Need a response field list? Open the model file for that domain.
- Need shared optionality rules? See [models/errors.md](models/errors.md) and the domain-specific model file.
- Need card level or `evolutionLevel` semantics? See [models/players.md](models/players.md) and [cards.md](cards.md).
