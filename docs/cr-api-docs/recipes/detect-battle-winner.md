# Detect Battle Winner

Battle log entries do not include a direct `winner` field.

## Precedence

1. If `boatBattleWon` exists, use it.
2. Else if `team[0].trophyChange` exists:
   - positive means win
   - negative means loss
   - zero means unresolved or draw
3. Else if both sides have crowns, compare `team[0].crowns` and `opponent[0].crowns`.
4. Else treat the outcome as unresolved.

For 2v2 battles, still use the first team entry because both teammates share the same result.

## Related Docs

- [../players.md](../players.md)
- [../models/battles.md](../models/battles.md)
