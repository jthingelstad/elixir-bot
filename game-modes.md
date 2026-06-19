# Clash Royale Gameplay Modes

This page is gameplay context for agents using the public Clash Royale API. It is not an endpoint reference; use it to
interpret what a player is doing when Trophy Road alone does not explain their activity.

For the broader scan-backed mapping between wiki concepts and API docs, see
[wiki-api-crosswalk.md](wiki-api-crosswalk.md).

Wiki-derived context was retrieved from the Clash Royale Wiki on 2026-06-19. The notes below deliberately ignore
history-only and removed content. When the wiki says a mode is temporary, rotating, or only available in challenges, do
not treat it as a stable always-on ladder unless the live API also exposes current events or leaderboard data for it.

---

## Avoid Trophy Road Bias

Trophy Road is only one activity surface. Agents that only read `Player.trophies`, `Player.arena`, or battle-log
`type=PvP` will miss meaningful player activity in Ranked, Clan Wars, tournaments, events, 2v2, and side modes.

Use these signals first:

- Trophy Road: `Player.trophies`, `Player.bestTrophies`, `Player.arena`, battle-log `type=PvP`, `gameMode.name=Ladder`,
  and `team[0].trophyChange`.
- Ranked / Path of Legend: `currentPathOfLegendSeasonResult`, `lastPathOfLegendSeasonResult`,
  `bestPathOfLegendSeasonResult`, battle-log `type=pathOfLegend`, `leagueNumber`, and `/locations/*/pathoflegend/*`
  ranking endpoints.
- Clan Wars / River Race: `/clans/{tag}/currentriverrace`, `/clans/{tag}/riverracelog`, battle-log `riverRacePvP`,
  `riverRaceDuel`, `riverRaceDuelColosseum`, and `boatBattle`.
- Events and challenges: `GET /events`, battle-log `eventTag`, `type=trail`, `gameMode`, `deckSelection`, and optional
  `modifiers`.
- Tournaments: `/tournaments`, `/globaltournaments`, `Battle.tournamentTag`, and tournament/global tournament ranking
  endpoints.
- Side modes: `Player.progress` opaque keys and `/leaderboards`/`/leaderboard/{leaderboardId}`.

---

## Mode Taxonomy

| Surface                    | Gameplay meaning                                                                                                                                                  | Primary API signals                                                                                                                                                                                                           |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Trophy Road / Arenas       | Standard ladder progression. Wins and losses move the player's trophy count, unlock Arenas, and determine arena-gated rewards.                                    | `Player.trophies`, `Player.bestTrophies`, `Player.arena`, battle-log `type=PvP`, `gameMode.name=Ladder`, `trophyChange`.                                                                                                      |
| Ranked / Path of Legend    | Separate ranked ladder formerly known in client-facing terms as Path of Legends. The API still uses `pathoflegend` and Path of Legend field names.                | `currentPathOfLegendSeasonResult`, `lastPathOfLegendSeasonResult`, `bestPathOfLegendSeasonResult`, `type=pathOfLegend`, `/locations/{id}/pathoflegend/players`, `/locations/global/pathoflegend/{seasonId}/rankings/players`. |
| Merge Tactics              | Separate 4-player auto-battler side mode with draft/deploy/merge rounds and its own progression currency (`Starsteel` in wiki terminology).                       | `Player.progress` mode-season keys, `/leaderboards`, `/leaderboard/{leaderboardId}` entries named `Merge Tactics`. Do not infer this from `Player.trophies`.                                                                  |
| Clan Wars / River Race     | Clan competition over weekly river races. Players use up to four unique War Decks, with Training Days and Battle Days.                                            | `/clans/{tag}/currentriverrace`, `/clans/{tag}/riverracelog`, participant `decksUsed`, `decksUsedToday`, battle-log river-race types, `boatBattleWon`.                                                                        |
| Victory Challenges         | Classic Challenge, Grand Challenge, and Mega Draft Challenge. These are challenge runs with win/loss limits and separate matchmaking.                             | Active challenge-like entries can appear in `/events`; `/challenges` is currently not usable. Battle logs may show non-ladder `type`/`gameMode` combinations.                                                                 |
| Special Event Challenges   | Temporary challenges with rule variants: 1v1/2v2, draft formats, elixir variants, environment changes, locked cards, mirror decks, and other special rules.       | `GET /events`, battle-log `eventTag`, `type=trail`, `deckSelection=eventDeck`, `draft`, `draftCompetitive`, `predefined`, or `pick`.                                                                                          |
| Global Tournaments         | Supercell-run tournament events with global ranking and event-specific rules.                                                                                     | `/globaltournaments`, `/locations/global/rankings/tournaments/{tournamentTag}` when an active tournament tag exists.                                                                                                          |
| Player-created Tournaments | Short-term competitions created by players, optionally public, password-protected, or closed.                                                                     | `/tournaments`, `/tournaments/{tournamentTag}`, battle-log `type=tournament`, `tournamentTag`, `gameMode`.                                                                                                                    |
| 2v2                        | Two players versus two players. It can appear as an event, challenge, friendly format, or other temporary surface; normal 2v2 does not risk Trophy Road trophies. | Battle `team` and `opponent` arrays with two entries each, `type=clanMate2v2` for clanmate 2v2, `gameMode.name=TeamVsTeam` or 2v2-specific event modes.                                                                       |
| Friendly Battle            | Practice battles against friends or clanmates. No normal progression rewards.                                                                                     | Battle-log `type=clanMate`, `clanMate2v2`, `friendly`, or rare `unknown`; often `gameMode.name=Friendly` or an event-friendly mode.                                                                                           |
| Training Camp              | Practice against an AI trainer; tutorial and risk-free bot practice.                                                                                              | Not a meaningful public API activity surface. Do not expect it to appear as normal player-versus-player progression.                                                                                                          |
| Card Delivery              | Temporary card-promotion/tutorial-style event against a non-player opponent, with one-time rewards.                                                               | Treat as event context only if surfaced by `/events`; do not model it as Trophy Road, Ranked, or normal PvP.                                                                                                                  |

