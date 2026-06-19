# Clash Royale Wiki And API Crosswalk

This page connects the public Clash Royale API docs in this repository to current gameplay context from the Clash Royale
Wiki. It is intended for agents that need to move from an API field or endpoint to the game concept it represents.

The wiki scan was performed on 2026-06-19 from the Clash Royale Wiki's own MediaWiki API and page structure, anchored at
[Clash Royale Wiki](https://clashroyale.fandom.com/wiki/Clash_Royale_Wiki). No search engine results were used for this
crosswalk.

---

## Scan Coverage

Wiki inventory:

- Wiki site statistics reported `1,967` articles.
- Main/article namespace scanned: `429` pages.
- Category namespace scanned: `327` pages.
- Deck namespace inventoried: `1,640` pages.
- Main/article pages parsed for title, categories, sections, and intro context: `429`.
- Parse errors: `0`.

Repository inventory:

- Markdown, text, and JSON files scanned locally: `36`.
- Endpoint docs, model docs, recipes, manifest data, and agent entry points were included.
- `node_modules` and generated dependency trees were excluded.

Scope choices:

- Main wiki pages were used for gameplay concepts, mechanics, cards, modes, currencies, clans, and profile context.
- Deck pages were inventoried but not imported as authoritative context because individual decks are volatile and not
  API concepts.
- Pages categorized or phrased as removed/history-only were treated as legacy context, not current gameplay truth.
- Version-history pages were scanned for current naming/availability clues only; old change history was not copied into
  gameplay docs.

---

## API To Wiki Crosswalk

| API docs                                                                                                                                           | Wiki context                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Agent guidance                                                                                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [players.md](players.md), [models/players.md](models/players.md)                                                                                   | [Player Profile](https://clashroyale.fandom.com/wiki/Player_Profile), [Trophies](https://clashroyale.fandom.com/wiki/Trophies), [Arenas](https://clashroyale.fandom.com/wiki/Arenas), [Ranked](https://clashroyale.fandom.com/wiki/Ranked), [Card Mastery](https://clashroyale.fandom.com/wiki/Card_Mastery)                                                                                                                                                                                                           | Treat a player profile as a blend of account identity, Trophy Road state, Ranked / Path of Legend state, clan status, current deck, collection, badges, achievements, and side-mode progress. Do not summarize a player from `trophies` alone.                                                                                   |
| [players.md](players.md#known-game-mode-ids), [models/battles.md](models/battles.md)                                                               | [Basics of Battle](https://clashroyale.fandom.com/wiki/Basics_of_Battle), [2v2](https://clashroyale.fandom.com/wiki/2v2), [Friendly Battle](https://clashroyale.fandom.com/wiki/Friendly_Battle), [Tournament](https://clashroyale.fandom.com/wiki/Tournament), [Special Event Challenges](https://clashroyale.fandom.com/wiki/Tournament/Special_Event_Challenges), [Clan Wars](https://clashroyale.fandom.com/wiki/Clan_Wars)                                                                                        | Battle logs are the best public API evidence for what a player actually played. Use `type`, `gameMode`, `deckSelection`, `eventTag`, `tournamentTag`, `modifiers`, and team size before assuming ladder rules.                                                                                                                   |
| [cards.md](cards.md), [models/cards-events.md](models/cards-events.md), [models/players.md](models/players.md#playeritemlevel)                     | [Cards](https://clashroyale.fandom.com/wiki/Cards), [Card Evolution](https://clashroyale.fandom.com/wiki/Card_Evolution), [Heroes](https://clashroyale.fandom.com/wiki/Heroes), [Battle Decks](https://clashroyale.fandom.com/wiki/Battle_Decks), [Tower Princess](https://clashroyale.fandom.com/wiki/Tower_Princess), [Cannoneer](https://clashroyale.fandom.com/wiki/Cannoneer), [Dagger Duchess](https://clashroyale.fandom.com/wiki/Dagger_Duchess), [Royal Chef](https://clashroyale.fandom.com/wiki/Royal_Chef) | The API splits standard cards into `items` and Tower Troops into `supportItems`. Player card levels are rarity-relative API levels; use this repo's normalization recipes before comparing power. Evolution/Hero fields are context-sensitive across catalog, collection, deck, and battle logs.                                 |
| [clans.md](clans.md), [models/clans.md](models/clans.md), [models/river-race.md](models/river-race.md)                                             | [Clans](https://clashroyale.fandom.com/wiki/Clans), [Clan Wars](https://clashroyale.fandom.com/wiki/Clan_Wars), [Trade Tokens](https://clashroyale.fandom.com/wiki/Trade_Tokens), [Friendly Battle](https://clashroyale.fandom.com/wiki/Friendly_Battle)                                                                                                                                                                                                                                                               | Clan profiles combine social membership, donations, and war score. River Race is the current war surface; classic war endpoints are legacy API baggage. Use `periodType`, participants, standings, and river-race battle types for war participation.                                                                            |
| [locations.md](locations.md), [models/locations-rankings.md](models/locations-rankings.md)                                                         | [Trophies](https://clashroyale.fandom.com/wiki/Trophies), [Arenas](https://clashroyale.fandom.com/wiki/Arenas), [Ranked](https://clashroyale.fandom.com/wiki/Ranked), [Clan Wars](https://clashroyale.fandom.com/wiki/Clan_Wars), [Tournament](https://clashroyale.fandom.com/wiki/Tournament)                                                                                                                                                                                                                         | Location endpoints mix geographic lookup, Trophy Road rankings, clan rankings, war rankings, tournament rankings, and Ranked / Path of Legend rankings. Keep these leaderboards separate; `eloRating` belongs to Ranked / Path of Legend ranking payloads.                                                                       |
| [leaderboards.md](leaderboards.md), [models/leaderboards.md](models/leaderboards.md), [data/game-modes.json](data/game-modes.json)                 | [Merge Tactics](https://clashroyale.fandom.com/wiki/Merge_Tactics), [Touchdown](https://clashroyale.fandom.com/wiki/Touchdown), [C.H.A.O.S.](https://clashroyale.fandom.com/wiki/C.H.A.O.S.), [Trophies](https://clashroyale.fandom.com/wiki/Trophies)                                                                                                                                                                                                                                                                 | `/leaderboards` is for game-mode or side-mode boards, not geographic Trophy Road rankings. Multiple IDs can share a display name, so use the ID as the stable lookup key and treat names as labels.                                                                                                                              |
| [events.md](events.md), [challenges.md](challenges.md), [models/cards-events.md](models/cards-events.md)                                           | [Events](https://clashroyale.fandom.com/wiki/Events), [Special Event Challenges](https://clashroyale.fandom.com/wiki/Tournament/Special_Event_Challenges), [Card Delivery](https://clashroyale.fandom.com/wiki/Card_Delivery), [C.H.A.O.S.](https://clashroyale.fandom.com/wiki/C.H.A.O.S.)                                                                                                                                                                                                                            | `/events` is the live public surface for active events and challenge-like modes. `/challenges` is currently undocumented and unavailable in live testing, so use `eventTag` and battle logs for event attribution.                                                                                                               |
| [tournaments.md](tournaments.md), [globaltournaments.md](globaltournaments.md), [models/tournaments.md](models/tournaments.md)                     | [Tournament](https://clashroyale.fandom.com/wiki/Tournament), [Special Event Challenges](https://clashroyale.fandom.com/wiki/Tournament/Special_Event_Challenges), [Gems](https://clashroyale.fandom.com/wiki/Gems)                                                                                                                                                                                                                                                                                                    | Player-created tournaments, victory challenges, special event challenges, and global tournaments are related gameplay concepts but have different API surfaces. Use `tournamentTag` for player-created tournament battle attribution and `/globaltournaments` plus location tournament rankings for Supercell-run global events. |
| [globaltournaments.md](globaltournaments.md), [locations.md](locations.md#global-tournament-rankings)                                              | [Tournament](https://clashroyale.fandom.com/wiki/Tournament), [Emotes](https://clashroyale.fandom.com/wiki/Emotes), [Gems](https://clashroyale.fandom.com/wiki/Gems)                                                                                                                                                                                                                                                                                                                                                   | Global tournaments may be absent without error. Empty `items` means no active global tournament, not a failed request. Rankings require an active or known `tournamentTag`.                                                                                                                                                      |
| [recipes/normalize-card-levels.md](recipes/normalize-card-levels.md), [recipes/interpret-evolution-level.md](recipes/interpret-evolution-level.md) | [Cards](https://clashroyale.fandom.com/wiki/Cards), [Card Evolution](https://clashroyale.fandom.com/wiki/Card_Evolution), [Heroes](https://clashroyale.fandom.com/wiki/Heroes)                                                                                                                                                                                                                                                                                                                                         | Use recipes when translating game concepts into code: card levels are rarity-relative, and `evolutionLevel` changes meaning depending on whether it appears in catalog/player collection, current deck, or battle-log cards.                                                                                                     |

---

## Concept Crosswalk

### Battle And Outcome

Wiki pages: [Basics of Battle](https://clashroyale.fandom.com/wiki/Basics_of_Battle),
[2v2](https://clashroyale.fandom.com/wiki/2v2), [Touchdown](https://clashroyale.fandom.com/wiki/Touchdown),
[Heist](https://clashroyale.fandom.com/wiki/Heist), [Chess Royale](https://clashroyale.fandom.com/wiki/Chess_Royale).

API surfaces:

- `GET /players/{playerTag}/battlelog`
- `Battle.type`
- `Battle.gameMode`
- `Battle.deckSelection`
- `PlayerBattleData.crowns`
- `PlayerBattleData.trophyChange`
- `Battle.boatBattleWon`

Agent context:

- The API has no universal `winner` field; infer it with the recipe in
  [recipes/detect-battle-winner.md](recipes/detect-battle-winner.md).
- Most normal battles use tower/crown logic, but modes such as Touchdown, Heist, Chess Royale, and boat battles alter
  the objective or tower model.
- Team modes change array shape: 2v2 has two `team` entries and two `opponent` entries.

### Cards, Levels, Evolutions, Heroes, And Tower Troops

Wiki pages: [Cards](https://clashroyale.fandom.com/wiki/Cards),
[Card Evolution](https://clashroyale.fandom.com/wiki/Card_Evolution),
[Heroes](https://clashroyale.fandom.com/wiki/Heroes),
[Tower Princess](https://clashroyale.fandom.com/wiki/Tower_Princess),
[Cannoneer](https://clashroyale.fandom.com/wiki/Cannoneer),
[Dagger Duchess](https://clashroyale.fandom.com/wiki/Dagger_Duchess),
[Royal Chef](https://clashroyale.fandom.com/wiki/Royal_Chef).

API surfaces:

- `GET /cards`
- `Player.cards`
- `Player.supportCards`
- `Player.currentDeck`
- `Player.currentDeckSupportCards`
- battle-log `cards`
- battle-log `supportCards`
- `maxEvolutionLevel`
- `evolutionLevel`
- `iconUrls.evolutionMedium`
- `iconUrls.heroMedium`

Agent context:

- Tower Troops are `supportItems` in the catalog and `supportCards` in player/battle payloads.
- Evolution and Hero capability is catalog-level context; ownership/deployment/played-as state must be read from the
  correct API array.
- Do not compare raw API `level` values across rarities without normalization.

### Progression And Rewards

Wiki pages: [Trophies](https://clashroyale.fandom.com/wiki/Trophies),
[Arenas](https://clashroyale.fandom.com/wiki/Arenas), [Ranked](https://clashroyale.fandom.com/wiki/Ranked),
[Chests](https://clashroyale.fandom.com/wiki/Chests), [Lucky Chests](https://clashroyale.fandom.com/wiki/Lucky_Chests),
[Experience](https://clashroyale.fandom.com/wiki/Experience),
[Pass Royale](https://clashroyale.fandom.com/wiki/Pass_Royale),
[Magic Items](https://clashroyale.fandom.com/wiki/Magic_Items), [Gold](https://clashroyale.fandom.com/wiki/Gold),
[Gems](https://clashroyale.fandom.com/wiki/Gems).

API surfaces:

- `Player.trophies`
- `Player.bestTrophies`
- `Player.arena`
- `Player.expLevel`
- `Player.expPoints`
- `Player.totalExpPoints`
- `Player.starPoints`
- `Player.currentPathOfLegendSeasonResult`
- `Player.progress`
- `GET /players/{playerTag}/upcomingchests`

Agent context:

- Trophy Road, Ranked / Path of Legend, and side-mode progress are separate. Keep them separate in summaries, scoring,
  and dashboards.
- The API exposes upcoming chest sequence names but not full reward contents.
- Currency and reward pages are useful context, but most inventories are not exposed by the public API.

### Social, Clan, And War

Wiki pages: [Clans](https://clashroyale.fandom.com/wiki/Clans),
[Clan Wars](https://clashroyale.fandom.com/wiki/Clan_Wars),
[Trade Tokens](https://clashroyale.fandom.com/wiki/Trade_Tokens),
[Friendly Battle](https://clashroyale.fandom.com/wiki/Friendly_Battle),
[Emotes](https://clashroyale.fandom.com/wiki/Emotes).

API surfaces:

- `GET /clans`
- `GET /clans/{clanTag}`
- `GET /clans/{clanTag}/members`
- `GET /clans/{clanTag}/currentriverrace`
- `GET /clans/{clanTag}/riverracelog`
- battle-log `clanMate`, `clanMate2v2`, `friendly`, `riverRacePvP`, `riverRaceDuel`, `boatBattle`

Agent context:

- Clan membership and River Race participation are different questions; use clan member endpoints for roster state and
  river-race endpoints for war state.
- Friendly battles can appear in battle logs but are practice context, not progression evidence.
- Trade Tokens, emotes, and many social cosmetics are useful wiki context but are not fully represented in the public
  API.

### Events, Challenges, And Temporary Modes

Wiki pages: [Events](https://clashroyale.fandom.com/wiki/Events),
[Special Event Challenges](https://clashroyale.fandom.com/wiki/Tournament/Special_Event_Challenges),
[Global Spell](https://clashroyale.fandom.com/wiki/Global_Spell),
[Troop Rush](https://clashroyale.fandom.com/wiki/Troop_Rush),
[Treasure Hunt](https://clashroyale.fandom.com/wiki/Treasure_Hunt),
[Obstacle Course](https://clashroyale.fandom.com/wiki/Obstacle_Course),
[Trick or Treat](https://clashroyale.fandom.com/wiki/Trick_or_Treat),
[C.H.A.O.S.](https://clashroyale.fandom.com/wiki/C.H.A.O.S.),
[Card Delivery](https://clashroyale.fandom.com/wiki/Card_Delivery).

API surfaces:

- `GET /events`
- battle-log `eventTag`
- battle-log `modifiers`
- battle-log `deckSelection`
- `GET /leaderboards`
- `GET /leaderboard/{leaderboardId}`

Agent context:

- Event mode availability changes. Use `/events` and observed battle logs instead of hardcoded wiki availability.
- Some modes are challenge-only or rotating; a wiki page does not imply a permanent API leaderboard.
- `Battle.modifiers` is currently important for C.H.A.O.S.-style battles.

---

## Legacy And Removed Signals

The scan found removed or history-heavy pages such as Achievements, Quests, Daily Tasks, Season Shop, classic Clan War
material, and older alternative trophy roads.

Use them this way:

- If the public API still returns legacy fields, document the field but mark it legacy or stale.
- Do not use removed wiki pages to infer current gameplay.
- Prefer live API observations where they conflict with old wiki history.
- For classic Clan War, this repo treats `currentwar` as removed and `warlog` as disabled; River Race is the active war
  model.

---

## Machine-Readable Data

Use [data/wiki-api-crosswalk.json](data/wiki-api-crosswalk.json) for a compact machine-readable version of this mapping.
Use [data/game-modes.json](data/game-modes.json) for game-mode families and event variants.
