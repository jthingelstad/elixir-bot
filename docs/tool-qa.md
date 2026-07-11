<!-- Generated 2026-07-11 by the awareness-tool-qa multi-agent workflow (run wf_e078fd48-896). Findings register — inventory + QA of the awareness brain tool surface. -->

# QA Audit — Elixir-Bot Awareness Brain Tool Surface (16 tools)

**Audit date:** 2026-07-11 · **Method:** 16-agent value-correctness sweep (multi-agent workflow) · **Status:** ✅ **all 88 findings resolved** (25 HIGH · 33 MEDIUM · 30 LOW) as of 2026-07-11 — see commit history for the per-finding fixes. A handful were resolved transitively (e.g. L13 by the H12/M12 battle_events rewrite; L11 already echoed maxed_only) and are noted in their commit.

## 1. Executive Summary

The tool surface **runs** — no tool crashes on load, edge-case/error handling is broadly solid, and identity resolution + name normalization are mostly injection-safe on the primary name field. But underneath the clean envelopes, the brain is being fed a recurring set of **trust-eroding defects that surface member-facing numbers which look authoritative and are materially wrong**. Two tools have an outright broken aspect (`get_awards`'s leaderboard crashes; `get_elixir_state`'s war_season returns empty standings via stale keys). Across the other 14, five themes dominate:

**Theme 1 — Lossy/stale `player_daily_battle_rollups` read instead of authoritative `battle_events` (single biggest root cause).** At least four tools source battle volume/W-L from the derived rollup store, which is both lossy for new/backfilled members and **stale (data ends 2026-07-03 while today is 2026-07-11)**. Consequences: `get_member` trend shows Andy's real ~21-10 week as a rosy 9-1; `get_clan_roster` trends reports the previous week as **0 battles / 0-0-0** (a false dead-clan signal) when `battle_events` holds 3323 battles in the real last 7 days; `get_clan_game_modes` returns two contradictory ranked counts in one payload (1152 vs 451); `get_member` playstyle disagrees 719 vs 247. **One root fix** (source windowed battle stats from `battle_events` + surface coverage) heals all four.

**Theme 2 — Mid-week finalization blindness (`war_weeks.our_fame` is NULL until week close).** `get_war_season` summary and season_comparison report **0 clan fame / a -1966.7 fame collapse** for the live season while members visibly have thousands; `get_elixir_state` season_window returns null rank/fame for the current week; `get_war_season` perfect_attendance counts the in-progress day and hides ~15 genuinely-perfect members while crediting only 1. The live authoritative figure (`war_participation.fame` / API `clan.fame`=6870) exists but isn't used for the in-progress week.

**Theme 3 — Unit conflation & mislabeling.** `get_clan_health` trophy_drops reports **MAX−MIN spread and labels trophy GAINS as "drops"** (every live result was a climbing member); it also reads Trophy Road trophies, missing Path-of-Legends losses where real competitive drops live. `get_clan_intel_report` emits a `total_fame` that reconciles with neither the clan fame standing nor reality (over- and under-counts in different clans) and a `clan_score` key that means two different things in one report. Deprecated `exp_level` (2026 model uses Collection Level) is surfaced as 0/near-random across `resolve_member`, `get_member`, `get_clan_roster`, `cr_api`.

**Theme 4 — Raw un-normalized (injection) names bypass the `display_name` guarantee.** The primary name field is scrubbed, but raw `current_name` still reaches the LLM via `resolve_member` aliases, `get_member` battles/playstyle/duos, `get_member` profile's nested membership_summary, and **5 of 6 `get_member_war_detail` aspects** (`<c0>亗ZØMB亗`, `²⁸`, fullwidth glyphs). External-clan names in `cr_api`/`get_clan_intel_report` are unsanitized by design (no display_name for non-members).

**Theme 5 — Hidden window coverage & missing freshness.** Pervasive: tools return `window_days` but never `days_found` / `battles_found` / completeness, and drop `observed_at` even when the source computes it (`get_river_race`, `get_elixir_state` war_season, streak rows, card snapshots). A thin/stale window is indistinguishable from a complete one. Departed members are returned as active with no `left_at` flag across war detail, card profile, streaks, and write tools — ghosts get reported as war no-shows and (via write tools) can have leader-closed cases silently reopened.

**Write-side (bonus theme):** three of four write tools are non-idempotent at the memory layer, two can **silently reopen kick/promo/demote cases a leader deliberately closed** (27 resolved/dismissed cases live now), and `schedule_revisit` has no lifecycle closer so due revisits nag the read forever.

---

## 2. Tool Inventory

