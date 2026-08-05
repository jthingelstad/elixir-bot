# Elixir v5.1 — Clan-Management Rules

> **Status:** 📐 Design record — §5 defaults **ratified as drafted (Jamie, 2026-07-03)**.
> **Owner:** Jamie · **Last worked:** 2026-07-03 · **Drift audited:** 2026-08-05
>
> ⚠️ **The elder band in §3.3 has changed materially since ratification and the
> numbers below are NOT current.** For what the engine does today, read
> `prompts/POLICY.md` (leader-facing prose) or `engine/management.py` (the
> constants themselves). This doc records the *original decisions and their
> rationale*, which is why superseded rules are marked in place rather than
> rewritten — the reasoning behind a number is worth keeping even after the
> number moves. Every such point below carries a **⟳ Superseded** note with the
> current value and the date it changed.
>
> This matters because the doc is cited as an authority from
> `engine/management.py:1`, `AGENTS.md` ("their meaning lives in
> `management.md`"), and `storage/war_analytics.py` (by section number). A
> reader following those pointers lands here expecting current rules.
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
- **Competitive-contribution floor (war OR ranked — PARTICIPATION):** you must
  actively compete for the clan in *some* arena — **either** played war on ≥1
  finalized battle day (decks used) in the last 14 days (`WAR_FLOOR_DAYS = 1`,
  `WAR_FLOOR_WINDOW = 14`), **or** played ≥`RANKED_FLOOR_BATTLES (5)` ranked
  battles in that same 14-day window. Reworked 2026-07-12 (Jamie): every Elder
  metric must be in the player's control and reward *participation*, not account
  power. The old floor was "reached Champion league" — a skill/collection ceiling
  that let a strong account coast on an old climb without playing. Now a member
  who neither plays war nor plays ranked recently is not elder material —
  regardless of how high they once climbed.

Members failing a filter are absent from the ranking. An elder who fails the
competitive floor **entirely** (played neither war nor ranked) becomes a
demotion candidate directly (§3.4) — but recent ranked *play* satisfies it, so
the active ranked specialist is protected.

### 3.2 The score (rank order among filtered members)

A blend of the member's **competitive contribution** and generosity, *within
the current roster*:

```
competitive = war_pct + RANKED_WEIGHT · ranked_pct · (1 − war_pct)   # RANKED_WEIGHT = 0.40
score       = 0.65 · competitive + 0.35 · donation_percentile
```

- **war_pct** = percentile of war_rate (decks used ÷ available) — the PRIMARY
  lane; war is direct clan contribution (Fame + clan-league progress).
  > ⟳ **Superseded 2026-08-04** — `war_rate` is no longer a flat decks ratio. It
  > is per-day credit **averaged over the days**, and finishing a day is worth
  > more than its deck share: `credit = 0.75·(used/available) + 0.25·(complete)`
  > (`FULL_DAY_BONUS = 0.25`). 4/4 = 1.00, 3/4 = 0.56, 2/4 = 0.38 — four decks is
  > ~2.7× two, not 2×. Averaging per *day* rather than summing decks first is
  > load-bearing: summed, four half-days would score identically to two complete
  > ones, which is the exact distinction the bonus exists to make. Jamie: *"All
  > four is more than half the decks and should impact calculation."*
- **ranked_pct** = percentile of **ranked battles played** in the score window —
  PARTICIPATION, not league reached (reworked 2026-07-12). Playing ranked reps
  the clan but doesn't build it the way war does, so it's muted by
  `RANKED_WEIGHT = 0.40` and only fills part of the *gap war leaves* (the
  `(1 − war_pct)` headroom): war-maxed → ~1 already; ranked-only caps low
  (~0.4·pct); doing **both** beats a single lane; bounded 0–1 (never saturates —
  the earlier `max()`/prestige version pinned most active members at 1.0).
- With pure-participation scoring, being **outranked** means "someone
  participated more than you this week" (in your control), not "someone has a
  stronger account." Concrete effect at the switch: a high-league / low-war elder
  who was #1 on prestige dropped to mid-pack; the members actually grinding war +
  ranked rose to the top.
- **donations** = the closed-week donation volume ("lead by example"),
  percentile within the roster.
  > ⟳ **Superseded 2026-07-12** — the input is the **trailing 4-week average**
  > (`member_management.donations_4wk_avg`), not a closed week. A single week
  > swings hard on the donation reset; a near-empty Sunday snapshot made
  > donations count for nothing. This also makes the scored number equal the
  > number the Elder Standing report and webapp display.
- Percentiles compute over the active non-leadership roster each weekly review,
  so the bar self-calibrates. Weights `SCORE_W_WAR = 0.65`,
  `SCORE_W_DONATION = 0.35` (the war weight now names the whole competitive
  term).
  > ⟳ **Amended 2026-08-04** — all three inputs are **zero-anchored**
  > (`_participation_percentile`): doing none of a thing scores 0.0 for it, and
  > only the members who did it are percentiled against each other. A mid-rank
  > percentile is a *ranking* device, but the score consumes it as a *credit
  > magnitude*, so a tied block of non-participants was being paid its midpoint.
  > Measured on the live roster: 27 of 41 members had zero ranked battles and
  > each received `ranked_pct = 0.329`, worth up to 0.086 of final score — **1.7×
  > `SWAP_MARGIN`**, the margin that decides seats. Eleven members with a 0.00
  > war rate likewise collected 0.134. Ranking participants among themselves
  > (rather than against the whole roster) keeps the scale meaningful when most
  > of the clan sits at zero — a whole-roster percentile would hand the very
  > first battle a ~0.67 cliff.
- **Battle/laddering is NOT in the elder score** (Jamie 2026-07-05) — general
  activity belongs to the kick path (§3.5), not elder-worthiness.

### 3.3 The band (how many elders)

> ⚠️ **This whole section is superseded.** Both the percentages and the
> population changed, and the single-`K` model was replaced by three lines with
> different jobs. The current rules are in `prompts/POLICY.md` §"How many
> Elders" and `engine/management.py::_elder_band`. Kept here for the 2026-07-05
> rationale — the "corps tracks the ranking" principle below is still exactly
> why the band works the way it does.

Target elder count for a non-leadership roster of `N` (roster minus leaders and
co-leaders by role):

```
floor   = round(0.15 · N)     ceiling = round(0.20 · N)
```

> ⟳ **Superseded twice.** The share is now **`0.20 / 0.30`** (2026-07-05
> retune), and since 2026-08-01 it is taken over the **whole active roster,
> leadership included** — not `N`. Jamie: *"one in four or five people here
> wears a badge"* is a statement about the clan you see in-game, and excluding
> the four leaders from the denominator quietly shrank it. Everything *else* —
> rank, median, who is promotable — still runs over the non-leadership
> population, because a co-leader cannot be promoted to elder and should not
> dilute the median that decides whether a member is worthy. Two populations,
> deliberately different. At a 45-member roster the live band is **9–14**, where
> this doc's formula would give 6–8.

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

> ⟳ **Superseded — there is no single `K` any more.** `clamp(current_elders,
> floor, ceiling)` made the corps' current size its own target, so growing to
> the floor and stopping was the behaviour. The floor is the *lowest acceptable*
> count, not the goal. Three lines now, each with a distinct job:
>
> ```
> target  = round((floor + ceil) / 2)   the midpoint; what growth is pulled TOWARD
> ceiling = round(0.30 · roster)        HARD; growth stops, and only past it is
>                                       anyone demoted for the count alone
> floor   = round(0.20 · roster)        a DRIFT LIMIT; force-promotes nobody
> ```
>
> `grow_line` = top-`target` (who is worth promoting); `hold_line` = top-`ceil`
> (who keeps a seat). Separating them is what lets the count sit anywhere in the
> band: an elder ranked 12th of 13 is inside the tolerance and is **not**
> displaced merely for being past the target, while a member inside the top 11
> is still worth promoting. A close call at the boundary can carry the corps to
> the ceiling, and a thin field lets it settle back toward the floor, without
> either edge ever being the aim.
>
> ⟳ **The worthiness floor now gates ALL growth, not just below-floor growth.**
> Promoting a below-average member to hit a number is precisely what a target
> must never cause — this is the mechanism by which a weak field trends the
> corps *down* instead of padding it out. Note also that
> `WORTHINESS_MIN_PERCENTILE = 0.50` is **decorative**: the constant is defined
> but referenced nowhere, and the median comparison in `_elder_band` is
> hardcoded. Changing the constant would do nothing.

### 3.4 Anti-flap + hysteresis — demotion easier, swaps never oscillate

A swap must be *earned and durable*, never a jitter at the boundary:

- **`SWAP_MARGIN = 0.05` deadband:** a member displaces an elder only when they
  out-score them by ≥ the margin. A near-tie holds the incumbent — so two
  players trading a hair-lead week to week never swap the seat.
  > ⟳ **Amended 2026-08-04 — tenure decides close calls.** Inside the deadband
  > the contest is a close call, and the longer-tenured player wins it. Usually
  > the incumbent *is* the longer-tenured one and nothing changes; the case this
  > exists for is the long-serving member held out by a recently-promoted elder
  > who is barely ahead. Tenure never manufactures a promotion — the challenger
  > must still be ahead on score; tenure only stops the deadband from shielding a
  > shorter-tenured incumbent. Jamie: *"clan tenure should weigh in for ties or
  > close calls. Tenure wins."* Both paths emit the single reason string
  > `"outranked"`; a token like `outranked_tenure` is an engine internal and the
  > reason reaches the member-facing Elder Standing report.
  >
  > ⟳ **The pairing is across the K line, not best-vs-best.** Pair the *weakest*
  > challenger above the line against the *strongest* outranked elder below it —
  > the closest contest, which is where a deadband belongs. Sorting both lists
  > the same way paired strongest-with-strongest (the widest gap, the easiest
  > swap), so the margin was tested against a contest that was never close and
  > the seat nearest the line got taken on a hair's lead. The contest is judged
  > at the **hold line**, not the target: an elder who slipped past the ceiling
  > was pushed out by whoever occupies that seat, and pairing only against
  > members inside the target would leave them unpaired and demoted with no
  > deadband check at all.
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

### 3.5 `kick_state` — the reactive path (Q1)

> Numbered **3.3** until 2026-08-05, colliding with the band section above while
> §3.2 pointed readers at a §3.5 that did not exist.

Driven by **days since last battle** (`D` below), evaluated every tick.
**Redesigned 2026-07-11** — flat thresholds, contribution grace scaled by
roster fullness, no trophy buffer, no newcomer shield:

```
none ──(D ≥ 3)──▶ watch ──(D ≥ KICK_AT_RISK_DAYS (5))──▶ at_risk
at_risk ──(D ≥ KICK_AT_RISK_DAYS + confirm_days)──▶ recommended  [reactive]
any battle ──▶ none  (open recommendation auto-withdrawn)

confirm_days = KICK_CONFIRM_DAYS (3)
             + (round(KICK_CONTRIB_GRACE_MAX (4) × slack) if contributes else 0)
slack        = max(0, ROSTER_CAP (50) − active_members) / ROSTER_CAP
contributes  = _passes_war_floor OR _passes_ranked_floor   # the elder floor
```

- `watch` at **3 days** is early attention, not removal — no action fires; it is
  visible in the management capability and awareness read.
- `at_risk` at a **flat 5 days** (`KICK_AT_RISK_DAYS`). Trophies buy **no** leeway
  — the old `max(7, trophies/1000 × 1.4)` buffer is removed (a high-trophy idle
  member on a full roster still costs a slot).
- **Contribution grace** replaces the trophy buffer: a member who passes the
  **same floor that earns elder** — a war day with a deck played in the last 14,
  *or* ≥5 ranked battles in the last 14, ranked counting equally — gets up
  to **+4 confirm days**, but only *scaled by open-slot slack*.
  > ⟳ **Corrected 2026-08-05.** This read "*or* Champion ranked … on live/last
  > season, not a 3-season average", which was already stale when written: the
  > ranked half of the elder floor became **participation** on 2026-07-12
  > (`RANKED_FLOOR_BATTLES = 5` in 14 days). Because the grace deliberately
  > reuses `_passes_war_floor OR _passes_ranked_floor`, it inherited the rework
  > automatically and the prose simply fell behind the code it describes. Full grace on an
  empty roster; **zero at 50/50** — an idle member only costs a slot when the
  clan is full. So the plain card is **8 days** (5 + 3); a contributor on a
  near-empty roster stretches to ~12.
- `at_risk → recommended` fires the **reactive** `kick_recommendation`
  (`runtime.md` §2 step 5) through the policy gate. `kick_state_since` tracks
  each entry.
- **Guards:** role elder+ never fires the reactive path — their inactivity
  surfaces in the weekly review instead (kicking an elder is a leadership
  conversation, not a bot escalation). **The LOA exception:** an open **`Hold:`**
  memory (`Hold:`/`Away:`/`LOA:` title — a member who told leaders they'll be
  away) suppresses `recommended` until it expires. A brain `Watch:` *inference* note does **not** — it only
  observes idleness; matching it (as the guard did pre-2026-07-11) self-defeated
  the card by shielding the very members it flagged.
- **No newcomer shield:** short-tenure members are held to the same clock as
  everyone else — a brand-new account that never engages should engage *more*
  early, not less. (`NEW_MEMBER_GRACE` removed.)
- The executed kick feeds **C1**: a `done` `kick_recommendation` within 14 days
  suppresses the public `member_left` post (recognition.md §4).

## 4. Evidence columns

The §6.3 metric columns (`donations_4wk_avg`, `war_fame_3season_avg`,
`war_attendance_rate`, `battle_days_last_28`, `tenure_days`) are **not** inputs
to the machines beyond the rules above — they are carried so the subagent can
*render evidence* ("4-week donation avg 212, war attendance 94%") without
re-deriving anything.

> ⟳ **`donations_4wk_avg` is no longer evidence-only** — since 2026-07-12 it is
> the donation term of the elder score itself (`_elder_scores` reads the column
> directly). One upside of the change: the number that is *scored* is now
> identical to the number the Elder Standing report and the webapp *display*,
> so a member cannot be shown one figure and ranked on another.
> `war_fame_3season_avg` is the opposite case — still a column, but nothing
> reads it since the 3-season fame grace was removed from the kick path. `state_json` holds each machine's internals
(qualifying-week history, miss counters) so auto-withdraw and the parity of
"why is X eligible?" are answerable.

## 5. Defaults — ✅ ratified as drafted (Jamie, 2026-07-03)

| Constant | Default | Basis |
|---|---|---|
| ~~`DONOR_WEEK_MIN`~~ | *removed 2026-07-05* | donations are now a ranking input (percentile), not a filter |
| **Elder band** (2026-07-05) | | |
| `PROMOTE_TENURE_MIN` | 28 days | four-week hard filter before elder consideration |
| `WAR_FLOOR_DAYS` / `WAR_FLOOR_WINDOW` | 1 / 14 | war half of the competitive floor (≥1 finalized day in 14) |
| ~~`RANKED_FLOOR_LEAGUE`~~ | 4 (Champion) — **no longer gates** | ⟳ 2026-07-12: retained for DISPLAY only (the league *name*). The ranked floor is now participation |
| `RANKED_FLOOR_BATTLES` | 5 | ⟳ *added 2026-07-12* — ranked half of the competitive floor: ≥5 ranked battles in `WAR_FLOOR_WINDOW` |
| `RANKED_WEIGHT` | 0.40 | ⟳ *added 2026-07-12* — mutes ranked inside the competitive term; it fills the gap war leaves, never substitutes |
| `WAR_RATE_WINDOW` | 28 days | trailing window the per-day war credit is averaged over |
| `FULL_DAY_BONUS` | 0.25 | ⟳ *added 2026-08-04* — share of a war day's credit reserved for **finishing** it |
| `SCORE_W_WAR` / `SCORE_W_DONATION` | 0.65 / 0.35 | war-weighted rank blend; war is the core elder duty |
| `ELDER_BAND_FLOOR` / `ELDER_BAND_CEIL` | ~~0.15 / 0.20~~ → **0.20 / 0.30** | ⟳ retuned 2026-07-05; denominator changed 2026-08-01 to the **whole active roster, leadership included** (§3.3) |
| `WORTHINESS_MIN_PERCENTILE` | 0.50 | ⚠️ **decorative** — defined but referenced nowhere; the median test in `_elder_band` is hardcoded. Note also that worthiness now gates *all* growth, not just below-floor |
| `SWAP_MARGIN` | 0.05 | anti-flap deadband — a member displaces an elder only by out-scoring them this much, **or by winning a close call on tenure** (⟳ 2026-08-04) |
| `PROMOTE_QUALIFYING_WEEKS` | 3 | sustained weeks to promote / to swap out an outranked elder |
| `DEMOTE_WEEKS` | 2 | abandonment demotion (faster); outranked demotion uses the 3-week promote cadence |
| **Kick path (redesigned 2026-07-11)** | | |
| `WAR_QUALIFY_RATE` | 0.75 | (legacy signal; retained for evidence rendering) |
| `BATTLE_DAYS_MIN` | 8 (of 28) | ≈2 battle-days/week floor |
| `KICK_WATCH_DAYS` | 3 | early attention; no action |
| `KICK_AT_RISK_DAYS` | 5 | flat at-risk threshold (7 = in-game profile flag, 10 = clearly unmanaged) |
| `KICK_CONFIRM_DAYS` | 3 | battle-free days past at-risk before a card (plain card = 8 days) |
| `KICK_CONTRIB_GRACE_MAX` | 4 | max extra confirm days for an elder-floor contributor, **× open-slot slack** (0 at 50/50) |
| `ROSTER_CAP` | 50 | full clan; the slack denominator |
| ~~`NEW_MEMBER_GRACE`~~ | *removed* | no newcomer shield — same clock for everyone |
| ~~trophy buffer~~ | *removed* | `max(7, trophies/1000×1.4)` gone; flat 5-day at-risk |
| ~~`war_fame_3season_avg` grace~~ | *removed* | 3-season fame grace replaced by the live elder floor × slack |

These move into `CLAN.md`'s Thresholds section (the precedent home) **at the
cut**, alongside C5's prose update — `CLAN.md` is the live bot's prompt, so the
constants land when the engine that reads them does (migration.md Phase 5).
This doc's tables remain the reference for what they mean.

> ⟳ **That is not how it landed, and the difference is why this doc rotted.**
> The engine never reads its constants from `CLAN.md` or from here — it carries
> them as module constants in `engine/management.py`, and both documents are
> hand-copied prose that drifts silently. `CLAN.md` still claimed to be the
> source the engine reads until 2026-08-05, by which point it had
> `promote_qualifying_weeks: 4` (is 3) and `demote_weeks: 4` (is 2, and
> abandonment-only).
>
> **`engine/management.py` is the single source of truth.** Changing an elder
> constant is a four-file change: the engine, `prompts/POLICY.md`, and the two
> poapkings.com pages (`src/members.njk`, `src/faq.njk`). The comment above
> `ELDER_BAND_FLOOR` names them all, and `tests/test_cr_knowledge.py` asserts
> POLICY.md's prose against the imported constants so this class of drift fails
> a test instead of surviving for a month.

## 6. What this doc deliberately does not do

- **No scoring, no LLM judgment** — states in, leader actions out (§13.3).
- **No new metrics** — every input already exists in the schema
  (`member_management`, `war_attendance_days`, `battle_events`, roster state).
- **No cross-clan portability claims** — the defaults are POAP KINGS-shaped;
  `CLAN.md` is where another clan would retune them.
- **Warm-up accepted** (migration.md Phase 3): all machines start at
  `none`/zero at the cut; kick-risk is live within ~2 weeks, promotion
  eligibility within ~4–6.
