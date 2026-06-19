# Interpret Evolution Level

`evolutionLevel` has different meanings depending on where it appears.

| Context                                          | Meaning                                              |
| ------------------------------------------------ | ---------------------------------------------------- |
| `Player.cards[]`                                 | Ownership: the player has the mode unlocked          |
| `Player.currentDeck[]`                           | Deployment: the card is slotted to play as that mode |
| Battle-log `team[*].cards` / `opponent[*].cards` | Played-as state in that specific battle              |

Values:

| Value  | Meaning                                                                                            |
| ------ | -------------------------------------------------------------------------------------------------- |
| `1`    | Evo                                                                                                |
| `2`    | Hero                                                                                               |
| `3`    | Evo + Hero, observed only in `Player.cards[]`                                                      |
| absent | No alternate mode in collection, or not deployed/played as alternate mode in deck or battle arrays |

For ownership checks, read `Player.cards[]`.

For active deck or battle behavior, read `currentDeck[]` or battle-log card arrays.

## Related Docs

- [../players.md](../players.md)
- [../models/players.md](../models/players.md)
