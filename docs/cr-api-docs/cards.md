# Clash Royale API – Cards Endpoints

Base URL: `https://api.clashroyale.com/v1`
Auth: Bearer token in `Authorization` header

---

## Endpoints

### GET /cards
Get the full list of available cards in the game.

**Query:** None documented. In live March 2026 tests, `limit`, `after`, and `before` were ignored rather than applied.

**Returns:** `Items` object with two arrays:
- `items` — 121 standard cards (troops, spells, buildings)
- `supportItems` — 4 Tower Troops

**Standard card shape (`items`):**
```json
{
  "name": "Knight",
  "id": 26000000,
  "maxLevel": 16,
  "maxEvolutionLevel": 3,
  "elixirCost": 3,
  "iconUrls": {
    "medium": "https://api-assets.clashroyale.com/cards/300/...",
    "heroMedium": "https://api-assets.clashroyale.com/cardheroes/300/...",
    "evolutionMedium": "https://api-assets.clashroyale.com/cardevolutions/300/..."
  },
  "rarity": "common"
}
```

**Support card shape (`supportItems`):**
```json
{
  "name": "Tower Princess",
  "id": 159000000,
  "maxLevel": 16,
  "iconUrls": { "medium": "https://api-assets.clashroyale.com/cards/300/..." },
  "rarity": "common"
}
```
Support items lack `elixirCost` and `maxEvolutionLevel`.

**Rarity → maxLevel mapping (observed):**

This is the API's rarity-relative upgrade scale, not a universal cross-rarity power scale. Higher-rarity cards have fewer API level steps, but all rarities still top out at the same effective end-state when normalized.

| Rarity | maxLevel |
|--------|----------|
| common | 16 |
| rare | 14 |
| epic | 11 |
| legendary | 8 |
| champion | 6 |

**Normalized interpretation:**

- `common`: API levels `1-16` -> normalized `1-16`
- `rare`: API levels `1-14` -> normalized `3-16`
- `epic`: API levels `1-11` -> normalized `6-16`
- `legendary`: API levels `1-8` -> normalized `9-16`
- `champion`: API levels `1-6` -> normalized `11-16`

Equivalent conversion from API `level` to normalized level:

- `common`: `normalized = level`
- `rare`: `normalized = level + 2`
- `epic`: `normalized = level + 5`
- `legendary`: `normalized = level + 8`
- `champion`: `normalized = level + 10`

**iconUrls variants:**
- `medium` — always present on all cards
- `heroMedium` — present on Hero-capable cards in live March 2026 sampling
- `evolutionMedium` — present on Evo-capable cards in live March 2026 sampling

**Observed interpretation for Elixir UX:**
- `maxEvolutionLevel=1` has only been observed on cards with `evolutionMedium` and no `heroMedium`
- `maxEvolutionLevel=2` has only been observed on cards with `heroMedium` and no `evolutionMedium`
- `maxEvolutionLevel=3` has only been observed on cards with both `heroMedium` and `evolutionMedium`
- This strongly suggests:
  - `1` => Evo-capable
  - `2` => Hero-capable
  - `3` => Evo + Hero capable
- This mapping is inferred from live API payloads and local stored data. It is suitable for Elixir's player-facing interpretation layer, but it does not prove slot-based activation behavior in decks.
- Player-facing output should prefer `Evo`, `Hero`, and `Evo + Hero` instead of raw numeric `evolutionLevel` wording.

**ID ranges (observed):**
- `26000xxx` — troops
- `27000xxx` — buildings
- `28000xxx` — spells
- `159000xxx` — Tower Troops (supportItems)

---

## Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad parameters |
| 403 | Auth failure / insufficient token scope |
| 404 | Not found |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Maintenance |

Observed error bodies are usually `{ reason, message? }`. This endpoint ignored `limit=0` in March 2026 testing and still returned the full catalog. `type`/`detail` were not observed.

---

## Agent Notes
- Global catalog endpoint — not player-specific. Use `/players/{playerTag}` for a player's collected cards with levels.
- `items` vs `supportItems`: Tower Troops (cards that replace/augment crown towers) are in `supportItems`; everything else is in `items`
- Pagination parameters appear to be ignored — `/cards?limit=1`, `/cards?after=...`, and `/cards?before=...` still returned the full catalog in March 2026 testing
- `maxLevel` is the rarity-relative API cap, not a normalized universal cap. Example: champions report `maxLevel: 6`, which corresponds to normalized level 16 at full upgrade.
- `maxEvolutionLevel` is optional — only 46/121 standard cards have evolutions (values observed: 1, 2, or 3)
- Observed icon correlation: `evolutionMedium` aligns with Evo capability, `heroMedium` aligns with Hero capability, and cards with both assets appear to support both
- No `paging` object is present in responses
