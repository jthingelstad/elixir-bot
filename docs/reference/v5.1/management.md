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
| `sustained_donor` | donations that week ≥ **`DONOR_WEEK_MIN` (50)** | clan-stream roster state (frozen at the weekly reset). Grounding: live 8-week distribution — half the roster donates 0/week, top quartile starts ~32; 50 selects the genuinely generous ~fifth |
| `war_reliable` | decks used / decks available ≥ **`WAR_QUALIFY_RATE` (0.75)** across the week's battle days (finalized `war_attendance_days` only, `runtime.md` §3) | bounded war stream (§16.5). Training-only weeks (no battle days observed): the week is **skipped**, not failed |
| `battle_active` | battle-days in the trailing 28 days ≥ **`BATTLE_DAYS_MIN` (8)** (≈2/week) | `battle_events` (`member_management.battle_days_last_28`) |

## 3. Layer 2 — candidacy state machines

### 3.1 `promote_state` — member → elder (weekly path, Q1)

Higher promotions (elder → co-leader) are **never automated** — they stay pure
leadership judgment; the weekly review shows the metrics, nothing more.

```
none ──(gate)──▶ building ──(4 qualifying weeks)──▶ eligible ──(review)──▶ recommended
```

- **Gate (none → building):** `tenure_days ≥ PROMOTE_TENURE_MIN (28)` AND
  ≥2 of the 3 evaluators `holding`, AND current role = member.
- **building:** each weekly review where the gate still holds increments
  `promote_qualifying_weeks`. A week where it doesn't hold is a **miss**; two
  consecutive misses reset the counter to 0 and the state to `none` (one miss
  is grace — the counter simply doesn't move).
- **building → eligible:** `promote_qualifying_weeks ≥ PROMOTE_QUALIFYING_WEEKS (4)`.
- **eligible → recommended:** the weekly review raises a
  `promotion_recommendation` leader action through the policy gate. Leaders
  execute in game; the clan stream's `role_changed(promoted)` resolves the
  action (§13.5) and the state returns to `none` (at the new role).
- **Auto-withdraw:** from `eligible`/`recommended`, two consecutive miss-weeks
  → back to `building` (counter halved, floor 0) and any open action is pulled.

### 3.2 `demote_state` — elder → member (weekly path, protective of the role's meaning)

Same shape, inverted inputs:

- **Gate (none → building):** role = elder AND `sustained_donor` **and**
  `war_reliable` both `lapsed` (battle_active alone never demotes — someone can
  battle daily and still not contribute to the clan).
- **building → eligible:** gate holds for **`DEMOTE_WEEKS` (4)** consecutive
  weekly reviews (no grace week — the gate is already two-signals-lapsed, which
  §2's hysteresis makes slow to enter).
- **eligible → recommended:** `demotion_recommendation` at the weekly review.
- **Auto-withdraw:** either evaluator leaving `lapsed` resets to `none`, pulls
  any open action.

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
| `DONOR_WEEK_MIN` | 50 | live distribution (half the roster donates 0; top quartile starts ~32) |
| `WAR_QUALIFY_RATE` | 0.75 | 12 of 16 decks over a 4-battle-day week |
| `BATTLE_DAYS_MIN` | 8 (of 28) | ≈2 battle-days/week floor |
| Layer-1 hold/lapse rule | 3-of-4 / 1-of-4 | hysteresis asymmetry (§2) |
| `PROMOTE_TENURE_MIN` | 28 days | one full month before elder consideration |
| `PROMOTE_QUALIFYING_WEEKS` | 4 | §13.3 "earned over N qualifying weeks" |
| `DEMOTE_WEEKS` | 4 | symmetric with promotion |
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
