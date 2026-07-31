# Battle Intelligence — Feature 4: Player-strength comparison

Status: **draft** (2026-07-30). Part of [Battle Intelligence
v1](battle-intelligence.md). **Independent** of Features 1–3 (complements them —
account strength is context for a battle/deck read). **$0 LLM**; modest CR API
quota for opponent fetches (§2).

## Goal

Compare any two players — member↔member or member↔opponent — on the four axes
Jamie named, **all sourced from one `player` payload**:

| axis | payload field | member (have) | opponent (fetch) |
|---|---|---|---|
| **king tower level** | `expLevel` | `player_current_state.exp_level` | fetch |
| **collection level** | badges → collection-level badge | `player_metadata.cr_collection_level` | fetch |
| **games won** (+ total for win-rate) | `wins` / `battleCount` | `cr_battle_wins` (+ add `cr_battle_count`) | fetch |
| **years played** | badges → `YearsPlayed` | `cr_account_age_years` | fetch |

One payload, four axes, identical parsing for both sides. This retires the
earlier proxy fumbling entirely — we read the real `expLevel`, **not**
`king_tower_hp` (which is *remaining* tower HP at battle end, a Feature-1
margin-of-defeat input, not a level).

**Progression nuance** (memory `cr-progression-model-2026`): `expLevel` is the
King-tower level and is being deprecated in favour of Collection Level as the
headline number. Present **both** as distinct axes; lead with collection level;
never conflate them.

## 1. Member side — mostly already ingested

- `cr_collection_level`, `cr_battle_wins`, `cr_account_age_years`,
  `player_current_state.exp_level` are all captured. **Two fixes**:
  - **Add `cr_battle_count`** (`battleCount`) so "games played" and true win-rate
    (`wins/battleCount`) are exact — today we store only `wins`.
  - **Close coverage**: `cr_collection_level` is populated for only **66/132**
    members. Ensure the profile/badge refresh (`storage/metadata.py` write path)
    enrols all active roster members.

## 2. Opponent side — fetch the profile (Jamie's decision)

The proxies in the battle log are too weak (card levels help; `king_tower_hp` is
HP not level). **The real signal is the opponent's actual `player` profile**, one
API call each — and the quota affords it.