---

## Ranked / Path Of Legend

Use `Ranked` for player-facing wording when discussing current gameplay, but keep `PathOfLegend`/`pathoflegend` in code
because the public API still uses those names.

Important distinctions:

- Ranked is separate from Trophy Road. A Trophy Road-only activity counter will undercount ranked players.
- Ranked entry is based on current-season trophy progress or the previous season's Ranked league status, not simply
  ordinary Trophy Road matchmaking.
- Ranked advances through league steps rather than Trophy Road trophies until Ultimate Champion.
- Ultimate Champion uses a rating score; the API exposes this as `eloRating` on ranking endpoints.
- The player profile's current/last/best Path of Legend result objects can be `null`, and `rank` inside those objects
  can also be `null`.

Implementation guidance:

- Treat `type=pathOfLegend` battle-log rows as ranked games even if user-facing copy says Ranked.
- Do not mix `/locations/{id}/rankings/players` with `/locations/{id}/pathoflegend/players`; they are separate
  leaderboards.
- When summarizing a player, show Trophy Road trophies and Ranked status separately.
- When ranking players, prefer Path of Legend ranking endpoints for current competitive ranked standing. The older
  season trophy ranking endpoint is documented in this repo as broken for tested recent seasons.

---

## Event And Challenge Variants

Special event modes are rule packages. They can appear in challenges, Party-style rotations, Clan Wars, private
tournaments, or the current main-screen mode switcher depending on season state. Avoid hardcoding them as permanent
surfaces.

| Variant                                                        | Agent interpretation                                                                                                                                       |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2v2                                                            | Four-player team battle; each side has two player entries. Do not apply 1v1-only assumptions to team/opponent arrays.                                      |
| Touchdown                                                      | No normal Crown Towers; units score by reaching the opponent's end zone. Crown-based winner logic may still work, but tower-hitpoint assumptions can fail. |
| Heist                                                          | Safe-focused objective with no defensive Crown Towers in the usual sense. Treat tower and spell-damage assumptions carefully.                              |
| Global Spell                                                   | Normal-ish 1v1 objective with a global spell effect applied to the arena.                                                                                  |
| Battleground                                                   | No river/bridges; troops move more directly toward towers and may interact with claimable center elements.                                                 |
| Troop Rush                                                     | A featured troop, spell, or special unit spawns periodically for both sides.                                                                               |
| Treasure Hunt                                                  | Neutral center objective with separate health/progress for each side; destroying it grants a benefit such as a troop or elixir.                            |
| Obstacle Course                                                | Neutral center unit or obstacle changes pathing or combat without belonging to either side.                                                                |
| Chess Royale                                                   | Alternate tower layout inspired by chess, with more tower-like buildings than normal 1v1.                                                                  |
| Trick or Treat                                                 | Claimable presents/buildings on both sides trigger randomized effects.                                                                                     |
| C.H.A.O.S.                                                     | Temporary 1v1-like mode where players choose card modifiers during the match; battle logs can include a `modifiers` array.                                 |
| Draft / Triple Draft / Mega Draft                              | Decks are selected during the match setup rather than purely from the player's active ladder deck. Use `deckSelection` instead of assuming `currentDeck`.  |
| Double / Triple / Infinite Elixir, Ramp Up, Sudden Death, Rage | Elixir or overtime rules differ from normal ladder. These are usually event/challenge/riverrace rotating rule sets.                                        |

