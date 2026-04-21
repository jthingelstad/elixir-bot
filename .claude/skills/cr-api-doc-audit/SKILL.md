---
name: cr-api-doc-audit
description: Cross-check live Clash Royale API payloads stored in raw_api_payloads against docs/cr-api-docs/ and recommend concrete documentation patches — undocumented fields, new enum values, type or nullability drift, shape changes
---

# CR API Doc Audit

Read live API responses out of `elixir.db` and compare them to the agent-facing reference in `docs/cr-api-docs/`. The goal is a tight, evidence-backed list of **doc patches worth making** — not a wall of field tables.

Pairs with `log-triage` (runtime health) and `awareness-report` (agent quality). This skill answers: *is our CR API reference keeping up with what Supercell is actually shipping?*

## Scope

Source of truth for live behavior: `/Users/jamie/Projects/elixir-bot/elixir.db` → table `raw_api_payloads`.

Only three endpoints are persisted as raw payloads (deduped by content hash):

| endpoint          | doc page       | call site                      |
|-------------------|----------------|--------------------------------|
| `player`          | `players.md`   | `storage/player.py` snapshot    |
| `player_battlelog`| `players.md`   | `storage/player.py` snapshot    |
| `clan_war_log`    | `clans.md`     | `storage/war_ingest.py` ingest  |

Other endpoints (`clan`, `currentriverrace`, `riverracelog`, `tournament`, `player_chests`, `cards`) are fetched but not persisted — if the user asks to audit them, say so and fall back to either (a) live `cr_api.py` calls in the REPL, or (b) the structured tables that ingest parts of those responses (e.g. `war_races` for riverracelog). Do not guess a schema for endpoints we do not store.

Default window: the full retained history in `raw_api_payloads`. Payload retention is 180 days (see `storage/metadata.py:RAW_PAYLOAD_RETENTION_DAYS`). The user can narrow: "just the last week", "only since 2026-04-01", "one payload per player".

## Sampling strategy

You do not need every payload. A representative sample is enough to find schema drift.

- **Field presence / type survey**: sample ~1 payload per `entity_key` per endpoint. Across ~50 members this yields 50 player payloads, 50 battlelogs, and ≤5 war logs — plenty to see optional fields, null shapes, and enum spread.
- **Rare-enum hunt**: when looking for new battle types, game mode IDs, or `deckSelection` values, scan *all* battlelog payloads — the rare cases are exactly what's under-represented.
- **Null / conditional coverage**: sample at least one payload from a player with no clan, one with no `leagueStatistics`, and one with no PoL history. Filter by predicate (`json_extract(payload_json, '$.clan') IS NULL`) to find them deliberately.

## Queries to run

Run these as a starter kit; widen or narrow based on what the user asked.

### 1. Inventory — what do we have to audit?

```sql
SELECT endpoint,
       COUNT(*)                  AS payloads,
       COUNT(DISTINCT entity_key) AS entities,
       MIN(fetched_at)           AS first_seen,
       MAX(fetched_at)           AS last_seen
FROM raw_api_payloads
GROUP BY endpoint
ORDER BY payloads DESC;
```

Sanity check: if `endpoint` has fewer than ~5 payloads, schema conclusions about optional fields are weak — say so.

### 2. Field-presence survey for a given endpoint

Take one payload per entity, flatten each to a list of JSON paths with their JSON types, and aggregate. This is best done with a small Python script via the `Bash` tool; don't try to do it in pure SQL. See **Example Python runner** below.

### 3. Enum-hunt for battlelog

Known enums to check against `players.md`:
- `type` (battle type)
- `gameMode.id` and `gameMode.name`
- `deckSelection`
- `arena.rawName`
- Modifier names in CHAOS payloads

```sql
-- Distinct gameMode IDs ever observed
SELECT DISTINCT json_extract(b.value, '$.gameMode.id')   AS gm_id,
                json_extract(b.value, '$.gameMode.name') AS gm_name,
                json_extract(b.value, '$.type')          AS battle_type,
                COUNT(*)                                 AS n
FROM raw_api_payloads p, json_each(p.payload_json) AS b
WHERE p.endpoint = 'player_battlelog'
GROUP BY gm_id, gm_name, battle_type
ORDER BY gm_id, battle_type;
```