**Quota sizing** (measured 2026-07-30): ~**860 distinct opponents/day** (1v1),
~98% one-off (11,691 of ~11,836 opponent tags seen once — so caching helps only
war rivals). At ~860/day spread out that's **~0.01 req/s** — negligible against
the CR API's per-second rate limit, and we already fetch ~60 member profiles a
cycle. Guardrails: a **per-run fetch cap** (safety valve) and a **short TTL**
(don't re-fetch a tag seen within N days).

**Timing matters — fetch near battle time, enrich ongoing.** A profile fetched
*now* is the opponent's *current* strength. For a fresh battle that's accurate;
for an old one the opponent has since levelled, so **backfill is approximate**.
So: opponent enrichment runs **ongoing** as battles arrive (accurate); historical
backfill is either skipped or stamped "as-of fetch date", never presented as
battle-time truth.

**Scope lever**: start with **competitive** battles (war + ranked — the ones
where opponent strength actually informs a read) and widen to all 1v1 if quota
stays comfortable. Ladder/special-event opponent strength is low-value.

## 3. Schema — `_apply_v30`

```sql
-- member: exact games-played / win-rate
ALTER TABLE player_metadata ADD COLUMN cr_battle_count INTEGER;
ALTER TABLE player_metadata ADD COLUMN cr_battle_count_updated_at TEXT;

-- opponent (and any non-member) strength snapshots, time-stamped for as-of reads
CREATE TABLE player_strength_snapshot (
    player_tag       TEXT NOT NULL,
    observed_at      TEXT NOT NULL,      -- fetch time ≈ battle time for ongoing
    king_level       INTEGER,            -- expLevel
    collection_level INTEGER,            -- from badges
    wins             INTEGER,
    battle_count     INTEGER,
    years_played     INTEGER,            -- YearsPlayed badge
    trophies         INTEGER,
    source           TEXT,               -- 'opponent_fetch' | 'member_refresh'
    PRIMARY KEY (player_tag, observed_at)
);
CREATE INDEX idx_pss_tag_time ON player_strength_snapshot(player_tag, observed_at);
```

- **`EXPECTED_TABLE_COUNT` 63 → 64** (one new table); update `REQUIRED_SCHEMA`;
  bump the `db/schema.py` hygiene baseline. (`_apply_vN` number slots into the
  live version at ship time; this assumes 1→2→3 shipped first.)
- Member axes stay in `player_metadata` (no churn); the snapshot table holds
  opponents. The `compare` view unions the two through one shared parser.
- Raw payloads already land in `raw_api_payloads` (endpoint `player`) — the
  snapshot table is the extracted, queryable projection, not a second copy.

## 4. Fetch job — `runtime/jobs/_battle_intel.py` (opponent strength step)

Cursor-driven, alongside the other Stage-A/B steps. **Ongoing:**

1. For new battles in scope (competitive first), collect opponent tags not
   snapshotted within the TTL.
2. Fetch each via the existing `player` endpoint path (respect the per-run cap);
   store the raw payload (as today) and extract the four axes → one
   `player_strength_snapshot` row (`source='opponent_fetch'`).
3. Shared parser turns a `player` payload → the four axes; the **member refresh
   reuses the same parser** so members and opponents are computed identically.
- **Telemetry**: `mark_job_start/success/failure` with counts (`"opp fetched +N,
  cache-hit skipped +C, cap-deferred +D"`) — never a bare success. Deferred-by-cap
  is visible, so a growing backlog is obvious.

## 5. Tool — `get_battle_intelligence`, add strength views (no new wiring)

Add views to the existing tool (no `_SHARED_TOOL_NAMES` change):

- **`member_strength`** (one member): the four axes + win-rate + games/day, each
  with its freshness (`*_updated_at`); missing/stale fields reported as such.
- **`compare`** (two players, member↔member **or** member↔opponent): the four
  axes side-by-side with deltas. For an opponent, use the snapshot nearest the
  relevant battle and **label its as-of date**. **Honesty floor**: an axis we
  don't have returns "not captured" — never a guess, never a zero.

The `battle` view (Features 1/3) gains real opponent context: the opponent's
snapshot (king level, collection level) at battle time, so a loss to a stronger
account reads as expected rather than a piloting failure — sharpening Feature 3's
`loss_nature` with **no LLM change**.

**Not a promotion signal**: Elder/leadership is participation-based (memory
`elixir-elder-participation`); strength is context, not a ranking lever. The
views say so, so the brain doesn't turn "stronger account" into "deserves
promotion".

## 6. Cost

**$0 LLM.** CR API: ~860 opponent fetches/day at full 1v1 scope (fewer if
competitive-only), ~0.01 req/s — comfortably within rate limits, capped per run,
TTL-cached. Member side adds no calls (rides the existing fetch).

## 7. Build order (each = one commit, suite green)

1. `_apply_v30` (`cr_battle_count` + `player_strength_snapshot`) + schema
   bookkeeping. Copy-validate.
2. Shared `player`-payload → four-axis parser; wire the **member refresh** to it
   and add `cr_battle_count`; fix collection-level coverage.
3. Opponent fetch step (competitive scope, per-run cap, TTL) + telemetry.
4. `member_strength` + `compare` views + honesty floors; opponent context in the
   `battle` view.
5. **Restart** (v30 migration) — back up, ask first.

## 8. Verification (validate data, not just errors)

1. **Values match the game**: spot-check 3 members and 3 recent opponents' four
   axes against their in-game profiles.
2. **Member coverage**: after the fix, `cr_collection_level` + `cr_battle_count`
   populated for all active members (not 66/132).
3. **As-of honesty**: an opponent comparison shows the snapshot date; a stale or
   missing axis reads "not captured", never a guess.
4. **Quota discipline**: fetch count/day matches scope; per-run cap holds; TTL
   suppresses re-fetch of a repeated war rival within the window.
5. **No `king_tower_hp`-as-level regression**: grep the strength code — king
   level comes from `expLevel`/the snapshot, never from a battle's tower HP.

## 9. Risks

| risk | guard |
|---|---|
| opponent fetch quota runs away | per-run cap + TTL cache + competitive-first scope; measured ~0.01 req/s |
| backfilled opponent strength is stale | fetch ongoing near battle time; historical stamped as-of, never battle-time truth |
| `expLevel` vs collection level confusion | present both as distinct axes; lead with collection level (memory `cr-progression-model-2026`) |
| `king_tower_hp` mistaken for level | king level comes only from `expLevel`/snapshot; HP stays a Feature-1 margin input; verification §8.5 |
| half-populated member collection level | coverage fix + verification §2; `compare` reports "not captured" honestly |
| strength misread as a promotion signal | views state strength is context, not a ranking lever (Elder = participation) |
| silent fetch-job death | work-set + cap-deferred counts in telemetry |
