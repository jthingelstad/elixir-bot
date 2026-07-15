# Elixir v5.1 — Review Feedback

> Review of `docs/elixir-v5.1.md`, grounded against the live `elixir-v5.db` and the
> codebase. **Rev 2 — 2026-07-02** (after Jamie's revision that added §16 Clan Wars,
> the scope boundary, the reworked ingest/retention/preservation §14, the recognition
> ledger §10, and the members/players reconciliation §7).

---

## ⭐ Decisions — answered (Jamie, 2026-07-02)

The three open asks are now settled. Direction below; details in New-2/3/4.

1. **Identity: adopt the Clash Royale tag as the natural key everywhere. — LOCKED,
   and bigger than a rename.** Not "rename `members` → `players`" cosmetically — the
   real decision is: **the immutable CR player tag *is* the identity; drop the synthetic
   `member_id`; name is just metadata on the tag.** Apply the same rule to every
   tag-bearing entity the API gives us — **player tag, clan tag, tournament tag** — so
   the dots connect without a translation layer. Internal-only entities that have no CR
   tag (messages, detections, clan_memories, the recognition ledger) keep their own
   keys. This is a v5.1 principle, not a later pass. See New-3 for the ripple effects —
   several are *good* (it retro-justifies `battle_telemetry`'s tag keying and collapses
   the battle-table duplication toward the tag-keyed one).
2. **Clan Wars: there is no dormant gap. — LOCKED.** A River Race is *always* active.
   Reword §16.1: a season dies to its recap and the next bounded season is born
   immediately (contiguous, never dormant). See New-2.
3. **Recognition ledger: keep it; just give it a home. — LOCKED (mechanical).** It's
   the shared table that enforces "one moment → one post" (the fix for the double-post
   bug): each recognized moment claims a deterministic key; first claim wins via a
   uniqueness constraint. The only to-do is placing its table in §14.2 with a retention
   window. See New-4 for the plain-English writeup to drop into the doc.

---

## ✅ Rev-1 items — all resolved

| # | Item | Resolved where |
|---|---|---|
| 1 | Preservation model inverted (raw log ≠ system of record) | §14.1 (raw = 14-day forward-only buffer), §14.4 (cold-archive move) |
| 2 | §8/§14 contradiction on the raw log | §14.1 explicit; §8 now says durable history = rollup layer |
| 3 | Single-ownership was a convention, not a mechanism | §10 shared recognition ledger with deterministic keys + uniqueness |
| 4 | Memory/knowledge missing / "insight" unanchored | §0 scope boundary; §10 "insight" reframed as a deferred seam; §3 defers versions-vs-audit |
| 5 | "member" collides with the schema | §7 reconciliation paragraph (proposes `players` + membership from `clan_memberships`) |
| V1 | action-type values exist | Confirmed (kept claim in §13.4) |
| V2 | raw-log retention | Confirmed 14d, now the backbone of §14 |
| V3 | table-count denominators | Fixed — "75 designed + 11 shadow" used consistently |

The retention model (§14.3) now correctly cites `_PURGE_TARGETS` and names the durable
layer (rollups/tenure/identity/memories) as precious. The `battle_telemetry` (unbounded)
vs `member_battle_facts` (30d) retention mismatch is captured. Good.

---

## New findings — rev 2

### New-1. §16 Clan Wars — verified against real payloads (mostly confirmed)

I checked the new war design against captured `currentriverrace` payloads and `cr_api.py`:

- ✅ **Season-end detection is real.** `periodType` in live payloads is exactly
  `training` / `warDay` / `colosseum`. The latest capture is `periodType=colosseum` at
  `sectionIndex=4` (a 5-week season) — so §16.1's "every season ends with Colosseum;
  detect it, never forecast past it" is grounded and the 4-vs-5 ambiguity is genuinely
  resolvable in code. Safe to lock.
