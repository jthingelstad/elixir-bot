# Elixir v5.1 — Ranked Seasons & Playstyle Profiles

> **Status:** 🟡 Spec'd 2026-07-04 (grounded exploration same day) — build not
> yet authorized. Decisions D1–D6 below need Jamie.
> **Owner:** Jamie · **Last worked:** 2026-07-04
>
> **The problem (Jamie):** "Ranked is super important activity that we are
> completely ignoring right now… track ranked seasons with the same closed-in
> season tracker that we use for war seasons." And: "a more robust player
> profile informed by the battle activity" — the battle stream shows 2v2,
> Path of Legend, events, friendlies; Elixir sees only Trophy Road and war.

## 1. Ground truth (explored 2026-07-04, live data)

### 1.1 What the API gives us for Path of Legend / Ranked

- Player profiles carry three season-result objects —
  `currentPathOfLegendSeasonResult` / `last…` / `best…`, each
  `{leagueNumber, trophies, rank}`. Observed live: OllieTurtle current
  league 7 @ 1867, last-season 7 @ 1619, best 10 @ 1896; Fullboat current 4,
  best **10 @ 1596** (a former Ultimate Champion we never celebrate).
- Semantics (docs/cr-api-docs/game-modes.md): leagues advance in steps 1–10;
  league 10 (Ultimate Champion) uses an open rating (`eloRating` on ranking
  endpoints). `trophies` = ranked rating; `rank` = global leaderboard rank
  (null unless charted). `best…` is a **season-end** snapshot (observed:
  Atternam's current 1982 exceeds his best 1958 — best updates at close).
- **No season id anywhere in profiles or battles.** Canonical season ids are
  `YYYY-MM` from `GET /locations/global/seasons`; per-season top-1000 via
  `/locations/global/pathoflegend/{seasonId}/rankings/players`. Both already
  wrapped in `cr_api.py` (`get_pathoflegend_season_rankings` — unused).
- A rollover is **observed**, war-style: `current` league/rating drop-reset
  while `last` swaps to the just-ended values. §16.1's discovered-lifecycle
  philosophy applies cleanly.
- Ranked battles in the log: `type=pathOfLegend`, `leagueNumber` per battle
  (already a `battle_events` column), rating delta in `trophy_change` (±30
  observed). 195 ranked battles / 9 players in the first 3 days.

### 1.2 What v5.1 already has (more than expected)

- `battle_events.mode_group='ranked'` + `league_number` per battle; ingest
  classifies all modes.
- The player `ranked` aspect projects **current only** `{league, rank,
  trophies}`; `pol_promotion` / `ultimate_champion_reached` /
  `pol_global_rank_attained` events implemented (0 fired yet — no crossings
  observed in 3 days); `ranked_pulse` fully implemented at accrual scoring
  (30/15 — deliberately never posts alone).
- **`player_daily_battle_rollups` already aggregates per (player, Chicago
  day, mode_group, game_mode_id): battles/wins/losses.** Playstyle profiles
  are a *read* over an existing table, not new pipeline.
- 2v2 teammates: the raw battlelog `team` array carries both members —
  **ingest keeps only `team[0]`** (the subject), so duo data survives only in
  the 14-day raw buffer today.

### 1.3 Two incidental findings (flagged for the parent, not this spec)

- `refresh_management_inputs`'s `battle_days` uses `datetime(?, '-28 days')`
  — the **param-form** of the space-vs-T comparison bug (the 07-04 sweep
  fixed only `datetime('now'…)` sites). Effect: `battle_days_last_28` counts
  all-time battle days. Same class may exist at other `datetime(?` sites.
- `battle_days` counts **all modes** (no filter) — so a PoL-only player is
  already `battle_active`; management does NOT have a trophy-road blind spot.

## 2. Design

### 2.1 PoL season tracker (mirrors the war tracker, §16.1)

Two tables, lifecycle discovered from observation:

```sql
CREATE TABLE pol_seasons (
    pol_season_id TEXT PRIMARY KEY,     -- canonical 'YYYY-MM' (D1)
    started_at TEXT, ended_at TEXT,     -- observed bounds
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pol_season_results (       -- one row per player per season
    pol_season_id TEXT NOT NULL,
    player_tag TEXT NOT NULL,
    league INTEGER, rating INTEGER, global_rank INTEGER,
    battles INTEGER, wins INTEGER,      -- from rollups over the season window
    observed_at TEXT NOT NULL,
    PRIMARY KEY (pol_season_id, player_tag)
);
```

- The `ranked` aspect projection extends to carry `last` + `best` (D6). The
  emitter watches for the rollover signature (current reset + last swap) →
  emits `pol_season_closed:{tag}:{season}` per player *once*, and the first
  such observation births/closes the `pol_seasons` rows (same
  first-observation-wins pattern as `war_day_opened`).
- Season id: derived locally as the `YYYY-MM` of the close month (D1);
  optionally verified against `/locations/global/seasons` (one call a month,
  already wrapped).
- At close, `pol_season_results` fills from each player's `last…` values +
  season-window rollup sums. Idempotent (PK).

### 2.2 Ranked recognition (volume principle applies)

- **Keep:** `pol_promotion` (65) and `ultimate_champion_reached` (95) event
  scoring as built; `ranked_pulse` stays accrual-only.
- **Add:** ONE `clan:pol_season_closed` summary intent per season close — the
  clan's PoL podium (top-3 by league then rating from `pol_season_results`),
  routed to clan-events like `season_awards`; ledger key
  `pol_season:{season}`. No per-player season posts; the podium names them.
- **Add (awards):** `pol_champ` rows (ranks 1–3) in the awards table at PoL
  season close — same `INSERT OR IGNORE` idempotency as war awards.
- NOT in the clan-chat relay allowlist initially (Discord-first; revisit).

### 2.3 Playstyle profiles (a read, not a pipeline)

`player_mode_profile(conn, tag, days=28)` over `player_daily_battle_rollups`:

- **Shape:** per-mode battles/wins/share over the trailing window + trend vs
  prior window; 2v2 duo partners (D3) with games-together counts.
- **Identity labels, deterministic** (D4): primary = largest mode share
  meeting (≥35% share AND ≥12 battles/28d); secondary listed. Vocabulary:
  `ladder regular`, `war-first`, `PoL grinder`, `2v2 duoist`, `event
  explorer`, `all-rounder` (no mode ≥35%), `quiet` (<8 battles). Pure code —
  the same label for the same numbers, testable, Editor-compatible.
- **Feeds:** (a) compose context — grounded personality facts for welcomes/
  milestones ("EddiePlayz has played 31 of his last 40 battles in 2v2" — real
  specifics, which the Editor's grounding gate will *verify* rather than
  block); (b) the weekly recap (mode-mix color); (c) `/members` Observatory
  (identity chip per member + a `/profile/{tag}` view); (d) management reads
  unchanged (§1.3: already mode-blind in the right way).

### 2.4 2v2 duos (D3)

Add `teammate_tag TEXT` to `battle_events` (additive migration); ingest
writes `team[1].tag` when present; backfill from the 14-day raw buffer at
build. Duo pairs = symmetric aggregation over `mode_group='two_v_two'`.
Recognition: at most a weekly-recap mention initially ("duo of the week" —
no new intent type until we see real volume).

## 3. Decisions — D1–D6

| # | Question | Recommendation |
|---|---|---|
| D1 | PoL season identity: derive locally vs poll `/seasons` | **Derive locally** (`YYYY-MM` of observed close), verify opportunistically via the already-wrapped endpoint; no new scheduled polling |
| D2 | Profile storage: computed read vs projection table | **Computed read** over existing rollups; cache only if reads get hot |
| D3 | `teammate_tag` column + 14-day backfill | **Yes** — additive, cheap, unlocks duos; without it the data evaporates every 14 days |
| D4 | Identity labels: deterministic thresholds vs LLM-derived | **Deterministic** — testable, stable, and the Editor can trust them as facts |
| D5 | Season-close posting: per-player vs one podium summary | **One podium summary** + `pol_champ` awards rows (volume principle; matches war ceremony shape) |
| D6 | Extend `ranked` aspect with `last`/`best` | **Yes** — required for reliable rollover detection; guard the first post-deploy diff (new fields vs old baselines must not emit) |

## 4. Build plan (after D1–D6)

1. Schema: `pol_seasons`, `pol_season_results`, `battle_events.teammate_tag`
   (+ `schema_v51.py` count), raw-buffer teammate backfill script.
2. Emitter: `ranked` aspect extension + rollover detection +
   `pol_season_closed` emission (first-diff guard per D6).
3. Season close consumer: results fill + `pol_champ` awards + the podium
   summary intent + deterministic fallback copy.
4. `engine/profiles.py`: `player_mode_profile` + identity labels + duo pairs;
   compose-context and weekly-recap integration; Observatory `/members` chip
   + profile view.
5. Tests: fixture-driven rollover sequence (simulate a PoL season close in
   `scripts/simulate.py` — the time-travel sim was built for exactly this),
   label golden cases at the thresholds, duo symmetry, idempotent close.
6. The two §1.3 findings fixed alongside (param-form timestamp sweep;
   they're one-line fixes but belong to the parent session's bug ledger).

**Not in scope:** any new polling (profiles/battlelogs already flow — the
seasons endpoint is opportunistic verification only); building before D1–D6
are ratified.