| Tool | Aspects | Primary data source(s) | Health |
|---|---|---|---|
| `resolve_member` | (single: name/tag/alias/discord match) | players / player_aliases / discord_* + preferred_display_name | issues |
| `get_member` | profile, form, playstyle, trend, losses, battles | mix: state_baselines/player_current_state (good) + **player_daily_battle_rollups** (trend/playstyle) + battle_events | issues |
| `get_member_war_detail` | summary, attendance, battles, missed_days, vs_clan_avg, war_decks | war_participation, war_weeks, war_attendance_days, **battle_events** (battles/war_decks) | issues |
| `get_river_race` | standings, engagement | state_baselines(riverrace) live projection + war_attendance_days | issues |
| `get_war_season` | summary, standings, win_rates, no_participation, boat_battles, score_trend, season_comparison, trending, perfect_attendance | **war_weeks.our_fame** (unfinalized), war_participation, battle_events, clan_daily_metrics, war_attendance_days | issues |
| `get_clan_roster` | list, summary, recent_joins, longest_tenure, role_changes, max_cards, card_owners, donations, trends | players/player_current_state/clan_events/player_card_collection (good) + **clan_daily_battle_rollups** (trends) | issues |
| `get_clan_health` | at_risk, hot_streaks, losing_streaks, trophy_drops, promotion_candidates | member_management, player_recent_form (battle_events-backed), **player_daily_metrics.trophies** (trophy_drops) | issues |
| `get_clan_game_modes` | summary, ranked, side_modes, events, duos | **player_daily_battle_rollups** (mode_mix) + battle_events (ranked_activity/events/duos) + player_current_state | issues |
| `get_elixir_state` | operational_summary, event_summary, recent_events, game_modes, season_window, war_season, decision_cases, communication_intents, communication_trace | events streams, battle_events, war_status, decision_cases, communication_intents | **broken** (war_season / operational_summary) |
| `lookup_cards` | (card catalog search) | card_catalog (mirror of /cards API) | issues |
| `get_member_card_profile` | king_tower, totals, by_rarity, ready_to_upgrade, levels_to_max, modes | player_card_collection × card_catalog + **cr_knowledge cost table** | **broken** (upgrade layer) |
| `lookup_member_cards` | deck, rarity/name/ready/near/maxed/evo/hero filters, mode=war | player_current_state deck, player_card_collection, battle_events (war) | issues |
| `get_clan_intel_report` | war engagement, roster, threat_rating | **live CR API** (currentriverrace + clan profile) | issues |
| `cr_api` | player, player_battles, player_chests, clan, clan_members, clan_war, clan_war_log, tournament, events, pathoflegend/leaderboards | **live CR API passthrough** (TTL cached) | issues |
| `get_awards` | list, leaderboard, current_standings | awards ledger + live war_participation (current_standings) | **broken** (leaderboard) |
| `WRITE_TOOLS` | save_clan_memory, flag_member_watch, record_leadership_followup, schedule_revisit | memories/memory_tags, decision_cases, revisits | issues |

---

## 3. Findings Register (batch-fix worklist, grouped by severity)

### HIGH severity

