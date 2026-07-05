# Elixir v5.1 — Clan-Management Rules

> **Status:** ✅ Build-ready — §5 defaults **ratified as drafted (Jamie, 2026-07-03)**.
> **Owner:** Jamie · **Last worked:** 2026-07-03
>
> The transition rules for the §13.3 deterministic core. The 2026-07-03 clarity
> review (feedback.md rev 5) found that `schema.md` §6.3 defines the state
> *enums* and `runtime.md` §2/§3 defines *when* they run, but no document
> defined a single evaluator rule or candidacy transition. This doc closes that
> gap. Policy numbers are **defaults** grounded in the live data and `CLAN.md`'s
> existing thresholds; they live in `CLAN.md` after ratification (§13.3 —
> "the state machines read them; the subagent never invents policy").

## 1. Ground rules (from the architecture, restated as constraints)

- **Engagement = battling.** Every rule below reads battles, donations, and war
  participation. `lastSeen` and logins are never inputs (§13.6). Where
  `CLAN.md` says "days of no login," v5.1 reads it as **days since last
  battle** (`battle_events`, any mode).
- **Weeks roll only at the weekly review** (Monday 7:00 AM America/Chicago,
  `runtime.md` §3). The tick recomputes *states* continuously; qualifying-week
  counters move once per week. A "week" below = the CR Monday-reset week just
  closed.
- **The LLM reads states, never metrics-to-judgment.** The subagent turns
  `eligible`/`recommended` states plus the evidence columns into leader-action
  copy. It cannot promote a state.
- **Auto-withdraw is structural** (§13.4): any transition *out* of a
  recommending state pulls the open leader action that state raised.

## 2. Layer 1 — sustained-signal evaluators

Three evaluators, one shared state machine. Per evaluator, each closed week
either **qualifies** or doesn't (rules below); the machine runs on the
qualifying-week history:

```
none ──(first qualifying week)──▶ building
building ──(≥3 qualifying of last 4 weeks)──▶ holding
holding ──(≤1 qualifying of last 4 weeks)──▶ lapsed
lapsed ──(next qualifying week)──▶ building
```

