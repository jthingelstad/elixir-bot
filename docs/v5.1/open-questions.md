# Elixir v5.1 — Open Questions

> **Status:** ✅ **All decisions made** (Jamie, 2026-07-02). This doc is now the
> decision record the downstream docs cite.
> **Owner:** Jamie · **Last worked:** 2026-07-02
>
> This is the decision record for the build spec. Every open decision named in
> `architecture.md` (§13.7, §16.8, §17.6) lives here, plus decisions the architecture
> leaves implicit. Each entry carries the grounded facts it rests on, the options,
> a recommendation, and an explicit decision owner. Once a decision is made, the
> answer is recorded **in place** — downstream docs (`schema.md`, `events.md`,
> `recognition.md`, `runtime.md`, `migration.md`) cite entries by number (Q1…Q8)
> and never restate the rationale.
>
> Grounding discipline: every factual claim below was verified against the live
> `elixir-v5.db` and the codebase on 2026-07-02. Citations are file:line or a
> query result. Anything unverifiable is marked as such.

## Decision status

| # | Question | Source | Decision (Jamie, 2026-07-02) | Blocks |
|---|---|---|---|---|
| Q1 | Clan-management review cadence | §13.7 | ✅ **C** — weekly batch for promote/demote; reactive for kick-risk | `runtime.md`, `schema.md` |
| Q2 | War Champ criteria model | §16.8 | ✅ Always top fame; **Free Pass rotation rule** (see Q2) | `schema.md`, `recognition.md` |
| Q3 | Attendance nudge placement | §16.8 | ✅ **No nudge, anywhere.** Participation stays a private mgmt input | `recognition.md` |
| Q4 | POAP tie-in at season close | §16.8 | ✅ **None** — POAP platform is paused; design nothing | `events.md` |
| Q5 | Award engine design + surviving types | §17.6 | ✅ **B** — awards consume stream events; standings compute, awards record | `schema.md`, `events.md`, `runtime.md` |
| Q6 | Manual / non-API evidence | §17.6 | ✅ **Clan Voyages is dead — drop it from the system** | `schema.md`, `migration.md` |
| Q7 | Onboarding / verification & system signals | §17.6 | ✅ **A** — port-and-repoint, no redesign | `migration.md` |
| Q8 | Battle-stream retention window | §14.3 (implicit) | ✅ **≥3 war seasons** → **180 days** (see Q8 note) | `schema.md` |

Tracked constraints (design obligations, not decisions) are listed after Q8 as C1–C6.
C5 and C6 were created by the Q2 and Q6 decisions respectively.

---

## Q1. Clan-management review cadence

**Source:** `architecture.md` §13.7 — "Weekly batch review vs. reactive… Likely both."

**Grounded facts**

- Today's reactive machinery exists and is **live**: `leadership-action-scan` runs on
  a 240-minute interval with `enabled_by_default=True`
  (`runtime/activities.py:124–141`). *Note: `AGENTS.md` says this activity is
  "retained but disabled" — that is doc drift; the registry says enabled (see C4).*
- The scan (`runtime/jobs/_core.py:1129` `_leadership_action_scan`) already does three
  things v5.1 wants to keep: refreshes due action **outcomes**, re-queues **feedback
  synthesis** per action type, and gates posting through a **policy check**
  (`can_post_leader_action`, `runtime/leader_action_policy.py:68`) with a
  `critical` bypass and a per-scan action cap (`LEADERSHIP_ACTION_SCAN_MAX_ACTIONS`).
- The weekly grain is already the design's stated natural cycle: donations and war
  reset weekly (`architecture.md` §13.3).

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Weekly batch only | One leadership review post per week; all candidacies surface there | Predictable, low-noise; but a kick-risk member can idle 6+ days before leadership hears |
| B. Reactive only | Fire a leader action whenever a candidacy state machine transitions | Prompt; but promotions/demotions arrive as drips, and hysteresis (§13.3) makes most transitions non-urgent anyway |
| C. Both, split by urgency | Weekly batch for promote/demote; reactive for kick-risk (protective) transitions only, through the existing policy gate | Matches urgency to cadence; two paths to test instead of one |