| # | Tool · Aspect | Status | Member risk | Evidence (tool vs ground truth) | Fix |
|---|---|---|---|---|---|
| H1 | `get_clan_roster` · trends/battle_activity | stale | yes | Reads `clan_daily_battle_rollups` (MAX(battle_date)=2026-07-03). Output: current={days:6, battles:1690}, **previous={0 battles, 0-0-0}**; trend_summary literally "battles 1690 vs 0". `battle_events` has **3323 battles in the real last 7 days**. False dead-clan narrative can reach member reports. | Source clan battle window from `battle_events`; surface days_found vs window_days, completeness_ratio, max(battle_date) freshness. |
| H2 | `get_member` · trend (7d vs 7d) | misleading | yes | Reads lossy rollups. Andy shows "9-1-0, 10 battles" (previous window days:2) while `battle_events` for the same week shows ~30 games at ~21-10. Per-window `days` coverage is computed but **dropped** from the returned string. | Compute windowed W/L from `battle_events`; surface per-window coverage (days_present, battles_found, completeness). |
| H3 | `get_member` · losses | misleading | no | SQL selects `NULL AS opponent_deck_json/opponent_name`; `battle_events` has no opponent-deck columns at all. `top_opponent_cards` is **always []**, coverage_note falsely implies newer battles have opponent decks. `guidance` still tells the model to use it for swaps. | Drop top_opponent_cards/guidance (or capture opponent decks at ingest); fix the false coverage_note. |
| H4 | `get_member_war_detail` · name field (5 of 6 aspects) | misleading | yes | attendance/battles/missed_days/vs_clan_avg/war_decks return raw `name='<c0>亗ZØMB亗'` / `'²⁸'`; summary correctly returns display_name 'Player J08Q'. | `SELECT COALESCE(p.display_name, p.current_name) AS name` in all five storage fns. |
| H5 | `get_member_war_detail` · attendance last_4_weeks | misleading | yes | Rolling window string-compares `created_date` which has **two formats** ('20260629T…' vs ISO '2026-07-06'). ISO hyphen sorts below digits, so the **two newest weeks are dropped** and older weeks kept. recent_total=3 vs true 4-5; understates recent participation feeding inactivity/kick reads. | Normalize created_date to one sortable format / parse to datetime before compare; assert newest war_week is in-window. |
| H6 | `get_member_war_detail` · departed members (missed_days/attendance/vs_clan_avg) | missing-caveat | yes | lux_alastor #VQPYV9U0J left 2026-07-05; war days are 07-10/11 (after he left) yet missed_days=3, participation_rate=0, fame_vs_average=-492.2. No aspect joins clan_memberships. Departed member reported as active war no-show. | Join clan_memberships, surface left_at/member_status, suppress/annotate attribution after left_at. |
| H7 | `get_war_season` · summary total_clan_fame | misleading | yes | total_clan_fame=0 / fame_per_active_member=0.0 for live season while same payload's top_contributors show pax=2400, Atternam=1800. `war_weeks.our_fame` is NULL until finalization; SUM(war_participation.fame)=23625, API clan.fame=6870. | For in-progress week source clan fame from API clan.fame or SUM(war_participation.fame); add in_progress flag. |
| H8 | `get_war_season` · season_comparison | misleading | yes | Live season returns current.total_fame=0, **fame_per_member_change=-1966.7** (fake collapse); passing finalized season_id=133 works (change -40.9). Same NULL-war_weeks root as H7. | Exclude/annotate in-progress season or compute from war_participation; add current_season_in_progress flag. |
| H9 | `get_war_season` · boat_battles | misleading | no | Returns window_wars:3, boat_battles:91 (52-39) but the query applies **no war/time window** — it's all-time (back to 2026-05-07). "last 3 wars: 52-39" is materially wrong. | Actually scope to N recent wars, or rename to all_time and drop the wars param; surface which seasons/sections included. |
| H10 | `get_war_season` · perfect_attendance | missing-caveat | yes | Live returns ONLY pax; it's Battle Day 3 of 4 with day 2 in progress. Counting the in-progress day excludes ~15 members who were perfect on both completed days. Docstring claims "every finalized battle day" but no finalization filter exists. | Filter to finalized days only; surface battle_days_finalized vs total. |
| H11 | `get_clan_health` · trophy_drops | misleading | yes | Computes MAX−MIN spread, direction-agnostic, labels all as 'drop'. Andy 8700→9004 (+304) reported drop:304; Ellipsis +667 is #1 'drop'. 30 members net-gained vs 9 dropped. Also reads Trophy Road trophies (caps 14000), missing Ranked/Path-of-Legends where real drops live. | Compute directional net (last−first), report only negatives ≤ −min_drop; read ranked_trophies for competitive drops; surface from/to + dates. |
| H12 | `get_clan_game_modes` · ranked self-contradiction | misleading | yes | One response returns two incompatible counts: mode_mix (rollups)=1152 vs ranked_activity (battle_events)=451 (2.5×). trophy_delta 16725 vs 14475. Nothing labels which is authoritative. | Pick one source (battle_events) or explicitly label rollup-estimated vs battle-log-confirmed; add coverage. |
| H13 | `get_elixir_state` · war_season (stale keys) | misleading / broken | yes | Reads `current.get('standings')`→[], `'our_fame'`→null, `'day_number'`→null, but the source dict actually has `race_standings`, `fame`=6870, `battle_day_number`=3. Structured race block is **empty** while summary string says "rank 1" — internal contradiction. period_points/primary_metric/boat_scored never exposed. | Fix key mapping: standings→race_standings, our_fame→fame, day_number→battle_day_number; add period_points/primary_metric/boat_scored/projected_day_fame/colosseum. |
| H14 | `get_elixir_state` · operational_summary | misleading | yes | Embeds get_war_season_snapshot verbatim, inheriting H13 — the single most decision-relevant live block (river race) is empty. | Heals automatically once H13 is fixed. |
| H15 | `get_member_card_profile` · ready_to_upgrade / cards_required | misleading | yes | `_ready_required` passes the **display level** into a **rarity-relative** cost table. Hog Rider raw=11 → reports 2000 (true 1500); Bowler/Princess emit no requirement at all (index past table). 89/118 of bonus's cards wrong; **492 truly-ready cards hidden clan-wide**. | Pass `card.get('api_level')` (already stored) into cards_required_to_upgrade; fixes ready lists, by_rarity.ready, progress, filters in one place. |
| H16 | `get_member_card_profile` · by_rarity.ready aggregate | misleading | yes | Same display-level path → non-common rarities almost never register 'ready' (all rarities ready=0 for bonus despite cards past true thresholds). | Same root fix as H15. |
| H17 | `get_clan_intel_report` · total_fame | misleading | no | Emits total_fame = sum(participant.fame) next to authoritative clan fame, unlabeled, on a different basis: R.E.I.C.H clan fame=3600 but total_fame=1050; POAP KINGS clan fame=6870 but total_fame=23625 (3.4× over). Inconsistent both directions → cross-clan ranking by total_fame is invalid. | Drop it or relabel participant_attributed_fame with a defense-fame caveat; surface only clan-level fame as the standing. |
| H18 | `cr_api` · clan_war periodPoints/decksUsedToday dropped | missing-caveat | no | `_filter_cr_clan_war` exposes only clan fame + participant fame/decksUsed; **silently drops periodPoints (800) and decksUsedToday**. On a Colosseum week (period-points-only) this removes the only meaningful metric. Local get_river_race has this; the external bridge doesn't. | Add clan periodPoints + participant decksUsedToday; label fame as week-cumulative vs periodPoints today-only. |
| H19 | `get_awards` · leaderboard | **crashes** | yes | Tool calls `db.award_leaderboard(rank=…, limit=…)` but the storage fn accepts neither. **Every** call raises TypeError → returned as an error string. All-time award-count leaderboard 100% non-functional; no test invokes this mode. | Drop rank/limit at the call site (or add them to the storage fn); add a test that invokes mode='leaderboard'. |
| H20 | `WRITE_TOOLS` · flag_member_watch | misleading | yes | When case_type is supplied it upserts a decision_case keyed `<case_type>:member:<tag>`; ON CONFLICT resets status IN ('resolved','dismissed') back to 'open' and nulls resolution. An awareness tick **silently reopens a kick/promo/demote case the leader closed** (27 such cases live). No departed-member guard; non-idempotent watch rows. | Never reopen leader-closed cases from an awareness write (or route through the sustained-evidence gate); add departed guard; dedup watch memory. |
| H21 | `WRITE_TOOLS` · record_leadership_followup | misleading | yes | Member-review case_types route to the same `<case_type>:member:<tag>` upsert → same leader-closure reset as H20. Generic path keys on a 48-char topic slug → distinct topics collide. Narrative memory INSERTs every call (non-idempotent). | Same closure guard; use a full-topic hash discriminator in case_key; dedup narrative memory. |
| H22 | `WRITE_TOOLS` · schedule_revisit | missing-caveat | no | Write is clean/idempotent, but **no lifecycle closer is wired**: `mark_revisited()` exists but has zero runtime callers, so `_due_revisits` re-surfaces every due row every tick forever (the "dead timer that nags the read" class). No future-time guard; per-exact-due_at dedup lets recomputed times spawn one row/tick per signal. | Wire mark_revisited into the tick (or auto-mark after N surfacings); require due_at in future; dedup per signal_key. |