- ✅ **War Champ has its data.** `currentriverrace` carries `clan.fame` (cumulative
  season fame) and per-member `clan.participants`, with all 5 racing clans present. The
  cumulative-fame standing (§16.3) is computable. Good.
- ⚠️ **Naming footgun for the "one clean raw log."** `cr_api.py:24` aliases
  `"riverracelog": "clan_war_log"` — the code calls the correct CW2 `/riverracelog`
  endpoint (`get_river_race_log`) but **stores it under the legacy entity_key
  `clan_war_log`** ("preserves existing rows"). So §16.7's `riverracelog` is the right
  API name, but the raw log's `endpoint` column mislabels it as `clan_war_log`. This is
  exactly the sediment v5.1 wants gone — **fix the stored endpoint name to
  `riverracelog` at the cut** so the new raw log isn't born with a misleading alias.

*Subsequently corrected (2026-07-15): normal weeks use the 10,000-fame finish
line; Colosseum has no finish line. Every battle across all four Colosseum battle
days continues to count. The old 5,000 assumption was false and is now enforced
through the canonical game-truth contract rather than prompt prose.*

### New-2. §16.1 — "dormant between seasons" is wrong; reword (LOCKED, Ask #2)

Confirmed with Jamie: a River Race is **always** active. Rewrite §16.1's closing so it
does **not** claim a dormant gap. Suggested: *"A season dies to its recap and the next
season's bounded stream is born immediately — River Race is continuous, so war-season
instances are back-to-back, never dormant."* The bounded model still holds (each season
is its own instance, born on a new `seasonId`, dies at Colosseum) — the instances are
just contiguous. Worth one line noting the born-instant is the same event as the prior
season's death.

### New-3. Identity → CR tags as the natural key (LOCKED, Ask #1) — ripple effects

Decision: **CR tag = identity; drop synthetic `member_id`; name is metadata; same for
clan and tournament tags.** This is a strong, simplifying call, but it touches most of
the schema — capture the ripples so the doc treats it as a principle with consequences,
not a rename:

**Good ripples (this decision *resolves* things Part I flagged):**
- **Collapses the battle-table duplication in the right direction.** Part I §2 flagged
  `member_battle_facts` (keyed `member_id`) vs `battle_telemetry` (keyed `player_tag`) as
  near-duplicates. Tag-as-identity makes the **tag-keyed one canonical** — the survivor
  is clear, no more "keyed differently."
- **Part II is already tag-native.** Emitter dedup keys (`player_level_up:{tag}:…`), the
  recognition ledger (`arena_up:{tag}:…`), `war_participation.player_tag` (already
  NOT NULL), `clan_daily_metrics.clan_tag`, `tournaments.tournament_tag` — the target
  design already speaks tags. This decision aligns *persistence* with what the engine
  already assumes, removing the id↔tag translation layer.
- **Opponent clans and our clan get one shape.** River-race opponents are clans with
  tags; clan-tag keying means our clan and the four opponents store identically.
- **Player vs member falls out cleanly (§7).** The *player* (tag) is the durable
  identity; *membership* is a time-bounded relationship (`clan_memberships`). A departed
  player keeps their tag/history and simply has no open membership.

**Ripples to call out in the doc:**
- **§4 must change.** The identity spine is no longer "leave it alone" — it is
  *re-keyed*: `member_id` removed, `player_tag` becomes the primary/natural key, FKs
  across the schema move from `member_id` to `player_tag`. Reframe §4 as "the identity
  *model* is sound; v5.1 re-keys it to CR tags (§7)."
- **Discord side is already natural-keyed.** `discord_user_id` is a Discord snowflake
  (external, immutable) — keep it; `discord_links` just becomes `discord_user_id ↔
  player_tag`.
- **Internal-only entities keep synthetic keys.** Messages, detections, clan_memories,
  the recognition ledger, decision cases, etc. have no CR tag — they keep their own ids.
  State the rule explicitly: *if the CR API identifies it with a tag, the tag is the key;
  otherwise a synthetic id is fine.*
