# Elixir v5.1 — The Pulse (broad-window stream commentary)

> **Status:** 🟡 Spec'd 2026-07-05 (Jamie: "this pattern may work well for
> player stream — the clan runs 24 hours a day with global players" — a
> post every 8 hours, 3 times a day). Build after the season-close weekend.
> **Owner:** Jamie · **Last worked:** 2026-07-05
>
> **The gap it fills:** recognition speaks one moment at a time (threshold
> 80); the weekly recap speaks once a week. Between them, ~100 battles and
> a handful of events per 8h window flow unnarrated. The Pulse is the organ
> that looks at the *shape of a window* and says something human about it.

## 1. The pattern

A **pulse** = one composed post narrating a bounded window of a stream,
grounded entirely in that window's rows. Not a moment (the ledger's job),
not a week (the recap's job) — a *window*. First instance: the player
stream. The pattern generalizes (war pulse, clan pulse) but each instance
is its own ratified decision — no speculative generality.

## 2. Why 8 hours (the cadence IS the feature)

Three windows a day, each covering a different third of the globe's active
hours — the EU evening, the Americas evening, the Asia/Pacific evening
each get their own narrated window, every day. The overnight-carriers whom
weekly summaries never mention become visible daily. Windows are
**anchored tiles**, not scheduled times: `next = anchor + 8h`, persisted;
restarts must NOT re-anchor (drift would collapse the even coverage).
Anchor at deploy such that boundaries land ~04:00 / 12:00 / 20:00 CT —
each post then reviews a window ending at a natural regional handoff.

## 3. The player pulse

**Window:** strictly `(anchor, anchor + 8h]` — every fact
cited must come from rows inside it. Windows tile perfectly; nothing is
ever narrated twice (chronicle discipline applied to short windows).

**Facts context (all from existing tables, zero new collection):**
- `player_events` in-window: level-ups, milestones, peaks, badges, PoL
  promotions (the sparse-but-real moments, including sub-threshold ones
  recognition never posted — the pulse is where quiet achievements get
  their nod)
- `battle_events`/`player_daily_battle_rollups` aggregates: total battles,
  distinct players active, mode mix, win-rate standouts (≥N battles),
  **around-the-clock texture** — activity by hour bucket, who carried the
  overnight hours (the global-clan story Jamie named; teammate pairs from
  `teammate_tag` when clan-internal)
- Playstyle identities (`engine/profiles.py`) for anyone spotlighted
- Timezone texture: which regions' hours this window covered (derivable
  from the window's UTC span — name the handoff, e.g. "the overnight shift")
- Ranked movement in-window (rating deltas for the UC cohort)
- **Battle spotlight** (Jamie: "highlight cool battles"): the window's single
  coolest battle by deterministic scoring — 3-crown sweeps, |trophy swing|
  ≥40, arena-up deciders, event-mode dominance, clan-duo 2v2 wins
  (`teammate_tag`, extraction fixed 2026-07-05); crowns/decks/mode all in
  `battle_events`. One spotlight per window — fun, volume-safe. Related but
  separate: arena_up posts in #player-highlights gain their deciding-battle
  facts (the moment plus the movie of it) — small compose-payload
  enrichment, not part of the Pulse.
- Suppressed-accrual highlights: claims banked but unposted in-window —
  "quietly building" material (peaks, anniversaries)
- Explicit exclusions: war-stream content (war has its own posts; one
  mention max as texture), management signals (never), anything outside
  the window

**Composition — the standard pipeline, not a side channel** (the
single-pipeline rule): the activity computes the facts JSON and raises a
`communication_intent` (`pulse:player_stream`, ledger key
`pulse:player:{window_end}`, lane = the new dedicated channel per P2); the next engine
tick composes (channel_update → Sonnet 5), **Editor-gates**, and delivers
it like any other post. At-least-once, 6h expiry, verdict trace — all
inherited. Not in the clan-chat relay allowlist (v1).

**Voice ask:** the compose ask frames it as "what I noticed lately" — a
clan member who's been watching, not a report. 3–6 sentences. Name 2–4
players max (rotation fairness: prefer players not named in the previous
two pulses — the context carries the recent-spotlight list). Numbers only
from facts. Deterministic fallback: totals + one standout line.

**Quiet windows:** at 3 posts/day, some 8h windows will genuinely be thin.
Post anyway (P1 — Jamie's cadence is fixed), but the ask legitimizes
brevity: a quiet window earns two honest sentences, not manufactured
excitement. The P4 review after week one is the checkpoint on whether
3/day reads as rhythm or noise in the channel.

## 4. Schedule & state

- New activity `player-pulse`: runs every 30 min as a cheap check (is
  `now >= anchor + 8h`? then build + raise intent and advance the anchor
  by exactly 8h — drift-free tiling even if checks are missed); anchor in
  `runtime_job_status.player_pulse.last_summary` (`anchor=<iso>`), seeded
  so boundaries land ~04:00/12:00/20:00 CT.
- Cost: 3× (Sonnet 5 compose + Haiku verdict) per day ≈ negligible.

## 5. Decisions

| # | Question | Recommendation |
|---|---|---|
| P1 | Always-post vs story-gate | **Always**, with brevity licensed on quiet windows (fixed cadence per Jamie; Editor substance gate is the floor; revisit at P4 if 3/day reads as noise) |
| P2 | Lane | **A NEW dedicated channel** (Jamie, 2026-07-05: "I think this may need a new channel") — the trophy-case/entertainment split: #player-highlights stays scarce earned recognition; the Pulse's 3/day rhythm + battle fun get their own home (#battle-feed or similar). Jamie creates it; wiring = DISCORD.md + compose lane + routing. This also resolves P1's volume concern. |
| P3 | Spotlight rotation memory | **Yes** — carry last-2-pulses' named players in context, prefer fresh names |
| P4 | Extend pattern to war/clan streams | **Not yet** — run the player pulse one full week (21 posts), review volume + voice in the editorial report, then decide |

## 6. Build plan

1. `runtime/jobs/player_pulse.py`: window query + facts builder (+ tests
   against fixture windows: tiling exactness, empty-window fallback,
   rotation-fairness list).
2. Intent raise + ledger claim; compose ask (`engine/recognition/compose.py`
   gains the `pulse:player_stream` ask + deterministic fallback).
3. Activity registration (hourly check) + anchor seeding.
4. Observatory: pulses appear in `/recognition` like any claim; add the
   anchor to `/ticks` job status view (free).
5. events.md §2 note: the pulse consumes battle/player rows, emits nothing
   — a reader, not a stream.
