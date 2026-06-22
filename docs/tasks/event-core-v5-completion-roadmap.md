# v5 Completion Roadmap — port the remaining v4 signals, then decommission v4

**North star (Jamie, 2026-06-21):** finish porting v4's reactive signal surface to
the v5 event-driven system, then **fully remove the old v4 signal/awareness system**
— leaving it alive is confusing (item 7). Each item below is decided; this is the
execution order, not a list of open questions.

Decisions are keyed to the review list Jamie answered.

## Done / in this batch
- **(1)** Member leave/promotion routing → #clan-events; demotions not posted. **Good as-is.**
- **(2a)** War update: richer + capped. DONE — `WarUpdateDetector` now fires once per
  active battle **day** (period_type=="warDay", keyed on period_index), payload
  carries fame/period_points/clan_score/day. ~1/day during the race, no
  training/off-season noise. **Ceiling:** standings-vs-rivals / who-hasn't-attacked
  need per-participant war capture (not evented yet) — see (2d-adjacent) below.
- **(3)** Dropped `battle_hot_streak` (the less-interesting twin of
  battle_trophy_push; was double-posting). Celebrate trophy/rank **movement**
  instead. Mode-aware movement (incl. PoL) folds into (2f).

## Sequenced backlog (committed)

**P1 — clean, events already exist**
- **(4) Enrich member_left + suppress kicks.** Add a name (+ last-known stats)
  snapshot to the MemberLeft detection so departure posts aren't a raw tag. AND
  do NOT post a departure if the member was **kicked** — cross-reference the
  leader-actions outcome (a kick recommendation that was acted on / a leader kick)
  for that player within a recent window; suppress the `member_left` intent if so.
  Requires connecting the leadership/leader-action outcome data to the detector.
- **(2b) Weekly donation leader → #clan-events.** Weekly only. Post the week's top
  donor(s). Timing: donations_week resets ~Mon; compute from the weekly window
  (member_daily_metrics) or post just before reset so the leader is real. Likely a
  small weekly scheduled emit of a `clan:` intent (agent-voiced), not a per-event
  detector.
- **(2c) Cake-day / birthday → #clan-events.** clan birthday, join anniversary,
  member birthday. Source is member_metadata (operational), so a daily scan that
  emits intents for today's anniversaries (once per item per year).

**P2 — bigger / needs capture**
- **(2f) Path-of-Legend — HIGH PRIORITY (lots of PoL players).** Currently BLOCKED:
  PoL data (league/rank/season result) is not in the event model (scalar-only
  ProfileObserved). Work: (i) capture PoL fields in profile ingest + a granular
  PoL-change event, (ii) detectors for PoL promotion/league-up, ultimate champion,
  global-rank attained, (iii) route → #player-highlights, (iv) fold into the
  mode-aware "movement" idea from (3). This is the core unfinished v5 promise.
- **(2d) Tournament family → #clan-events.** started/ended/lead-change/joined/etc.
  Tournament data is operational + command-driven; add detectors or a tournament
  observer that emits intents. Check whether the existing tournament-watch path
  still functions post-consolidation.
- **(2e) Opponent intel report.** Trigger **once after a new clan-wars SEASON is
  detected** (not monthly). Detect new season (season_id change in RiverRace) →
  fire the existing `generate_intel_report` → post to #river-race. Reuse the
  current intel workflow; just change the trigger to season-start.

**P3 — endgame**
- **(7) Decommission the v4 signal/awareness system.** Once the above cover the
  needed signals: remove the v4 heartbeat signal detectors, signal lanes/delivery,
  the disabled awareness jobs, and the now-dead tables (signal_detector_cursors,
  signal_outcomes, awareness_ticks, signal_log, etc.) + guard/retire their admin
  readers. This is the "stop running two systems" cleanup Jamie wants — gated on
  P1/P2 being live so nothing is lost.

## Not doing
- (5) paused automation — ignore. (6) disk cleanup — hold. POAP KINGS website —
  being removed.