### MEDIUM severity

| # | Tool · Aspect | Status | Member risk | Evidence | Fix |
|---|---|---|---|---|---|
| M1 | `resolve_member` · departed/observed members | misleading | yes | Hardcodes status='active', so exact-tag lookup of departed 'Jonah the beast' #P82C298G9 → []. `db.resolve_member(status=None)` returns him as 'observed'. Brain can wrongly conclude a recently-departed player doesn't exist. | Forward a status arg / default status=None and rely on the returned status field. |
| M2 | `get_member` · playstyle (28d mode mix) | misleading | yes | Reads rollups: bonus total_battles 719 vs battle_events 247; per-day disagree (07-07: 40 vs 52). No coverage surfaced; duo_partners uses raw current_name (null names). | Reconcile against battle_events or expose rollup coverage; use display_name; flag thin windows. |
| M3 | `get_member` · battles | misleading | yes | Core values correct but member_name is raw current_name (`<c0>亗ZØMB亗`, `²⁸`). opponent fields always NULL. | Return display_name; note opponent decks not captured. |
| M4 | `get_member_war_detail` · battles vs missed_days coherence | misleading | yes | Andy: battles aspect reports 5 war battles (war_day_index=NULL training-day games) while missed_days says days_participated=0/missed=3 for same season. Contradictory, no reconciliation, no battle-day/training-day split. | Exclude/flag training-day (NULL war_day_index) battles; return a breakdown; cross-note attendance source. |
| M5 | `get_member_war_detail` · battles window & coverage | missing-caveat | yes | Correct source (battle_events) but window is the WHOLE SEASON, unlabeled; no weeks_covered/date span. A multi-week season total masquerades as "this week". | Add season span, weeks_covered, first/last battle_time; label scope. |
| M6 | `get_river_race` · points_today / top_points_today | misleading | yes | Every points_today=0 (fame_delta never populated: 0 nonzero across 644 rows). top_points_today falls back to cumulative total → ranks by season total but is labeled "today". | Populate fame_delta or derive per-day from period-point/battle deltas; until then drop/null points_today (member keys are now `points`/`points_today`/`top_points_total`, not fame). |
| M7 | `get_river_race` · freshness (both aspects) | missing-caveat | yes | get_current_war_status computes observed_at (~6 min old) but `_execute_get_river_race` drops it; standings/engagement carry no freshness. | Add observed_at + age note to both return dicts (mirror _war_standings_freshness). |
| M8 | `get_war_season` · win_rates | missing-caveat | yes | Correct source (battle_events) but min_battles=1 + ORDER BY win_rate DESC ranks 1-0 (1.000) above pax 16-4 (0.8). No low-sample flag. | Raise default min_battles (4-8) or add low_sample flag / Wilson sort. |
| M9 | `get_war_season` · trending | missing-caveat | no | Live season has 1 section → all earlier_avg_fame/fame_trend null; degrades to a plain current-fame list with no "trend unavailable" note. Can't cross season boundary. | Emit sections_available/trend_available:false; consider spanning prior season's final races. |
| M10 | `get_clan_roster` · list exp_level | misleading | no | Surfaces deprecated exp_level; Vijay (maxed) shows 0 while new Andy shows 44 with cr_collection_level=None. A reader treating it as King level ranks Vijay below Andy. | Drop/rename legacy_exp_level; rely on cr_collection_level. |
| M11 | `get_clan_health` · losing_streaks | missing-caveat | yes | Uses scope='competitive_10' (incl war/2v2/events) while hot_streaks uses 'ladder_ranked_10' — different battle universes, and neither row includes the scope field. Lists look symmetric but aren't. | Return scope + definition in each row; align scopes or document the asymmetry. |
| M12 | `get_clan_game_modes` · summary mode_mix | missing-caveat | yes | by_group/by_game_mode read rollups: ladder 30d rollups=4690 vs battle_events=2018; Andy rollups=57 vs events=80 (~29% undercount). Only window_days exposed, no coverage. | Source from battle_events or annotate source='rollup' + lossy-for-new-members caveat + coverage_days. |
| M13 | `get_clan_game_modes` · side_modes | misleading | no | side_mode_progress always [] (`NULL AS progress_json` hardcoded); leaderboards always [] (zero leaderboard contexts). Reads as "nothing happening" rather than "not tracked". | Wire progress_json / populate leaderboard contexts, or emit explicit 'not_tracked'. |
| M14 | `get_elixir_state` · season_window | missing-caveat | yes | Live season 134 only week returns rank/fame/trophy_change/clans all null (war_weeks unfinalized); weeks_recorded=1 overstates completeness. No provisional caveat. | Mark in-progress week provisional or backfill from live war clock; note fame/rank land at week close. |
| M15 | `get_elixir_state` · event_summary | missing-caveat | no | total_events flat across windows (streams hold ~7 days) while battles_mirrored spans ~65 days; a "90d" block mixes 7-day stream + 65-day battle coverage. `event_class` arg silently discarded. | Surface per-source coverage span; honor event_class or drop it from schema. |
| M16 | `get_elixir_state` · recent_events | misleading | no | `event_class='battle'` is a silent no-op (`del event_class`, never queries battle_events) — returns signal-stream rows instead of battles, no error. | Implement event_class='battle' or remove the param and document coverage. |
| M17 | `lookup_cards` · name search ranking | misleading | yes | Alphabetical order: `name='Knight'` returns Golden Knight first; limit=1 returns Golden Knight, not Knight. Sibling get_card_by_name (exact-first) isn't the tool the LLM calls. | ORDER BY exact match DESC, then prefix, then length/name. |
| M18 | `lookup_cards` · result truncation | missing-caveat | yes | Default limit=25 of 126 cards / 86 troops with no total_count/has_more; "list all X" can be silently incomplete. | Return total_matched + returned/limit or a truncated flag. |
| M19 | `lookup_member_cards` · mode=war coverage | missing-caveat | yes | Correct source (battle_events) but discards reconstruct evidence (war_battles_seen=16, distinct_decks=4, confidence). total_matching=32 gives no signal whether from 16 battles or 2; collapses up to 4 war decks into one flat set. | Surface war_battles_seen/distinct_decks/confidence alongside the caveat. |
| M20 | `get_member_card_profile` · departed edge case | missing-caveat | yes | Departed #C920YGLC2 (left 2026-07-04) returns a full current-looking profile; storage never checks clan_memberships. | Join clan_memberships, add roster_status/left_at; surface fetched_at age. |
| M21 | `get_member_card_profile` · upgrade precision caveats | missing-caveat | yes | cards_required/ready presented as hard numbers with no note they come from a static offline table; an omitted field currently means the None-index bug, not "maxed". False precision ("2000 to next level"). | After H15 fix, emit the field uniformly; emit null-with-reason instead of dropping it. |
| M22 | `get_clan_intel_report` · war.clan_score mislabel | misleading | no | war.clan_score=826/820 equals clanWarTrophies, but roster.clan_score=99123 is real ladder score. Same key, two meanings; also duplicates roster.war_trophies. | Rename to war_trophies or drop it. |
| M23 | `get_clan_intel_report` · war-day coverage/window | missing-caveat | no | Cumulative-over-week decks/active_participants shown unlabeled next to decksUsedToday; war participant_count (32) ≠ roster member_count (50) with no note; no war-day context. | Add periodIndex/war day N-of-4; label cumulative vs today; surface participant_count vs member_count. |
| M24 | `cr_api` · clan_war top_participants | misleading | no | Global race-top-5, not the queried clan's: querying R.E.I.C.H returned only POAP KINGS members. No field flags the mismatch beyond clan_tag. | Compute per-clan, or rename race_top_participants and document. |
| M25 | `cr_api` · clan_members expLevel=0 | misleading | no | /clans memberList zeroes expLevel; passed through raw (reads as level-0 players). Also deprecated per 2026 model. | Drop/annotate expLevel in clan_members; caveat deprecation wherever surfaced. |
| M26 | `get_awards` · current_standings vs list semantics | missing-caveat | yes | current_standings(134) returns live standings; list(134) returns count=0 (ledger only writes at season close). Same tool/season, two sources, no flag. | Add provisional/finalized flag + source label to current_standings; document ledger-vs-live. |
| M27 | `get_awards` · list season_id overloaded | misleading | yes | season_id mixes war integers (129-134) and pol_champ YYYYMM (202606). Filtering list(133) silently excludes the pol award for the same period. | Normalize to one scheme or add season_scheme/season_label. |
| M28 | `WRITE_TOOLS` · save_clan_memory | missing-caveat | no | Non-idempotent on the awareness path (unconditional INSERT), while the leader path upserts by event_id — identical shapes behave differently by workflow. Weak field validation; awareness skips the players existence check. | Add idempotency key (event_id/content hash); validate required fields; reuse existence check. |