**Recommendation: C.** Promotions and demotions are earned over N qualifying weeks
(§13.3 hysteresis) — nothing about them is urgent, and a weekly review is where
leadership already thinks in weeks. Kick-risk is the one protective signal where
waiting has a cost. Both paths emit the same leader-action types and flow through the
existing post-policy gate; the weekly batch is a scheduled activity, the reactive path
is a projection-refresh side effect. `runtime.md` will specify both.

**Decision:** ✅ **C** (Jamie, 2026-07-02). Weekly batch review for promotion/demotion
candidacies; reactive surfacing for kick-risk transitions only; both through the
existing leader-action post-policy gate.

---

## Q2. War Champ criteria model

**Source:** `architecture.md` §16.8 — how leadership declares per-season criteria.

**Grounded facts**

- Policy today is prose, not config: `prompts/CLAN.md:92` ("The top war performer each
  war season earns a free Pass Royale"), `CLAN.md:100–101` ("The season's top Clan Wars
  contributor is the War Champ… unless leadership announces a special variant"), and
  `CLAN.md:45` ("The exact free-pass criteria can change from season to season").
- Code today hard-codes the default: `_grant_war_champ` ranks by cumulative season fame
  from `get_season_awards_standings` (`heartbeat/_awards.py:140–172`;
  `storage/awards.py:640`). There is no variant mechanism at all.
- The variant Jamie cited (a past season: *only player who used every war deck on every
  battle day*) is computable — attendance/decks-used are in the standings inputs
  (`get_iron_king_candidates`, `storage/awards.py:375`, tracks perfect battle days).

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Criteria enum in config | `CLAN.md` declares `war_champ_criteria: top_fame` (default) or a named variant (`perfect_attendance`, …) with optional params; code computes every supported metric all season, applies the declared criterion at close | Declarative, portable, auditable; variants limited to what code supports (which is the point — §16.3 says math lives in code) |
| B. Per-season leadership declaration via leader action | Leadership announces the variant in-season; Elixir records it as engine state | Captures "announced mid-season" reality; adds a write path + state for something that changes ~once a year |
| C. Free-text criteria, LLM interprets | Maximum flexibility | Violates §16.3 ("math… lives in code, never the LLM"). Rejected. |

**Recommendation: A**, with a B-flavored escape hatch later if declaring in `CLAN.md`
proves annoying: the standings table stores **metrics** (fame, decks used, attendance,
perfect-attendance flag), never the winner; the winner is computed at season close
against the criterion declared in config. Editing one config line before season close
*is* the declaration mechanism — matches how thresholds already live in `CLAN.md:120–127`.

**Decision:** ✅ (Jamie, 2026-07-02) — **simpler than every option above.** There is
no variant mechanism at all:

1. **War Champ is always top fame.** Fixed criterion, in code. No config enum, no
   per-season declaration. (This supersedes the "special variant" language in
   `CLAN.md:101` and `architecture.md` §16.3 — see C5.)
2. **The Free Pass rotates.** The Free Pass Royale cannot go to the same player in
   **sequential seasons**. If the top-fame player also received the Free Pass last
   season, they are **still the War Champ** (the honor is unconditional), but the
   Free Pass goes to **2nd place** in the standings.

**Spec consequences (owned by `schema.md` / `events.md`):**

- War Champ (honor) and Free Pass recipient (reward) are **two distinct computed
  outcomes** of season close. They usually coincide; the rotation rule is the only
  divergence.
- The rotation check needs a **durable ledger of Free Pass recipients per season** —
  "who got the pass last season" must survive any retention window. Concretely: a
  `free_pass` award type alongside `war_champ` in the awards ledger (or a
  `free_pass` flag/column on the season-close record). The existing `awards` table
  history shows `war_champ` only, so this is a **new** durable record; the archive's
  `war_champ` rows seed it for the sequential check at the first post-cut season
  close (acceptable approximation: historically champ = pass recipient).
- Determinism edge: the rule needs exactly rank-1 and rank-2 of the final standings
  plus last season's pass recipient. No LLM involvement (§16.3 holds).

---

## Q3. Attendance nudge placement

**Source:** `architecture.md` §16.8 — public `#river-race` nudge vs. private management
signal; "likely both, from the same participation data."

**Grounded facts**

- There is **no public attendance nudge today**: the only attendance computation lives
  in leadership reporting (`runtime/helpers/_reports.py`; no other non-test module
  references attendance). So "public nudge" is new behavior, not a port.
- The tone constraint is already policy: once the weekly race is won, Elixir drops
  urgency — "completion, recognition, and clean closure," never guilt
  (`prompts/CLAN.md:80`; `architecture.md` §16.2/§16.4).
- The private side has a designed home: the "war-reliable" evaluator input
  (`architecture.md` §16.5).

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Private only | Participation feeds the management projection; no public nudge | Zero spam risk; but the clan's own tradition (War Champ race, free pass) loses its public heartbeat |
| B. Public only | War subagent nudges `#river-race` on battle days | Motivates; but chronic no-shows become public shaming, violating the tone rule |
| C. Both, different grains | Public: **clan-level** pace/remaining-decks framing on battle days, gated by the war clock (never during training, never after the race is won, never naming laggards). Private: **member-level** attendance into the war-reliable evaluator | Same data, two scopes; public stays positive-sum, names only appear privately |

**Recommendation: C.** The scope split is the key move: public nudges speak about *the
race* ("18 decks still on the table today"), private signals speak about *members*.
This satisfies both §16.8 halves with one participation projection and keeps the
never-guilt rule structural (the public path simply has no member-level payload).
`recognition.md` will encode the routing; the war clock (§16.2) gates timing.

**Decision:** ✅ (Jamie, 2026-07-02) — **drop the nudge entirely.** No public
attendance nudging in any lane. Participation data still feeds the private
"war-reliable" evaluator (§16.5) — that is management input, not a nudge, and is
unaffected. `recognition.md` designs **no** attendance-nudge routing; this supersedes
§16.8's "likely both" and narrows §16.5's "public nudge to use your decks" clause to
the private half only.

---

## Q4. POAP tie-in at season close

**Source:** `architecture.md` §16.8 — auto-propose a War Champ / season POAP, or keep
it a manual tradition.

**Grounded facts**

- POAPs are core identity but Elixir has **no POAP tooling**: "POAP records currently
  live on the website and related systems… Elixir should not claim to directly issue
  or manage POAP drops unless tools for that exist" (`prompts/CLAN.md:48–49`). No POAP
  issuance code exists in the repo (verified: no matches outside prompts/docs).
- Season close already produces a durable record: `war_champ` awards exist for 12
  seasons through season 132 (live DB, `awards` table).
- The Leader Action structure (§13.4) is the established human-in-the-loop surface,
  and season POAPs are a real tradition (`CLAN.md:39`: "We issue POAPs for seasons…").

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Fully manual (status quo) | Season close posts the recap; POAP stays a human tradition | No new machinery; relies on someone remembering |
| B. Auto-propose as a leader action | Season-close event triggers a `poap_proposal`-style leader action carrying the recap facts (champ, placements, dates) as design input | Keeps humans as the actor (consistent with §13.5 advisory-not-actuator); one new action type |
| C. Auto-issue via POAP API integration | Elixir mints the drop | New external integration, out of v5.1 scope, and contradicts `CLAN.md:49` today |

**Recommendation: B.** It is the same advisory pattern the whole management loop uses,
costs one action type plus payload plumbing on an event that already exists
(season-close), and turns "remembering the tradition" into a nudge with the evidence
attached. C is a separate future project if ever wanted. But this is a clan-tradition
call, not an engineering one.

**Decision:** ✅ (Jamie, 2026-07-02) — **none. POAP is paused and on hold; the POAP
platform itself is paused.** v5.1 designs no POAP tie-in: season close produces the
recap and the awards records only. If POAPs return, option B (auto-propose as a
leader action) is the pre-agreed shape. Note for prompt hygiene: `CLAN.md:38–49`
presents POAPs in present tense ("We issue POAPs for seasons…") — worth a wording
pass so Elixir doesn't promise paused traditions, but that is a prompt edit, not
v5.1 scope.

---

## Q5. Award engine — general design and surviving award types

**Source:** `architecture.md` §17.6 — "The doc designs War Champ (§16.3) but not the
general eligibility/grant engine."

**Grounded facts**

- The engine today (`heartbeat/_awards.py`) grants five types: `war_champ`,
  `iron_king` (perfect battle days), `donation_champ`, `rookie_mvp`, and
  `war_participant` (every member with season fame > 0; granted **silently** — rows
  only, no Discord post, `heartbeat/_awards.py:347–360`).
- Three types are deprecated with an active deletion path: `perfect_week`,
  `victory_lap`, `donation_champ_weekly` (`heartbeat/_awards.py:32`, cleanup at :408+).
- Cadence: the daily `award-detection` activity (`runtime/activities.py:90–103`) runs
  `_award_detection_tick` (`runtime/jobs/_core.py:393`), which is idempotent via
  signal keys `award_earned::{type}::{season}::{scope}::{tag}::r{rank}`
  (`heartbeat/_awards.py:45`) and gated on `season_is_complete`
  (`storage/awards.py:743`). New grants post directly to `#clan-events`.
- Live counts: `war_champ` 12, `iron_king` 9, `donation_champ` 12, `rookie_mvp` 12
  (through season 132); `war_participant` 135 (accruing in season 133).
- **Re-key ripple:** `insert_award` writes both `member_id` and `player_tag`
  (`storage/awards.py:64`) — under §7 the `member_id` column drops.

**The structural question.** §16.3 designs War Champ standings *inside the bounded war
stream*; the award engine *also* computes War Champ. Two implementations of one concept
is the Gen A/B/C failure in miniature (C-risk flagged in the planning discussion).

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Keep a separate daily award scanner | Port `_award_detection_tick` onto new tables | Familiar; but War Champ math lives twice (standings + awards), and "daily poll for a ~monthly event" is scan-shaped, not event-shaped |
| B. Awards as consumers of stream events | Season awards subscribe to the war stream's **death/recap event** (§16.1); `war_participant` accrues from the participation projection; the standings projection (§16.3) is the **single computation**, awards are its durable record at close | One computation source; event-shaped (grant fires when the season actually dies); requires the bounded-stream lifecycle events to exist first (they do — §12/§16.1) |
| C. Fold awards entirely into the war stream | The war subagent grants awards | Wrong home — `donation_champ` and `rookie_mvp` are not war-only concepts; couples a general ledger to one bounded stream |

**Recommendation: B**, stated as a rule for `schema.md`/`events.md`: **standings
projections compute; the awards ledger records.** War Champ = read the §16.3 standings
at the season-death event and write one durable row. `iron_king`/`rookie_mvp`/
`donation_champ` read their own season aggregates at the same event. `war_participant`
stays a silent accrual from the participation projection (its current no-post behavior
is correct and worth preserving — `heartbeat/_awards.py:352–355`). The five current
types survive; the three deprecated types are **dropped at the cut** (their rows are
already actively deleted, so there is nothing to carry). Grant idempotency keeps the
deterministic-key discipline (§8) and grants flow to `#clan-events` as recognition-
ledger-claimed events like any other moment.

**Decision:** ✅ **B** (Jamie, 2026-07-02), as recommended. Standings projections
compute; the awards ledger records; season awards fire on the war stream's
death/recap event; `war_participant` stays a silent accrual; the three deprecated
types drop at the cut. Q2's rotation rule adds the `free_pass` record to the same
season-close grant pass.

---

## Q6. Manual / non-API evidence

**Source:** `architecture.md` §17.6 — Clan Voyages and arena-relay screenshots are
"clan reality the CR API never exposes"; the stream model has no home for them.

**Grounded facts**

- Storage exists: `storage/clan_voyages.py`, `storage/screenshot_observations.py`.
- Live volume is **tiny**: 1 voyage, 42 voyage entries, 1 arena-relay screenshot
  observation — all from a single day, 2026-06-14 (live DB). This is three weeks old,
  so the features are recent, not dead — but used exactly once.
- The arena-relay screenshot path is wired into channel routing:
  `runtime/channel_router.py` special-cases leader-posted screenshots in
  `#leader-actions` and replies with an observation readout (`AGENTS.md`, verified
  in the router).
- The §8 event envelope already carries an `evidence` field, and §14.2's layers are
  source-agnostic below the raw-log layer.

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Manual events in the clan stream | Screenshot/voyage ingestion emits typed clan-stream events with `evidence.source = manual` (vs `api`); baselines/rollups treat them uniformly | One pipeline, recognition sees them like anything else; slightly widens the clan stream's meaning from "roster poll" to "clan reality" |
| B. A bounded manual stream per voyage | Each voyage is a bounded stream (§12) | Honest lifecycle fit (a voyage is time-boxed); heavy machinery for an event used once so far |
| C. Side tables outside the stream model (status quo) | Keep dedicated tables, port them | No design work; but recognition can't see manual moments, and §17.6's gap stays open |

**Recommendation: A**, with B available later if voyages become a regular tradition
(the bounded-stream machinery will exist regardless). The envelope change is one enum
value; the voyage/screenshot tables become the *evidence detail* the events point to,
which is exactly the evidence pattern the rest of the engine uses. Timing honesty (§8)
applies: manual events are `estimated` unless the screenshot shows a timestamp.

**Sub-question for Jamie (blocks the schema either way):** is **Clan Voyages an
ongoing practice** worth first-class support, or was 2026-06-14 a one-off experiment?
If one-off: drop the tables at the cut, the cold archive (§14.4) keeps the record, and
manual evidence is designed for screenshots only.

**Decision:** ✅ (Jamie, 2026-07-02) — **Clan Voyages is a dead end. Drop it from the
system.** Consequences (tracked as C6):

- `clan_voyages` / `clan_voyage_entries` tables: **not carried forward**; the cold
  archive (§14.4) keeps the record. `storage/clan_voyages.py` and the ingest path
  are deleted at the cut.
- The `get_clan_voyage` query tool is **removed** from the read-layer port — one
  fewer tool in the §14.5 coverage matrix.
- With voyages gone, the manual-evidence gap (§17.6) loses its main driver. What
  remains is the **arena-relay screenshot readout** — an interactive channel-router
  behavior (leader posts a screenshot in `#leader-actions`, Elixir replies with an
  observation), not an engine stream. v5.1 keeps that *behavior* as-is and ports its
  tiny observation table as an ops singleton (Part I §4 class); no manual-evidence
  stream is designed. Option A above is shelved unless a real manual-evidence need
  returns.

---

## Q7. Onboarding / verification & system signals — port or redesign

**Source:** `architecture.md` §17.6 — "deferrable, but schema-coupled like the query
tools."

**Grounded facts**

- System signals are small and self-contained: `runtime/system_signals.py` reads/writes
  a single `system_signals` table (:305, :318); startup seeding is idempotent
  (`queue_startup_system_signals`, per `AGENTS.md` System Signals section).
- Onboarding/verification (`runtime/onboarding.py`, `reception` workflow) touches
  identity tables (`members`, `discord_links`) — which §7 re-keys. So it cannot be
  ignored entirely: its identity reads/writes must repoint to `player_tag` keys even
  if nothing else changes.
- Both are classic "ops singletons" in Part I §4's taxonomy — independent,
  low-coupling, not entangled with the engine generations.

**Options**

| Option | Shape | Tradeoffs |
|---|---|---|
| A. Port-and-repoint at the cut | Keep behavior identical; update identity FKs to tags; `system_signals` table carries over as-is | Minimal scope; onboarding UX untouched (it works today) |
| B. Redesign alongside v5.1 | Fold verification into the clan stream (a join event triggers onboarding) | Cleaner conceptually; expands v5.1 scope into the deferred conversation/UX territory (§0) |

**Recommendation: A.** Redesigning reception drags v5.1 into the interactive-lane UX
that §0 explicitly defers. The migration cost is a mechanical FK repoint, which
`migration.md` will list in its checklist. This is an appetite question though —
if you've wanted to rework onboarding anyway, B is the moment.

**Decision:** ✅ **A** (Jamie, 2026-07-02) — port-and-repoint, no redesign. Context
worth preserving for future design work: **at most half the players use Discord**,
which is why in-game clan-chat messaging (`runtime/clan_chat_copy.py`) is so
important a channel. Onboarding works; don't touch it beyond the §7 FK repoint.

---

## Q8. Battle-stream retention window

**Source:** `architecture.md` §14.3 — "`battle_telemetry` is in *no* purge target and
grows unbounded… whichever survives needs a **deliberate** retention choice." The
architecture demands the choice but never makes it.

**Grounded facts**

- Verified: `battle_telemetry` is absent from `_PURGE_TARGETS`
  (`storage/metadata.py:391–408`); its legacy twin `member_battle_facts` is purged at
  `SNAPSHOT_RETENTION_DAYS` (30d, `storage/metadata.py:397`).
- Live volume: 10,115 rows spanning 2026-06-07 → 2026-07-02 (~25 days) ≈ **~400
  battles/day clan-wide**. A 30-day window is ~12k rows — trivial for SQLite.
- **Battlelog depth (resolves §17.7's "verify exact battlelog depth"):** across the
  last 400 `player_battlelog` payloads in `raw_api_payloads`, the entry count is
  **mode 30, range 12–59** (max ever observed: 59). Not the 25 commonly assumed. The
  window varies per player — treat **~30 most-recent battles** as the planning number,
  with no guarantee above that. At clan-wide ~400 battles/day, poll cadence — not
  storage — is the capture constraint, which is what adaptive polling (§15) spends
  its budget on.

**Options**

| Option | Window | Tradeoffs |
|---|---|---|
| A. 30 days | Matches the legacy precedent and every other snapshot store | Consistent; form/recent-battle reads never need more; durable history is the rollup layer's job anyway (§14.2 L4) |
| B. 90 days | More raw lookback for the data analyst | Duplicates what rollups + the 14-day raw log already serve; invites tools to lean on fine-grained data that will eventually age out |
| C. Season-aligned (~35–40d) | One full war season of raw battles | Cute, but seasons vary (4–5 weeks) and war battles are separately projected by the bounded war stream (§16.5) |

**Recommendation: A — 30 days**, with the standing rule from §14.2 made explicit in
`schema.md`: any read that needs more than 30 days of battle history is by definition
a **rollup read**, and if a rollup doesn't exist for it, that's a missing rollup, not
a retention bug. The recognition ledger's retention stays **durable** per §10/§14.2
(feedback New-4: "it's tiny, so durable is also fine").

**Decision:** ✅ (Jamie, 2026-07-02) — recommendation **overridden**: the window must
hold **≥3 war seasons of battles**; Jamie's stated range was "90 or 180 days."

Recorded value: **180 days** (`BATTLE_EVENT_RETENTION_DAYS = 180`). The math behind
picking the upper value: seasons run 4–5 weeks (§16.1), so 3 seasons = 84–105 days —
**90 days can clip a third 5-week season**, while 180 guarantees 3 full seasons
(≈5–6 typical seasons) with margin. Volume is a non-issue either way: at the observed
~400 battles/day clan-wide, 180 days ≈ 72k rows. If a tighter window is ever wanted,
120 days is the minimum that still guarantees 3 five-week seasons — but 180 is the
decision unless Jamie revises.

The §14.2 rollup rule adjusts accordingly: reads beyond **180** days of battle
history are rollup reads. The recognition ledger stays durable, as recommended.

---

## Tracked constraints (obligations, not decisions)

These are not open questions — they are couplings and fixes the downstream docs must
carry. Listed here so they don't fall between documents.

### C1. Kick-suppression re-host (§17.5)

`MemberLeftDetector._was_kicked` suppresses a "member left" post when a
`kick_recommendation` leader action with `status='done'` exists within
`_KICK_SUPPRESS_DAYS = 14` (`event_core/mind/detectors.py:542, 568–592`). Its
enrichment query also joins `members` ↔ `member_current_state` on `member_id` —
Gen-coupled and re-keyed by §7. **Owner: `recognition.md`** (the member-left event's
recognition must consult leader-action state) **and `runtime.md`** (ordering: the
leader-action projection must be current before clan-stream recognition runs).

### C2. Detection-type → event-type mapping

The ported scorer's constants are keyed by Gen C detection types
(`_PLAYER_HIGHLIGHT_BASE_SCORES`, `_PLAYER_HIGHLIGHT_BYPASS_TYPES`,
`PLAYER_HIGHLIGHT_THRESHOLD = 80` — `event_core/mind/communication.py:71–89`). The new
streams emit new event types. **`recognition.md` must contain the explicit three-column
mapping (old detection_type → new event_type → base score/bypass)** so no tuned score
is silently dropped. `events.md` owns the new namespace; `recognition.md` owns the
mapping.

### C3. `clan_war_log` → `riverracelog` alias fix

`cr_api.py:24` stores riverracelog payloads under the legacy entity key
`clan_war_log` (confirmed in the live raw log: 3 `clan_war_log` payloads, 237
`currentriverrace`). Per feedback New-1, the new raw log records the true endpoint
name. **Owner: `migration.md`** (cutover step).

### C4. Doc drift: `leadership-action-scan` enabled state

`AGENTS.md` says the activity is "retained but disabled"; the registry says
`enabled_by_default=True` on a 240-minute interval (`runtime/activities.py:124–141`).
Fix alongside the §17.8 drift items. **Owner: `migration.md`** cleanup list (or fix
immediately — it's a one-line doc edit).

### C5. Free Pass rotation rule — policy propagation (from Q2)

The Q2 decision (War Champ always top fame; Free Pass never to the same player in
sequential seasons, falling to rank 2) is stated **nowhere** today: `CLAN.md:101`
says the champ earns the pass "unless leadership announces a special variant," and
`architecture.md` §16.3 says "criteria are policy and vary by season." Both are
superseded. **Owners:** `schema.md` (durable `free_pass` record per season, seeded
from archived `war_champ` rows), `recognition.md` (season-close composition
distinguishes honor from reward when they diverge), and a `CLAN.md` prose update so
Elixir answers "who gets the free pass?" correctly.

### C6. Clan Voyages teardown (from Q6)

Drop at the cut: `clan_voyages` / `clan_voyage_entries` tables (cold-archived, not
carried), `storage/clan_voyages.py`, the voyage ingest path, and the
`get_clan_voyage` query tool (shrinks the §14.5 coverage matrix by one). The
arena-relay screenshot readout behavior is retained; its observation table ports as
an ops singleton. **Owners:** `schema.md` (exclusion list), `migration.md` (teardown
checklist).

---

## Appendix — verifications performed for this doc (2026-07-02)

| Claim | Method | Result |
|---|---|---|
| Battlelog depth | Parsed last 400 `player_battlelog` payloads from `raw_api_payloads` | mode 30 entries, range 12–59; max ever 59 |
| `battle_telemetry` unbounded + volume | `_PURGE_TARGETS` inspection + row count | absent from purge list; 10,115 rows / ~25 days |
| Award types + counts | `awards` table group-by | 5 live types; 12/9/12/12 season awards through s132; 135 `war_participant` (s133) |
| Deprecated award types | `heartbeat/_awards.py:32` | `perfect_week`, `victory_lap`, `donation_champ_weekly` — active deletion path |
| `awards` dual-keying | `storage/awards.py:64` | writes `member_id` **and** `player_tag` |
| War Champ policy prose | `prompts/CLAN.md:45, 90–102` | criteria variable per season; default top contributor |
| Kick suppression | `event_core/mind/detectors.py:542–592` | 14-day window; reads `leader_action_recommendations` status='done' |
| Scorer constants | `event_core/mind/communication.py:71–89` | threshold 80; base-score + bypass dicts present |
| Clan Voyages recency | `clan_voyages` / `clan_voyage_entries` / `arena_relay_screenshot_observations` counts | 1 / 42 / 1 rows, all 2026-06-14 |
| `leadership-action-scan` state | `runtime/activities.py:124–141` | `enabled_by_default=True`, 240-min interval (contradicts AGENTS.md) |
| Raw-log endpoint mix | `raw_api_payloads` group-by endpoint | player 3177, player_battlelog 1800, clan 856, currentriverrace 237, clan_war_log 3 |