Cross-reference each row against the "Known game mode IDs" and "Battle types observed" tables in `players.md`. Anything unknown is a doc patch candidate.

### 4. Null vs. missing for player optional fields

`players.md` is very specific about which optional fields are **absent** vs. **present-and-null**. Verify:

```sql
SELECT
  SUM(json_extract(payload_json, '$.clan') IS NULL) AS clan_null_or_missing,
  SUM(json_type(payload_json, '$.clan') = 'null')   AS clan_explicit_null,
  SUM(json_type(payload_json, '$.clan') = 'object') AS clan_present,
  SUM(json_extract(payload_json, '$.leagueStatistics') IS NULL) AS league_null_or_missing,
  SUM(json_type(payload_json, '$.currentPathOfLegendSeasonResult') = 'null') AS pol_explicit_null,
  SUM(json_type(payload_json, '$.currentPathOfLegendSeasonResult') = 'object') AS pol_present
FROM raw_api_payloads
WHERE endpoint = 'player';
```

If `clan_explicit_null > 0`, the doc's "absent (not null) when the player has no clan" claim is wrong for at least some responses — worth patching.

## Example Python runner (for field-presence surveys)

Put this in a temporary file or run it inline via `Bash`. Do **not** commit it — the skill is analysis-only.

```python
import json, sqlite3
from collections import defaultdict

conn = sqlite3.connect("/Users/jamie/Projects/elixir-bot/elixir.db")
conn.row_factory = sqlite3.Row

def flatten(obj, prefix=""):
    """Yield (path, json_type) pairs. Lists collapse to '[]'."""
    if obj is None:
        yield prefix, "null"
    elif isinstance(obj, bool):
        yield prefix, "bool"
    elif isinstance(obj, int):
        yield prefix, "int"
    elif isinstance(obj, float):
        yield prefix, "float"
    elif isinstance(obj, str):
        yield prefix, "string"
    elif isinstance(obj, list):
        yield prefix, "array"
        for item in obj[:5]:  # sample first 5 elements
            yield from flatten(item, prefix + "[]")
    elif isinstance(obj, dict):
        yield prefix, "object"
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)

# One payload per entity to avoid over-weighting active players
rows = conn.execute("""
    SELECT entity_key, MAX(fetched_at) AS last_at, payload_json
    FROM raw_api_payloads
    WHERE endpoint = 'player'
    GROUP BY entity_key
""").fetchall()

path_types = defaultdict(lambda: defaultdict(int))  # path -> type -> count
path_entities = defaultdict(set)

for row in rows:
    payload = json.loads(row["payload_json"])
    for path, jtype in flatten(payload):
        path_types[path][jtype] += 1
        path_entities[path].add(row["entity_key"])

# Paths with multiple observed types are type-drift candidates
# Paths seen in <30% of entities are "conditional / optional" candidates
total = len(rows)
for path, types in sorted(path_types.items()):
    entities_seen = len(path_entities[path])
    coverage = entities_seen / total
    if len(types) > 1 or coverage < 0.3:
        print(f"{path:50s} coverage={coverage:.0%} types={dict(types)}")
```

Outputs a list of interesting paths with coverage and observed types. Diff this against `docs/cr-api-docs/players.md`'s field table.

## What to look for

### Doc patch candidates (surface these)

1. **Undocumented fields** — path appears in ≥1 payload but is not in the doc's field table. Sub-categorize:
   - Appears in ~100% of payloads → probably a real field the doc missed. High priority.
   - Appears in <30% → conditional field. Medium priority — doc should at least note its existence and when it appears.
