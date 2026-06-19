# Normalize Card Levels

The API reports card levels on a rarity-relative scale. Normalize before comparing cards across rarities.

| Rarity      | API levels | Normalized levels | Formula      |
| ----------- | ---------: | ----------------: | ------------ |
| `common`    |     `1-16` |            `1-16` | `level`      |
| `rare`      |     `1-14` |            `3-16` | `level + 2`  |
| `epic`      |     `1-11` |            `6-16` | `level + 5`  |
| `legendary` |      `1-8` |            `9-16` | `level + 8`  |
| `champion`  |      `1-6` |           `11-16` | `level + 10` |

Tower Troops are in `supportItems` in the catalog and `supportCards` in player payloads.

## Related Docs

- [../cards.md](../cards.md)
- [../models/players.md](../models/players.md)
- [../models/cards-events.md](../models/cards-events.md)