### LOW severity

| # | Tool · Aspect | Status | Member risk | Evidence | Fix |
|---|---|---|---|---|---|
| L1 | `resolve_member` · aliases raw-name leak | missing-caveat | yes | current_name is scrubbed but aliases still ships raw: '28'→aliases=['²⁸']; 'Shafith'→['Ｓｈａｆｉｔｈ Ｎｉｈａｌ♥️']. Bypasses the injection guard. | Fold aliases through callable_name or drop raw aliases equal to folded current_name. |
| L2 | `resolve_member` · exp_level | stale | yes | exp_level=0 for 49/51 rows (2 stale 40/44); reads as "King level 0". | Drop from projection or replace with cr_collection_level. |
| L3 | `resolve_member` · freshness | missing-caveat | no | Surfaces mutable state (trophies/rank/role) without observed_at/last_seen_api. | Include observed_at or document callers should use get_member for authoritative stats. |
| L4 | `get_member` · form staleness | correct(low) | no | Values match battle_events but departed lux_alastor returned form computed_at 6 days stale with no warning. | Add form_age_hours + stale flag. |
| L5 | `get_member` · profile nested name / exp_level | correct(low) | yes | Top-level member_name safe, but nested membership_summary.member_name leaks raw `<c0>亗ZØMB亗`; deprecated exp_level=39 surfaced, cr_collection_level None. | Use display_name in membership_summary; de-emphasize exp_level. |
| L6 | `get_member_war_detail` · vs_clan_avg semantics | missing-caveat | no | avg_fame_per_member includes 0-deck/departed rows; multi-week seasons compare cumulative fame to per-member season avg without weeks-played normalization; dead avg_fame_per_row column. | Document the basis; consider per-week normalization; drop dead column. |
| L7 | `get_river_race` · projected_day_fame caveat | missing-caveat | yes | projected_day_fame=3000 is placement floor only (boat-defense survival fame not exposed) but reads as precise; clan_fame may under-credit defense. | Add a note field: placement floor, defense fame not included. |
| L8 | `get_war_season` · season_comparison metric semantics | unverified | no | Divides SUM(war_weeks.our_fame) (incl Colosseum 42600) by DISTINCT participant count — mixes clan race-total with per-participant denominator; couldn't confirm intended basis. | Document intended fame basis; ensure Colosseum weeks don't inflate; consider per-race normalization. |
| L9 | `get_clan_roster` · trends window_days label | misleading | no | trend_summary header prints "window_days: 30" but 30 is the history span; actual comparison window is 7d. | Rename header to history_days, distinct from the 7d window. |
| L10 | `get_clan_roster` · summary | missing-caveat | no | avg_collection_level via AVG skips NULLs (unsynced members like Andy silently dropped); no sample size; avg_trophies false 2-decimal precision. | Add synced_member_count/sample_size; round trophies. |
| L11 | `get_clan_roster` · card_owners | unverified | no | Not exercised (needs card_name); maxed_only=True default silently filters "owners" to "maxed owners". | Verify display_name usage; echo maxed_only default in payload. |
| L12 | `get_clan_health` · streak freshness | missing-caveat | no | Streak rows omit computed_at; streak capped at FORM_SAMPLE=10 with no "may be longer" note. | Include computed_at/age + note the 10-battle cap. |
| L13 | `get_clan_game_modes` · events mode_mix contradiction | correct(low) | no | event_activity (battle_events) sound, but bundled rollup mode_mix disagrees (Crazy_Arena 1018 vs 984) and uses a different window (UTC vs Chicago calendar-day). | Drop/relabel the rollup mode_mix inside events; align windows. |
| L14 | `get_clan_game_modes` · duos caveat wording | correct(low) | no | Caveat says teammates outside clan don't appear, but a departed member still in players WILL appear (JOIN not filtered by left_at). | Tighten caveat; note departed members may appear. |
| L15 | `lookup_cards` · cost filters drop NULL-cost cards | missing-caveat | yes | 5 costless cards (Mirror, tower troops) excluded by min/max_cost SQL NULL semantics: tower_troop + cost 0-10 → []. | Document, or treat NULL-cost as special; surface elixir_cost=None. |
| L16 | `lookup_cards` · mode_label capability vs unlock | missing-caveat | yes | Catalog mode_label = CAPABILITY (max_evolution_level) but the member card tool uses the same name for UNLOCK (evolutionLevel); "Hero" reads as unlocked. | Emit supports_evo/supports_hero, or rename supported_modes. |
| L17 | `lookup_member_cards` · invalid rarity | misleading | yes | `rarity:'mythic'`→total_matching=0 (valid-looking) instead of unknown_rarity; `_normalize_rarity_filter` returns raw for unknown values, so the guard is dead code. | Return None for values outside {common,rare,epic,legendary,champion} so the guard fires. |
| L18 | `lookup_member_cards` · freshness/departed | missing-caveat | yes | fetched_at present but no age/stale flag; inconsistent 'Z' timestamps; departed #200V8UYCLL returns full collection with no left-club flag. | Add observed_at age/stale flag + departed indicator; normalize timestamps. |
| L19 | `get_clan_intel_report` · unnormalized external names | missing-caveat | no | top_players[].name / clan name raw from API ('огурец погибели'); no display_name path for opponents → injection tokens reach the brain. | Sanitize external names before returning / mark as untrusted. |
| L20 | `get_clan_intel_report` · freshness | missing-caveat | no | No observed_at; recently_active_count is relative to "now" with no timestamp. | Add observed_at (UTC). |
| L21 | `get_clan_intel_report` · threat_rating | missing-caveat | no | Opaque 1-5 heuristic with hidden weights/caps; ignores fame/period_points entirely (roster+engagement only) → a low-fame strong-roster clan outranks an actually-winning opponent. | Return component sub-scores / rationale. |
| L22 | `cr_api` · unnormalized external names | missing-caveat | no | Battle opponent / clan / tournament names raw ('zx3.porchy', 'السعودية'). By design (external), but unsanitized. | Run external name fields through the injection-safe normalizer. |
| L23 | `cr_api` · no freshness on any payload | missing-caveat | no | All aspects TTL-cached (up to 600s for river race log) but no fetch timestamp / cache-age. | Stamp observed_at + cache-hit on each envelope. |
| L24 | `cr_api` · tournament members_count | misleading | no | Returns members_count:null while members_returned:1 (reads absent membersCount key). | Fall back to len(membersList). |
| L25 | `get_awards` · list truncation | missing-caveat | yes | Default returns count=100 of 201 rows (101 oldest dropped, season DESC); count==returned length reads as complete. | Add total_matching + truncated/has_more. |
| L26 | `get_awards` · list player_name fallback | correct(low) | no | COALESCE(display_name,current_name); all 201 holders currently have display_name so safe, but latent raw-name risk if one lacks it (unlike current_standings which folds via callable_name). | Route the fallback through callable_name. |