- **One perf footnote (non-blocking).** SQLite's fast `INTEGER PRIMARY KEY` is the
  rowid; a `TEXT` tag PK loses that aliasing and makes indexes slightly larger. At this
  scale (thousands of players, millions of battles) it's negligible — but if a hot table
  ever needs it, keep an internal rowid while the **tag stays the logical/foreign key**.
  Don't reintroduce a *semantic* synthetic id.

### New-4. Recognition ledger — keep it; place it in §14.2 (LOCKED, Ask #3)

Plain-English writeup to drop into the doc (Jamie asked what it is): *The recognition
ledger is the single shared table that enforces "one real moment → one post." Three
stream subagents run independently, so two of them can spot the same moment (a champion
unlock that is also a trophy push) and post it twice — the worst recurring bug. Before
posting, a subagent claims a deterministic key for the moment
(`champion_unlock:{tag}:{card}`, `arena_up:{tag}:{arena}`); a UNIQUE constraint means the
first claim wins and the second backs off. Single-ownership becomes a database rule, not
a subagent convention.*

Only to-do: it's durable engine state that must survive restarts (or dedup resets and you
double-post after every reboot). Add it to §14.2 — sits near event streams / projections
— with a retention window at least as long as a moment could plausibly be re-recognized
(aligning it to the relevant stream's window is simplest; it's tiny, so "durable" is also
fine).

### New-5. §13 "three streams" vs the war stream as a 4th source (wording)

§13 opens saying clan management "reads the same three streams," then §13.2/§16.5 have it
also reading **war** participation (a *bounded* stream). Tighten the wording so
"three streams" doesn't read as excluding the war stream — e.g. "reads the three
persistent streams plus the bounded war stream's participation projection."

### New-6. §14.4 "carry the durable layer forward" may need a transform (minor)

If v5.1 changes the rollup grain/shape, the old rollup tables won't drop straight into the
new schema — "carry forward" could mean a one-time transform, and it might be lossy. Not a
blocker (the cold archive is the backstop), but worth one line so it's not assumed to be a
plain copy.

---

## Rev 3 — build-spec pass findings (2026-07-02, while writing docs 2–7)

Drift and corrections surfaced while grounding the build spec; recorded here so
`architecture.md` isn't silently contradicted.

- **§15's "today" description is half right.** "Round-robins ~4–5 players per
  tick, every ~4 hours" grounds in `player-progression`
  (`PLAYER_INTEL_BATCH_SIZE = 5`, `runtime/jobs/_intel.py:55`) — but the v5
  reactive tick *separately* full-roster-polls every member's profile + battlelog
  every 30 minutes (`fetch_payloads`, `event_core/live/tick.py:22–41`). Two
  pollers, two data models — Part I's duplication in live form. Strengthens the
  §15 case; `runtime.md` §1 documents it and the adaptive scheduler replaces both.
- **Battlelog depth ≠ 25.** Empirically mode **30**, range 12–59 (400 recent
  payloads). §17.7's "verify exact battlelog depth" is resolved in
  `open-questions.md` Q8.
- **AGENTS.md drift (C4):** `leadership-action-scan` is `enabled_by_default=True`
  at 240-min interval (`runtime/activities.py:124–141`), not "disabled" as
  AGENTS.md claims.
- **Decisions Q1–Q8 supersede three architecture clauses:** §16.3 "criteria vary
  by season" (now: always top fame + free-pass rotation), §16.8/§16.5 attendance
  nudge (now: none), §17.6 manual evidence (Clan Voyages dropped). Per the README
  convention the detail docs win; flagged for the eventual architecture.md tidy.
- **One naming alignment:** the old `path_of_legend_global_rank` dedup prefix
  didn't match its `detection_type`; v5.1 sets key prefix = event_type everywhere
  (`events.md` §3).

## Rev 4 — completeness review + ratification pass (2026-07-03)

