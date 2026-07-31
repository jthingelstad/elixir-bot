# Battle Intelligence — Feature 4: Player strength (battle-bound only)

Status: **re-scoped 2026-07-30 — profile fetch DELETED (not deferred)**. Part of
[Battle Intelligence v1](battle-intelligence.md). Depends on Feature 1; adds no
ingestion, no fetch, no new table.

> **Decision (Jamie, 2026-07-30): any concept of player strength is bound ONLY to
> data already in the battle.** No opponent profile fetch, no
> `player_strength_snapshot` table, no added member columns. The earlier design
> (fetch each opponent's `player` payload for king level / collection level /
> games / years, plus new member ingestion) is **deleted**.

## Why deleted (pressure test, 2026-07-30)

Verified against the live DB (12,436 1v1 battles):

- **Timing is unfixable.** A profile fetched now is *current* strength — for a
  battle weeks or months old it is simply wrong, because the opponent has since
  levelled, gained collection levels, and played hundreds of games. Stamping it
  "as-of fetch date" labels wrong data, it doesn't make it answer "how strong
  were they *in that battle*."
- **Ranked normalizes levels.** Path of Legends (`is_ranked`, 16% of battles)
  plays every card at level 11, so account strength is battle-irrelevant there.
  The old scope fetched "war + ranked" first — half-aimed at the mode where the
  signal means nothing.
- **98.8% of opponents are one-off** (11,705/11,850; even war opponents are
  95.6% one-off), so the fetch is single-use machinery for opponents never faced
  again.
- **The battle already carries the only free, time-correct strength signal**
  (below), so the fetch is largely redundant as well as misleading.

## What strength IS now — only what the battle record carries (all Feature 1)

- **`level_gap`** — avg member card levels − avg opponent card levels, from the
  two decks' `deck_json`. The on-field level difference at the *exact* battle
  moment. **$0, time-correct by construction.**
- **`hp_margin` / `closeness`** — how decisive the result was (margin of
  victory/defeat).
- The raw per-card levels and tower HP already stored on `battle_events`.

There are **no** account axes (king level, collection level, years played, games
won) — each needs a profile the battle does not contain, so each is out.

## The one required guard: suppress level signals in normalized modes

`deck_json` stores **account** card levels even for ranked, but the ranked battle
was played at a capped level. So `level_gap` is meaningful only where levels are
real — **war and ladder**. In **ranked** (and any level-capped special event),
report `level_gap = NULL` with reason `levels_normalized`, never a fabricated
advantage. This is a small **Feature 1 correction** and lands with `level_gap`
(see [`battle-intelligence-1-data.md`](battle-intelligence-1-data.md) §2).

## Reads (no fetch, ever)

- **A member's strength**: their own battle-observed card levels + the
  `level_gap`s they have actually faced. Aggregated from battles only.
- **Member ↔ opponent**: only a `level_gap` observed in a real battle between
  them. If they never played, the answer is "not observed" — never a fetch.

The `battle` view (Features 1/3) already surfaces `level_gap` and `hp_margin`;
that *is* the opponent-strength context, sharpening Feature 3's `loss_nature`
(a loss into a higher `level_gap` in war/ladder reads as expected) with no new
data and no LLM.

**Not a promotion signal** (memory `elixir-elder-participation`): strength is
battle context, never a ranking lever. Elder/leadership is participation-based.

## Out of scope — deleted, not deferred

- Opponent `player` profile fetch; `player_strength_snapshot` table; per-run
  cap; TTL cache; as-of stamping; the `_apply_v30` migration.
- New member columns (`cr_battle_count`, collection-level coverage backfill).
- "Compare two arbitrary players who never met" — strength that is not in a
  battle is not knowable here, and we do not go fetch it.