The 3-of-4 / 1-of-4 asymmetry *is* the hysteresis: one strong week never mints
`holding` (the §13.1 failure — "promoting a member for one strong donation
week"), and one weak week never breaks it.

| Evaluator | A week qualifies when | Source |
|---|---|---|
| `sustained_donor` | donations that week **> 0 AND ≥ the active roster's median** | clan-stream roster state (frozen at the weekly reset). Relative/self-calibrating (Jamie 2026-07-05: "remove the static filter, let the math do the work") — the static `DONOR_WEEK_MIN=50` had drifted to select ~78% of the roster as its grounding distribution changed. Median = "above-typical donor"; the >0 guard keeps freeloaders out even in a low-donation week. |
| `war_reliable` | decks used / decks available ≥ **`WAR_QUALIFY_RATE` (0.75)** across the week's battle days (finalized `war_attendance_days` only, `runtime.md` §3) | bounded war stream (§16.5). Training-only weeks (no battle days observed): the week is **skipped**, not failed |
| `battle_active` | battle-days in the trailing 28 days ≥ **`BATTLE_DAYS_MIN` (8)** (≈2/week) | `battle_events` (`member_management.battle_days_last_28`) |

## 3. Layer 2 — the elder band (ratified 2026-07-05, Jamie)

> **This replaces the absolute-threshold model** (independent 2-of-3 signal
> qualification). Elder is a *scarce, relative* status: "are you among the
> most deserving, given limited slots" — not "did you clear a fixed bar."
> Elixir manages member↔elder ONLY; co-leader and above are pure leadership
> judgment (the review shows metrics, never a state).

The pipeline: **filters gate who's rankable → a score ranks them → the band
sizes the corps → hysteresis paces the moves.**

### 3.1 Filters (hard gates — fail any and you are not rankable for elder)

- **Tenure:** `tenure_days ≥ PROMOTE_TENURE_MIN (28)`. Four weeks in the clan
  or you are not considered, period.
- **Competitive-contribution floor (war OR ranked):** you must compete for the
  clan's prestige in *some* arena — **either** played war on ≥1 finalized
  battle day (decks used) in the last 14 days (`WAR_FLOOR_DAYS = 1`,
  `WAR_FLOOR_WINDOW = 14`), **or** hold a meaningful Ranked standing:
  **Champion league (4) or above** (`RANKED_FLOOR_LEAGUE = 4`), using the
  **better of current and last-season league** so the monthly reset never
  strips credit. Ratified 2026-07-05 (Jamie): "it is a point of pride to have
  ranked players" — a Ultimate Champion who grinds Ranked instead of wars is
  contributing to the clan's prestige, not shirking. A member who does neither
  meaningful wars nor meaningful ranked is not elder material.

Members failing a filter are absent from the ranking. An elder who fails the
competitive floor **entirely** (no wars AND no ranked standing) becomes a
demotion candidate directly (§3.4) — but ranked standing alone satisfies it, so
the UC grinder is protected.

### 3.2 The score (rank order among filtered members)

A blend of the member's **competitive contribution** and generosity, *within
the current roster*:

```
competitive = max(war_rate_percentile, ranked_prestige)
score       = 0.65 · competitive + 0.35 · donation_percentile
```

- **war_rate** = decks used ÷ decks available over the trailing war weeks
  (continuous — "more is better", a core way to compete for the clan).
- **ranked_prestige** = an **absolute** 0–1 from the member's best-of
  (current, last-season) Ranked league + rating — "reached Ultimate Champion"
  is an achievement in absolute terms, not relative-to-clan. League map:
  Champion (4) ≈ 0.5, Grand Champion (5) ≈ 0.65, Royal Champion (6) ≈ 0.8,
  Ultimate Champion (7) ≈ 0.9–1.0 scaled by rating; below Champion → 0.
- `competitive = max(...)`: you get credit for your **best** arena — a war
  stalwart with no ranked and a UC grinder with no wars both land high; nobody
  is punished for specializing. Ratified 2026-07-05 (Jamie's two knobs:
  Champion floor + UC-weighted absolute prestige).
- **donations** = the closed-week donation volume ("lead by example"),
  percentile within the roster.
- Percentiles compute over the active non-leadership roster each weekly review,
  so the bar self-calibrates. Weights `SCORE_W_WAR = 0.65`,
  `SCORE_W_DONATION = 0.35` (the war weight now names the whole competitive
  term).
- **Battle/laddering is NOT in the elder score** (Jamie 2026-07-05) — general
  activity belongs to the kick path (§3.5), not elder-worthiness.

### 3.3 The band (how many elders)

Target elder count for a non-leadership roster of `N` (roster minus leaders and
co-leaders by role):

```
floor   = round(0.15 · N)     ceiling = round(0.20 · N)
```

**The corps TRACKS THE RANKING** (Jamie, 2026-07-05 — reverses the earlier
"never churn an in-band elder" reading): *"it is entirely appropriate for an
elder to lose because he was outranked — that is by design. Elder isn't held
if you don't do the things."* The Elders should be the **top-K** of the
eligible roster, where `K = clamp(current_elders, floor, ceiling)`:

```
should_be_elders = top-K by score, among those eligible to hold Elder
                   (pass the competitive floor; members also need tenure ≥ 28)
promote_set  = members in should_be_elders who aren't Elders
demote_set   = Elders NOT in should_be_elders  (outranked OR abandoned)
```

This **unifies** the two ways to lose the seat: *abandonment* (an elder fails
the competitive floor entirely — removed from the ranking) and *outranked* (a
member overtakes them past the K line). Below-floor growth still requires the
**worthiness floor** (score ≥ median, `WORTHINESS_MIN_PERCENTILE = 0.50`) so a
thin/weak clan promotes nobody rather than the undeserving. "Range, not quota"
now means the *count* floats in [floor, ceiling] while the *composition* always
tracks the ranking.

### 3.4 Anti-flap + hysteresis — demotion easier, swaps never oscillate

A swap must be *earned and durable*, never a jitter at the boundary:

- **`SWAP_MARGIN = 0.05` deadband:** a member displaces an elder only when they
  out-score them by ≥ the margin. A near-tie holds the incumbent — so two
  players trading a hair-lead week to week never swap the seat.
- **Sustained weeks** (the state machines): a member holds the promotable set
  for **`PROMOTE_QUALIFYING_WEEKS (3)`** reviews → `eligible`; one miss is
  grace, two consecutive misses reset.
- **Reason-specific demotion cadence:**
  - *outranked* → **3 weeks** (matches the challenger's promotion cadence, so
    the swap lands together and the count never dips mid-swap);
  - *abandonment* (failed competitive floor) → **`DEMOTE_WEEKS (2)`**, faster —
    abandoning the duty shouldn't linger.

Margin + weeks together make oscillation impossible: a swapped-out elder must
re-clear the margin *and* sustain 3 weeks to swap back.
- **eligible → recommended → resolved:** the review raises the leader action;
  the clan stream's `role_changed` resolves it and the state returns to `none`
  at the new role. Auto-withdraw: leaving the candidate set two weeks (promote)
  or one week (demote) pulls any open action.

### 3.5 `kick_state` — the reactive path (Q1), unchanged by the band

### 3.3 `kick_state` — the reactive path (Q1)

Driven by **days since last battle** (`D` below), evaluated every tick, using
`CLAN.md`'s existing trophy-scaled formula reinterpreted battle-based (§1):

```
none ──(D ≥ 3)──▶ watch ──(D ≥ max(7, trophies/1000 × 1.4))──▶ at_risk
at_risk ──(+KICK_CONFIRM_DAYS with zero battles)──▶ recommended  [reactive]
any battle ──▶ none  (open recommendation auto-withdrawn)
```

- `watch` at **3 days** is `CLAN.md`'s `inactivity_days` ("early attention, not
  removal"). No action fires; it's visible in `get_clan_health(at_risk)`.
- `at_risk` uses the existing trophy-scaled threshold verbatim (`CLAN.md`
  Thresholds: a 5k-trophy member at 7 d, 10k at 14 d, 12.5k at 17.5 d —
  "higher-trophy members have earned more rope").
- `at_risk → recommended` fires the **reactive** `kick_recommendation`
  (`runtime.md` §2 step 5) through the policy gate, after
  **`KICK_CONFIRM_DAYS` (7)** more battle-free days. `kick_state_since` tracks
  each entry.
- **Guards:** members with `tenure_days < NEW_MEMBER_GRACE (14)` never pass
  `watch` (reception period). Role elder+ never fires the reactive path — their
  inactivity surfaces in the weekly review instead (kicking an elder is a
  leadership conversation, not a bot escalation). An open
  `flag_member_watch` decision case (leadership hold) suppresses
  `recommended` until resolved.
- The executed kick feeds **C1**: a `done` `kick_recommendation` within 14 days
  suppresses the public `member_left` post (recognition.md §4).

## 4. Evidence columns

The §6.3 metric columns (`donations_4wk_avg`, `war_fame_3season_avg`,
`war_attendance_rate`, `battle_days_last_28`, `tenure_days`) are **not** inputs
to the machines beyond the rules above — they are carried so the subagent can
*render evidence* ("4-week donation avg 212, war attendance 94%") without
re-deriving anything. `state_json` holds each machine's internals
(qualifying-week history, miss counters) so auto-withdraw and the parity of
"why is X eligible?" are answerable.

## 5. Defaults — ✅ ratified as drafted (Jamie, 2026-07-03)

| Constant | Default | Basis |
|---|---|---|
| ~~`DONOR_WEEK_MIN`~~ | *removed 2026-07-05* | donations are now a ranking input (percentile), not a filter |
| **Elder band** (2026-07-05) | | |
| `PROMOTE_TENURE_MIN` | 28 days | four-week hard filter before elder consideration |
| `WAR_FLOOR_DAYS` / `WAR_FLOOR_WINDOW` | 1 / 14 | war half of the competitive floor (≥1 finalized day in 14) |
| `RANKED_FLOOR_LEAGUE` | 4 (Champion) | ranked half of the competitive floor; best-of current/last season |
| `SCORE_W_WAR` / `SCORE_W_DONATION` | 0.65 / 0.35 | war-weighted rank blend; war is the core elder duty |
| `ELDER_BAND_FLOOR` / `ELDER_BAND_CEIL` | 0.15 / 0.20 | elder share of the non-leadership roster (range, not quota) |
| `WORTHINESS_MIN_PERCENTILE` | 0.50 | below-floor promotions still require ≥ roster-median score |
| `SWAP_MARGIN` | 0.05 | anti-flap deadband — a member displaces an elder only by out-scoring them this much |
| `PROMOTE_QUALIFYING_WEEKS` | 3 | sustained weeks to promote / to swap out an outranked elder |
| `DEMOTE_WEEKS` | 2 | abandonment demotion (faster); outranked demotion uses the 3-week promote cadence |
| **Kick path (unchanged)** | | |
| `WAR_QUALIFY_RATE` | 0.75 | (legacy signal; retained for evidence rendering) |
| `BATTLE_DAYS_MIN` | 8 (of 28) | ≈2 battle-days/week floor |
| `KICK_CONFIRM_DAYS` | 7 | week of silence past the trophy-scaled threshold |
| `NEW_MEMBER_GRACE` | 14 days | reception period; kick path suspended |

These move into `CLAN.md`'s Thresholds section (the precedent home) **at the
cut**, alongside C5's prose update — `CLAN.md` is the live bot's prompt, so the
constants land when the engine that reads them does (migration.md Phase 5).
This doc's tables remain the reference for what they mean.

## 6. What this doc deliberately does not do

- **No scoring, no LLM judgment** — states in, leader actions out (§13.3).
- **No new metrics** — every input already exists in the schema
  (`member_management`, `war_attendance_days`, `battle_events`, roster state).
- **No cross-clan portability claims** — the defaults are POAP KINGS-shaped;
  `CLAN.md` is where another clan would retune them.
- **Warm-up accepted** (migration.md Phase 3): all machines start at
  `none`/zero at the cut; kick-risk is live within ~2 weeks, promotion
  eligibility within ~4–6.