A full read of the nine-doc set assessed it build-ready pending the three README
ratifications; those are now settled and the findings below were fixed in place.

**Ratified (Jamie, 2026-07-03):**

- `arena_up` base score **85, bypass** (recognition.md §4/§9) — as drafted.
- Runtime defaults **as drafted** (runtime.md §8): 10-min tick,
  `POLL_BUDGET_PER_TICK = 40`, poison-skip after 3.
- Weekly leadership review: **Monday 7:00 AM America/Chicago** (runtime.md §3).

**Findings fixed in this pass:**

- **`cake_day_announcements` carry was a false safety.** migration T13 claimed
  carrying it prevents cross-cut birthday re-posts — but the table is **empty**
  (verified live), on a 7-day purge (`storage/metadata.py:411`), written only by
  the Gen-B roster path; the live Gen C calendar detectors dedup via detection
  `dedup_key` instead. Fixed: the table **drops** (schema.md §2 note, §8.1), and
  a new **T14** seeds the recognition ledger from the archive's `detections`
  (calendar + `weekly_donation_leader` types, trailing 14 days; keys are
  format-identical so they copy verbatim). Engine table count 49 → **48**; a
  Phase 6 parity check covers the seed.
- **The "27 dropped tables" count didn't add up.** The §8 list mixed true drops
  with transforms and omitted `ingest_cursor` / `projection_tracking` /
  `signal_detector_cursors` (all verified live). Rewritten as §8.1 (26 dropped
  outright) + §8.2 (7 transformed) = **33 names gone**, all verified;
  acceptance criterion #4 updated.
- **Hysteresis week-roll owner was unassigned.** `promote_qualifying_weeks` /
  `week_anchor` now roll **only** in `weekly-leadership-review`; the engine tick
  updates evaluator states but never the weekly grain (runtime.md §2 step 5, §3)
  — a mid-week flap can't mint a qualifying week.
- **`weekly_donation_leader` owner was unassigned.** Pinned to the roster
  emitter: reset detected in the roster diff, leader computed from the
  *previous* baseline in the same transaction (events.md §4).