---

## 4. Gap Analysis — Right Tools for the Job

**Capabilities the brain needs but lacks or must work around:**

- **No single trustworthy windowed battle record.** trend/playstyle read rollups while form/losses/battles read battle_events, and the two disagree per-day and per-window. The brain cannot get one reliable "W/L over last N days" number for a member or the clan (H1, H2, M2, M12, H12). This is the highest-leverage gap.
- **No live in-progress war fame anywhere except get_river_race/cr_api.** get_war_season, get_elixir_state (war_season/season_window), and get_clan_roster.summary all read unfinalized war_weeks and report 0 mid-week (H7, H8, H13, H14, M14). The authoritative live figure (API clan.fame / war_participation) is never wired into these aspects.
- **Opponent decks are captured nowhere.** get_member.losses' entire stated purpose (scout the cards a member loses to / propose counters) is structurally unfulfillable — battle_events has no opponent-deck columns (H3). Either build capture or remove the capability.
- **No departed/left_at awareness across the surface.** resolve_member, get_member_war_detail, get_member_card_profile, lookup_member_cards, get_clan_health streaks, and the write tools all treat departed members as active (M1, H6, M20, L18). The brain can't say "X left on <date>" and can create/reopen cases against ghosts.
- **Period-points / Colosseum blindness in war-scouting.** cr_api.clan_war and get_war_season expose only week-cumulative fame; on a Colosseum (period-points-only) week the brain has no score to read for an opponent (H18, and get_war_season gap). get_river_race is the only tool that keeps fame vs period_points separate correctly.
- **No coverage/freshness envelope convention.** Almost every windowed tool returns window_days but no days_found/battles_found/completeness and drops observed_at even when computed (M7, M5, M12, M19, L23, and the streak/card freshness findings). A shared "coverage + as_of" envelope would close a dozen findings at once.
- **Upgrade advice is unusable.** get_member_card_profile can't answer "what should this member upgrade next" — 492 truly-ready cards hidden clan-wide (H15, H16).
- **No all-time award leaderboard.** get_awards.leaderboard crashes; the brain cannot answer "most-decorated war champ" (H19).
- **No revisit self-closure.** schedule_revisit writes but nothing ever marks revisits done (H22).

