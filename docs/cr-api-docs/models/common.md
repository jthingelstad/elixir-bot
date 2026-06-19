# Common Models

Shared objects verified against live API responses (March-April 2026).

## Arena

```json
{ "id": 54000142, "name": "Ultimate Clash Pit", "rawName": "Arena_L16" }
```

Used in player profiles, clan members, battles, and side-mode progress entries.

Observed ID ranges:

- Main arena IDs: `54000xxx`
- Side-mode arena IDs: `168000xxx`

## Location

```json
{ "id": 57000249, "name": "United States", "isCountry": true, "countryCode": "US" }
```

Fields:

- `id` - integer location ID
- `name` - display name
- `isCountry` - boolean
- `countryCode` - 2-letter ISO country code, present only when `isCountry=true`

`countryCode` is absent for regions and meta-locations such as `International`. The `/locations` endpoint returns 262
total locations: 8 regions plus 254 countries.

## PlayerClan

```json
{ "tag": "#GP8292Y8", "name": "Miyake YT", "badgeId": 16000054 }
```

Used in:

- `Player.clan`
- `PlayerBattleData.clan`
- `TournamentMember.clan`
- player ranking entries

Absent, not null, when the player has no clan.

## GameMode

```json
{ "id": 72000006, "name": "Ladder" }
```

`id` is always present where `GameMode` appears. `name` is reliably present in battle-log entries, but may be absent in
tournament contexts.

Known IDs include:

| ID         | Name                         |
| ---------- | ---------------------------- |
| `72000006` | `Ladder`                     |
| `72000007` | `Friendly`                   |
| `72000051` | `TeamVsTeam_Touchdown_Draft` |
| `72000232` | `7xElixir_Friendly`          |
| `72000266` | `ClanWar_BoatBattle`         |
| `72000267` | `CW_Duel_1v1`                |
| `72000268` | `CW_Battle_1v1`              |
| `72000464` | `Ranked1v1_NewArena2`        |
| `72000502` | `Crazy_Arena`                |

See [../players.md](../players.md) for the fuller observed battle-log game mode table.