2. **Doc-only fields** — listed in the doc but never observed in any payload. Could be a deprecated field or a field only seen on accounts we don't snapshot (e.g. top global ranks). Note but don't recommend deletion unless clearly dead.
3. **New enum values** — `gameMode.id`, `battle.type`, `deckSelection`, `arena.rawName`, `badge.name`, etc. Compare to the explicit tables in the doc. Any new value deserves a doc row with the first-observed `fetched_at` + the `entity_key` that carried it.
4. **Type drift** — a path has multiple observed JSON types across payloads. Example: `globalRank` observed as both `null` and `int` is *expected* and already documented; but a path observed as both `int` and `string` would be a real mismatch.
5. **Nullability drift** — doc says "absent when X" but payloads show `null` (or vice versa). The `players.md` comments about absent-vs-null are precise and worth verifying.
6. **Cardinality / bounds** — doc says "returns ~30-40 battles", verify. Doc says `currentDeck` has 8 cards, verify.
7. **Nested shape drift** — shapes of commonly-nested objects (`Arena`, `PlayerClan`, `PlayerBattleData`, `ClanWarLogEntry`) — if a new field appears on a nested object it can hide in aggregate counts.

### Known noise (suppress by default)

- Fields documented as conditional (e.g. battle-type-specific) matching their conditional coverage — don't re-flag.
- Fields described as "opaque identifiers" (e.g. `progress` keys) — do not enumerate values.
- Card-catalog specific fields on `cards[]` entries — these are noisy across patches; only flag if a *new kind* of field appears.
- Cosmetic drift like `iconUrls.medium` appearing alongside `iconUrls.large` — mention once, don't enumerate per-badge.

## Output format

```
## CR API Doc Audit — <window>

**Summary:** <1 sentence — "3 doc patches worth making, 1 new game mode ID, no type drift">

**Coverage:** player=<n> payloads / <k> entities; battlelog=<n>/<k>; war_log=<n>/<k>

### Doc patches to apply

1. **<doc file>:<nearest heading>** — <what to add/change>
   - Evidence: <field path, coverage, or enum value>
   - Example payload: `endpoint=<e> entity_key=<tag> fetched_at=<ts>` (payload_id=<id>)
   - Proposed text: <one-to-two-sentence patch, in the doc's voice>

2. ...

### Observations worth noting (no patch yet)

- <e.g., "`leagueStatistics.bestSeason.rank` appears ~20% of the time — doc already says 'optional `rank`' so no change, just confirming">

### Endpoints out of scope this run

- <e.g., "clan / currentriverrace / riverracelog not persisted; would need live `cr_api.py` calls to audit">
```

Keep the report tight — under ~40 lines when there's little drift. If nothing needs patching, say so in one sentence and stop.

## Doc-patch style

Match the existing doc voice — terse, table-driven, specific. Look at `docs/cr-api-docs/players.md` as the reference style:

- Field tables use `| Field | Type | Notes |` format.
- Enum tables show `| ID | Name |` or `| type | Description | Game Modes |`.
- "Agent Notes" section at the bottom carries longer-form observations, dates ("Additional battle variants observed in March 2026 sampling"), and caveats.

Good proposed patches look like:
- "Add row to **Known game mode IDs** table: `72000009 | Normal Battle` — observed 2026-04-18 on battlelog for #UY98QVVQP."
- "Revise **leagueStatistics** section: `currentSeason` can include `id` when carrying over from previous — observed on 3 of 47 players sampled 2026-04-20."
- "Append to **Agent Notes**: `currentDeckSupportCards` is always present as an array, empty when the player has no Tower Troop equipped — observed on 12 of 47 payloads 2026-04-20."

Bad proposed patches look like:
- "Consider updating the field table." (no specific change)
- "The API might return additional fields." (no evidence)

## When to act vs. just report

Read-only analysis by default. The skill produces a report and stops.

Only edit `docs/cr-api-docs/*.md` when the user explicitly asks. When they do, apply only the patches they approved from the report — do not batch in cosmetic rewrites.

## Arguments

Optional natural-language scope ("just battlelog", "since 2026-04-01", "one payload per player", "focus on enum drift"). If none given, audit all three persisted endpoints with default one-per-entity sampling and the full retained window.

$ARGUMENTS
