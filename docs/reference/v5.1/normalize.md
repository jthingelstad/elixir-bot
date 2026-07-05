# Elixir v5.1 — CR Normalizer Layer (addendum)

> **Status:** ✅ **Built 2026-07-04** (`engine/normalize.py` + migration sweep +
> tool annotation + `tests/test_engine_normalize.py` incl. grep gates).
> Companion to the v5.1 set; same conventions (detail wins, ground everything).
> Build additions the gates caught: `card_display_max_level` (a 4th card-math
> site in storage/player.py the inventory missed) and list-form battlelog
> handling in `annotate`.
> **Owner:** Jamie · **Last worked:** 2026-07-04
>
> **The problem (Jamie, go-live night):** CR API representational quirks leak
> into Elixir everywhere — 0-based war days rendered as "day 2" when it's day 3,
> odd card levels, nine copies of the timestamp parser. v5.1 isolated *calling*
> the API (one client, one raw log) but never centralized *interpreting* it.

## 1. Design principles

1. **One module owns the quirk catalog:** `engine/normalize.py` — pure
   functions, each rule citing `docs/cr-api-docs/` or the live incident that
   taught it. `cr_knowledge.py` keeps prose game knowledge; normalize.py owns
   the math.
2. **Normalize at the projection boundary.** L1 (`raw_api_payloads`) stays
   byte-true — the analyst's record. Everything from L2 up (baselines, events,
   projections, payloads) speaks engine language. The existing projection
   functions (`engine/ingest.extract_battles`,
   `engine/emitters/*.project_*_aspects`) are the seam — this consolidates
   them, it does not add a pipeline stage.
3. **Annotate, don't mutate, at the tool boundary.** The direct `cr_api` tool
   keeps returning true API responses, with derived fields attached alongside
   raw ones (`display_level` next to `level`, `day_human` next to
   `periodIndex`). Elixir sees both; nothing is hidden or rewritten.

## 2. The quirk catalog (grounded 2026-07-04)

| Quirk | Truth | Today's scatter | Normalizer function |
|---|---|---|---|
| War days are 0-based | `periodIndex % 7`: 0–2 training, 3–6 battle; war_day_index 0–3 but humans say "day 1–4" | 5 sites do the arithmetic; `war_day_human` was patched into the *recognizer* (wrong altitude) after live copy said "day 2" on day 3 | `war_day(period_index) -> {index, war_day_index, human: "battle day 3 of 4", phase}` |
| Card levels are rarity-relative | API `level` is within-rarity (1..maxLevel); display level = `level + 16 − maxLevel` | `engine/emitters/player.py:38–41`, `storage/cards.py`, `storage/card_catalog.py` each do their own math | `card_display_level(level, max_level) -> int` |
| CR-compact timestamps | `20260703T211500.000Z`; suffixless strings are UTC by engine convention | **9 files** carry a parser (`engine/clock.py`, `engine/recognition/scorer.py`, `engine/management.py`, `storage/war_calendar.py`, `storage/war_analytics.py`, `storage/tournament.py`, `storage/opponent_intel.py`, `db/__init__.py`, `runtime/webapp/render.py`); three had naive/aware tz bugs fixed live | `parse_cr_time(value) -> datetime` (the one parser) |
| War reset hour drifts per season | nominal 10:00Z, observed ~09:37Z s133; skew changes seasonally (pre-v5.1 issue #20) | restored 2026-07-04 in `engine/clock.py` (`period_anchor_from_events`) | stays in clock.py; normalize.py re-exports the anchor helper so callers find it |
| Seasonal vs canonical arenas | arena ids ≤ 54000016 are stable Trophy Road; above churn monthly ("PANCAKES!") | `ARENA_UP_MAX_CANONICAL_ID` lives in the recognizer after 7 false arena-up posts | `arena_kind(arena_id) -> 'road' \| 'seasonal'` |
| `seasonId` absent from live race payloads | absent in ALL 259 archived payloads; always inferred | `engine/clock.py:infer_season_id` (correct home; catalog it) | cataloged, stays in clock |
| PoL ranks are lower-is-better | rank 1 beats rank 100 | player emitter + read layer each know it implicitly | `pol_rank_improved(old, new) -> bool` |
| Tag canonicalization | `#`-prefixed, uppercase, O→0 | `db._canon_tag` (correct home; catalog it) | re-export as `canon_tag` |
| CR-compact timestamps leak into OUR OWN tables | migration/seed paths copied '20260628T…' into columns where live code writes ISO-Z — compact sorts ABOVE ISO, mis-bucketing time-ordered queries | recognition_ledger.claimed_at T14 seeds (found 2026-07-04 live audit; one-time fix scripts/migrate_v51/fix_ledger_seed_ts.py) | write-side convention: always `parse_cr_time(...).strftime(ISO)` before persisting |
| SQLite `datetime('now')` is space-separated | stored timestamps are ISO-T; `'T' > ' '` so `>=` cutoffs match EVERYTHING and `<` cutoffs match NOTHING | 12 sites (live incident 2026-07-04: the clan-chat relay's 15-min freshness filter never filtered — R108–R114 spam) — all fixed to `strftime('%Y-%m-%dT%H:%M:%S','now',…)` | SQL convention, catalogued; grep gate candidate |
| Ranked `leagueNumber` spans two epochs | pre-mid-2025 values use the 10-league Path of Legends scale (10=UC); post-rework uses 7 leagues (7=UC) | `best*SeasonResult` fields carry old-scale values forever (live: best=10 beside current max 7) | catalogued 2026-07-04; display maps by era |
| `memberList[].expLevel` is 0 for every member | the clan endpoint's expLevel is dead at the CR API; only PROFILE payloads carry the real level | found by the Q&A battery 2026-07-04 ("average level is 16" — an average over zeros); roster projection now maps 0→None so profile truth survives, one-time backfill from profile baselines | catalogued |
| Colosseum has no finish line | fame accrues all 4 days (live: 20,600 on day 2); spec's 5,000 was wrong | fixed in clock.py 2026-07-04 | cataloged, stays in clock |

## 3. Build plan (executed 2026-07-04)

1. `engine/normalize.py` with the functions above + docstring citations; unit
   tests (golden values per quirk, incl. the live-incident cases).
2. Migrate the scatter: projections/emitters/read-layer call normalize.py;
   delete the local copies (the 9 parsers become imports). `war_day_human`
   moves from the recognizer into the emitter payload.
3. Tool annotation: `agent/tool_exec.py`'s direct-CR-API paths pass responses
   through `normalize.annotate(payload, endpoint)` — attaches derived fields,
   never replaces raw ones. Battle-log and player-profile annotations first
   (cards + war days are the highest-leak quirks).
4. Observatory: `/streams` raw-payload inspector shows annotated view beside
   raw (the teaching surface for future quirk discoveries).
5. Doc: each new quirk discovered in the bake gets a catalog row — the
   catalog is the living registry.

## 4. Verification

- Unit: golden tests per quirk (display level for each rarity's maxLevel; day
  humanization across the period range; timestamp forms incl. suffixless).
- Grep gates: no `strptime.*%Y%m%dT` outside normalize.py; no `16 -` card
  math outside it; no `+ 1` day arithmetic outside it.
- Live: one war week's copy shows correct day labels/hours; card posts show
  display levels matching the game UI.