For event variants, the API's best stable handles are `eventTag`, `gameMode.id`, `gameMode.name`, and `deckSelection`.
Names and active availability change over time, so use `/events` and battle logs as the live source.

---

## Clan Wars / River Race

Clan Wars are not just another ladder. The current system is River Race based:

- Wars run continuously in multi-week seasons, with one River Race per week.
- Training Days and Battle Days have different stakes. Training Days reward gold; Battle Days contribute medals and
  river progress.
- Players create four War Decks with unique cards across decks. A used War Deck cannot be reused until the next day.
- River tasks include 1v1 battles, duels, final-week colosseum duels, rotating modes, and boat battles.
- Boat Battles are PvE-style attacks against clan boat defenses; damage can persist for later attacks.

API implications:

- Use `periodType` on `/currentriverrace` rather than `state` to determine the daily phase.
- Use `decksUsed` and `decksUsedToday` on participants for participation tracking.
- Do not expect `trophyChange` on live `currentriverrace`; use `/riverracelog` for standings trophy movement.
- Use battle-log river race types to observe individual play, and clan endpoints to observe clan progress.

---

## Side-Mode Progress And Leaderboards

Some modes have separate progress or leaderboard surfaces:

- `Player.progress` is a map keyed by opaque mode-season identifiers. Treat keys as labels, not a stable enum.
- `/leaderboards` lists game-mode leaderboards such as Merge Tactics, Touchdown, and other seasonal/side-mode boards.
- Multiple leaderboard IDs can share the same display name, usually because each ID represents a different season or
  variant.
- A leaderboard name alone does not prove the mode is currently playable. Check `/events`, current battle logs, or the
  latest `/leaderboards` response before declaring active availability.

Removed or history-only alternative trophy roads from the wiki are intentionally excluded from this guide. If an old
mode name appears in historical API data or stale leaderboards, classify it as a legacy/side-mode artifact unless
current events or live battle logs confirm activity.

---

## Source Pages

| Topic                                 | Source                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Wiki navigation and gameplay taxonomy | [Clash Royale Wiki](https://clashroyale.fandom.com/wiki/Clash_Royale_Wiki)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Core battle rules                     | [Basics of Battle](https://clashroyale.fandom.com/wiki/Basics_of_Battle)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Trophy Road and Arenas                | [Arenas](https://clashroyale.fandom.com/wiki/Arenas), [Trophies](https://clashroyale.fandom.com/wiki/Trophies)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Ranked / Path of Legend               | [Ranked](https://clashroyale.fandom.com/wiki/Ranked)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Tournaments and challenges            | [Tournament](https://clashroyale.fandom.com/wiki/Tournament), [Special Event Challenges](https://clashroyale.fandom.com/wiki/Tournament/Special_Event_Challenges)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Clan Wars / River Race                | [Clan Wars](https://clashroyale.fandom.com/wiki/Clan_Wars)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Team and practice modes               | [2v2](https://clashroyale.fandom.com/wiki/2v2), [Friendly Battle](https://clashroyale.fandom.com/wiki/Friendly_Battle), [Training Camp](https://clashroyale.fandom.com/wiki/Training_Camp)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Side and event modes                  | [Merge Tactics](https://clashroyale.fandom.com/wiki/Merge_Tactics), [Touchdown](https://clashroyale.fandom.com/wiki/Touchdown), [Heist](https://clashroyale.fandom.com/wiki/Heist), [Global Spell](https://clashroyale.fandom.com/wiki/Global_Spell), [Battleground](https://clashroyale.fandom.com/wiki/Battleground), [Troop Rush](https://clashroyale.fandom.com/wiki/Troop_Rush), [Treasure Hunt](https://clashroyale.fandom.com/wiki/Treasure_Hunt), [Obstacle Course](https://clashroyale.fandom.com/wiki/Obstacle_Course), [Chess Royale](https://clashroyale.fandom.com/wiki/Chess_Royale), [Trick or Treat](https://clashroyale.fandom.com/wiki/Trick_or_Treat), [C.H.A.O.S.](https://clashroyale.fandom.com/wiki/C.H.A.O.S.) |