**Redundant / overlapping tools that confuse the model:**

- **`clan_score` collides with itself** inside get_clan_intel_report (ladder score vs war trophies, same key) and duplicates war_trophies (M22).
- **mode_label** means CAPABILITY in lookup_cards/catalog but UNLOCK in the member card tools (L16) — same field name, two meanings.
- **exp_level** surfaced by resolve_member, get_member, get_clan_roster, cr_api — all deprecated, none paired with a Collection Level replacement (L2, L5, M10, M25).
- **Rollup vs battle_events** exposed simultaneously and unlabeled within a single get_clan_game_modes response (H12) and across get_member aspects — the model can cite either.
- **current_standings vs list** in get_awards use different season semantics under the same season_id (M26, M27).

**Dead / structurally-empty surfaces:**

- `get_awards.leaderboard` — crashes on every call (H19).
- `get_member_card_profile` upgrade layer — wrong for ~75% of cards (H15/H16).
- `get_clan_game_modes.side_modes` — side_mode_progress and leaderboards always [] (M13).
- `get_member.losses` top_opponent_cards — always [] (H3).
- `get_river_race` fame_today / top_fame_today — fame_delta never populated (M6).
- `event_class` filter — silent no-op in both event_summary and recent_events (M15, M16).
- `mark_revisited()` — exists, tested, zero runtime callers (H22).
- Unused dead columns: avg_fame_per_row (L6), joined_date hardcoded None in at_risk items.