- **Scope wording:** events.md §1 now distinguishes scope (widest permitted
  audience) from posting (recognition's call) — resolves the
  `role_changed(demoted)` public-scope-but-never-posted apparent contradiction.
- **Retention asymmetry documented:** war summaries (365d/durable) outlive war
  battles (`battle_events` 180d); battle-level war reads cover ~4–6 trailing
  seasons by design (schema.md §1 note).
- **architecture.md superseded clauses now carry inline callouts** (§13.7,
  §16.3, §16.5, §16.8, §17.6 → Q1–Q7), so a reader following the README's
  reading order can't absorb a stale decision.

## Rev 5 — pre-build cold read (2026-07-03)

Jamie requested one final review before the build. A fresh-context reader went
through all nine documents as a cold builder; 20 findings, merged with the
maintainer read's three. Decisions made (Jamie, 2026-07-03) and fixes applied:

**Decisions:**

1. **`war_events` gets its own table** (schema.md §5.3, 365 d) — the six war
   event types were cataloged, emitted, and consumed but had no storage home;
   the biggest gap in the set. Engine count 48 → **49**. Tournament lifecycle
   moments deliberately do *not* get a table — they are ledger-claimed derived
   moments off the `tournaments` star.
2. **`management.md` added** — the clan-management core had state enums and
   scheduling but zero transition rules anywhere (and `CLAN.md` holds only the
   inactivity thresholds). The new doc specifies the Layer-1 qualifying rules,
   the 3-of-4/1-of-4 hysteresis machine, and all three candidacy paths.
   Its §5 defaults were **ratified as drafted later the same day** — the set's
   final gate, closed.
3. **`member_management` warm-up accepted** — starts fresh at the cut; no
   archive seeding of hysteresis state (documented in migration.md Phase 3).

**Bug-class fixes:**

- **War keys stamped from the wrong clock.** Tick step 2 stamped war keys "from
  the war clock" (tick-time) — late-mirrored battles from a previous war day
  would land in the current day. Now resolved from the battle's own
  `battle_time` against the season calendar (runtime.md §2).
- **Migration T3 under-seeded `clans`.** Opponent tags older than the raw log's
  14-day window would break T9's `war_week_clans` FK; `war_period_clan_status`
  added as a T3 source with T3-before-T9 ordering.
- **Envelope math contradicted the ratified budget.** "~9 k/day worst case"
  assumed 62+ calls/tick under a 40-call budget. Rewritten: budget covers
  per-player calls only (clan/riverrace are ≤3/tick overhead), worst-case
  ceiling ≈ 6.2 k/day; `riverracelog` fetches once per week/season close.
- **Battle-stream cursor was undefined** — `battle_events` has a TEXT PK, so
  "events since cursor" had no ordering key. Defined: implicit rowid (table is
  not `WITHOUT ROWID`); `event_id` for the other three streams (runtime.md §2).

**Owner/boundary pins:** calendar events → a clock-driven calendar emitter in
tick step 3 (first tick of each Chicago day); `clan_score_milestone` /
`clan_league_changed` / war events → the direct-post path, with the
celebrate-vs-direct boundary now stated once in recognition.md §1;
`arena_changed` explicitly scores as the `arena_up` claim (85);
`war_attendance_days` → tick upserts live, the daily activity finalizes, and
evaluators read finalized days only; tournaments → leader-triggered watch job
carried as-is (verified in `runtime/jobs/_tournament.py`).

**Verbatim-carry repairs** (constants that were cite-only into files Phase 4
deletes): the full `_CELEBRATE_PRIORITY` table (translated to v5.1 names, plus
one **new** value — `arena_up` priority **90**, consistent with its ratified
85/bypass score; flag if you want it elsewhere), the complete `_META_MARKERS`
list, `ranked_pulse`'s thresholds (7 d / 12 battles / 12 decided / 9 wins /
70%), `trophy_push`'s run semantics (anchored on the run's *last* battle), and
`weekly_donation_leader` restored to **top-3** (`TOP_N = 3` was silently
narrowed to top-1 in the first draft of events.md).

**Honesty fixes:** `hot_streak` is *not computed* in v5.1 (matches today — the
detector is intentionally unregistered; it was ambiguously "computable but
retired"); architecture.md §14.3/§14.4 now carry the Q8 supersession the rev-4
callout pass missed; README no longer claims DDL for every table (carried
tables export their DDL from the archive at Phase 2, now specified);
`raw_api_payloads` got its missing migration disposition (fresh, data not
copied); the battle stream's dedup-key exception is stated in events.md §1;
lane-key ≠ channel-name is called out in recognition.md §6; the startup
initialize-at-head rationale is rewritten plainly; `pol_global_rank_attained`'s
key rank is `to_rank`; the riverrace baseline row is fully specified
(`entity_kind='riverrace'`, `entity_tag` = our clan tag, aspect `'race'`).

## Net

Rev 2 closed every rev-1 item; the §16 Clan Wars design holds up against the real API
data; and the three open decisions are now **answered** (top of doc):

- **Identity → CR tags** (New-3) is the one with reach — it re-keys the schema and
  should be written up as a v5.1 *principle*, but it *simplifies* (retro-justifies
  `battle_telemetry`, aligns persistence with the already-tag-native Part II). §4 needs
  reframing from "leave identity alone" to "re-key identity to tags."
- **No war dormancy** (New-2) and **recognition-ledger placement** (New-4) are quick
  wording/modeling fixes.

Remaining work is all mechanical: the §4 reframe (New-3), the §16.1 reword (New-2), the
ledger's home in §14.2 (New-4), plus New-1 (fix the `clan_war_log` → `riverracelog` alias
at the cut), New-5 ("three streams + bounded war stream" wording), and New-6 (carry-forward
may need a transform). Nothing structural is open.