---

## 5. Prioritized Fix Plan (member-risk × frequency first)

**P0 — Broken/crashing, member-facing, high frequency**

1. **Wire windowed battle stats + clan battle trends to `battle_events` (with a coverage envelope).** Single root fix for H1, H2, H12, M2, M12; unblocks the "one trustworthy W/L over N days" gap. *Effort: L (touches get_member trend/playstyle, get_clan_roster trends, get_clan_game_modes summary/ranked reconciliation + a shared coverage helper).*
2. **Fix mid-week war fame across war tools** — source in-progress clan fame from API clan.fame / SUM(war_participation.fame) and add an in_progress/provisional flag. Covers H7, H8, H13, H14, M14, M26. H13/H14 are a trivial key-rename (`standings→race_standings`, `our_fame→fame`, `day_number→battle_day_number`) that alone restores the entire live river race to get_elixir_state. *Effort: M.*
3. **Fix get_clan_health.trophy_drops direction** — directional net (last−first), report only real declines, read ranked_trophies for competitive drops (H11). Every live result is currently inverted. *Effort: S.*
4. **Fix get_member_card_profile upgrade layer** — pass api_level (already stored) into cards_required_to_upgrade; fixes ready lists, by_rarity.ready, progress, filters in one place (H15, H16, M21). *Effort: S.*
5. **Fix get_awards.leaderboard crash** — drop rank/limit at the call site or extend the storage fn; add a test that invokes the mode (H19). *Effort: S.*
6. **Write-tool leader-closure guard** — never reopen resolved/dismissed decision_cases from an awareness write; add departed-member guard (H20, H21). Prevents silently overturning human decisions. *Effort: S–M.*

**P1 — Misleading member-facing values, medium frequency**

7. **Injection-name cleanup** — replace raw current_name with COALESCE(display_name,current_name) in get_member_war_detail's 5 aspects, get_member battles/playstyle/duos + nested profile, and fold resolve_member aliases (H4, M3, M2, L1, L5). One pattern, several call sites. *Effort: S.*
8. **Departed/left_at flag as a shared enrichment** — resolve_member (status=None path), war_detail, card profile, streaks, lookup_member_cards (M1, H6, M20, L18). *Effort: M.*
9. **War attendance date-format fix** — normalize created_date before the last_4_weeks compare (H5). *Effort: S.*
10. **perfect_attendance / boat_battles window correctness** — finalized-days-only filter; actually scope boat_battles to N wars or rename all_time (H10, H9). *Effort: S–M.*
11. **fame_today** — populate fame_delta or derive per-day; drop/rename the mislabeled top_fame_today (M6). *Effort: M.*
12. **lookup_cards name-search relevance ranking** (exact→prefix→substring) + truncation envelope (M17, M18). *Effort: S.*
13. **schedule_revisit lifecycle closer** — wire mark_revisited + future-time guard + per-signal dedup (H22). *Effort: M.*

**P2 — Missing caveats, false precision, coverage/freshness (batchable)**

14. **Shared coverage + freshness envelope** applied across windowed tools — days_found/battles_found/completeness + observed_at/as_of. Closes M5, M7, M15, M19, L3, L12, L23, L18, and several streak/card freshness items in one convention. *Effort: M (design once, apply widely).*
15. **Deprecate exp_level surface** — drop or rename legacy_exp_level everywhere; expose cr_collection_level where a level is wanted (L2, L5, M10, M25). *Effort: S.*
16. **Unit/label disambiguation** — rename war.clan_score→war_trophies (M22); split catalog mode_label into supports_evo/supports_hero (L16); label rollup vs battle_events sources; add period_points/Colosseum to cr_api.clan_war (H18). *Effort: S–M.*
17. **cr_api top_participants per-clan** (M24); external-name sanitization for cr_api/get_clan_intel_report (L19, L22). *Effort: S–M.*
18. **Honesty fixes for structurally-dead fields** — get_member.losses guidance/coverage_note (H3), side_modes not_tracked markers (M13), event_class implement-or-remove (M15, M16), lookup_member_cards dead unknown_rarity guard (L17), lookup_cards NULL-cost filter (L15). *Effort: S each.*

**P3 — Low-risk polish**

19. Remaining low-severity items: get_awards season_id scheme + truncation (M27, L25), win_rates low-sample flag (M8), trending no-trend note (M9), threat_rating sub-scores (L21), vs_clan_avg documentation (L6), roster summary sample_size / precision (L10), dead-column cleanup (L6, at_risk joined_date). *Effort: S, opportunistic.*

**Highest ROI:** items 1, 2, and 7 — the rollup→battle_events migration, the mid-week war-fame fix (including the one-line stale-key rename in get_elixir_state), and the injection-name cleanup — together resolve the majority of the high-severity, member-facing findings and the two most damaging false narratives (the "dead clan / 0 battles" and "0 clan fame while members scored thousands" reads).
