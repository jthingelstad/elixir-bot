# Elixir v5.1 — Architecture Design

> **Status:** Implemented. This doc preserves both the pre-cut diagnosis and the
> resulting design. **Part I** is a historical snapshot of `elixir-v5.db` on
> 2026-07-02, not a description of the current runtime. **Part II**, together
> with the implementation amendments below and the sibling reference specs, is
> the v5.1 architecture of record.
>
> **Scope at design time:** v5.1 was the **recognition + decision engine**, including the
> **read/query layer** that answers member/leader questions (§14.5) and the
> **deterministic notability core** (§10). Deferred to a later pass: the
> **conversation & memory design** — subsequently implemented and documented in
> `memory.md`.
>
> **Owner:** Jamie · **Last reviewed:** 2026-07-15

> **Implementation amendment (2026-07-14):** the clean break is complete and
> production now has two explicit runtime stages. `engine.tick.run_tick` is a
> five-step materializer (poll → ingest → emit → project → manage). The unified
> awareness loop is the sole proactive consumer: it reads emitted streams by
> durable per-stream cursors, plans across the whole situation, sends, records
> `awareness_posts`, and atomically acknowledges cursor checkpoints with its
> thought. The deterministic recognizer/`communication_intents` delivery chain
> is isolated behind `engine.legacy_proactive` for offline rehearsal only.
> Multi-table roster and season-close transitions are derived as explicit
> change sets and must satisfy postconditions before their baselines advance.
> Post-cut schema changes are ordered in `db/schema.py`; runtime modules do not
> mutate schema on first use.
>
> **Read-layer amendment (2026-07-15):** product surfaces no longer assemble
> high-level domain meaning independently. Versioned contracts in
> `capabilities/` sit between storage/projections and tools, awareness, reports,
> memory, and admin reads. They currently own game-mode, war, member,
> management-decision, and awards semantics. These are read-only contracts:
> external API refresh remains ingress work, and deterministic engine policy
> (especially `member_management`) remains authoritative rather than being
> re-evaluated in the capability layer.

---

## 0. Purpose

v5.1 is the next re-architecture of Elixir's engine — the fourth or fifth pass at
how Elixir turns Clash Royale into awareness, recognition, and clan management. Each
prior pass taught us more about how the CR API behaves and where Elixir gets stuck.
The recurring failure mode has been **additive** re-architecture: every new engine
was layered on top of the last rather than replacing it. The result is a database
where a newcomer (including Elixir itself) can't tell the spine from the sediment.

Before designing the target, we document the ground truth: what exists, what's
entangled, and what's actually fine.

**Scope boundary.** v5.1 is the **recognition and decision engine** — ingest, the
three activity streams, recognition (including its deterministic notability core, §10),
clan management, the bounded streams (tournaments, Clan Wars), and the **read/query
layer** that answers member and leader questions (§14.5). The read layer is *in* scope
because the tools that answer questions are the read side of the engine, and they are
coupled to the very tables v5.1 replaces (§17.1) — they cannot be deferred without
taking Q&A offline.

What v5.1 **defers** is narrower: the **conversation and memory design** — curated
`clan_memories` (including the Part I §3 versions-vs-audit question), conversational
memory (`messages`, `memory_facts`, `memory_episodes`), and the interactive-conversation
UX of the `#clan-chat` / `#ask-elixir` lanes. The query *tools* are ported now; the
conversational *experience* and curated-memory redesign come later. Where recognition
touches "insight" (§10), that is a named *seam* to that future pass, not a subsystem
v5.1 designs.

---

# Part I — Pre-cut state (historical, 2026-07-02)

## 1. Three engine generations were layered

Before the clean break, three generations of data flow were layered on top of
each other in the live database. This was the source of the "what is all this?"
feeling — and, more importantly, of Elixir reasoning against **different models
at once**. The table and evidence below describe that retired database only.

| Gen | What it is | Core tables | Status in reality |
|---|---|---|---|
| **A** | v4 **event store** | `game_event_stream` → `event_rollups` | Retired at the v5.1 clean break. |
| **B** | v4 **signal / delivery** layer | `signal_log`, `signal_outcomes`, `awareness_ticks`, `revisits` | Retired at the v5.1 clean break. |
| **C** | **Event Core** (v5) | `detections` → `elixir_projects` / `decision_cases` → `communication_intents` → delivery | Retired at the v5.1 clean break; selected deterministic logic survives only in the offline legacy seam. |

The seams that prove they're entangled rather than cleanly superseded:

- **Gen C is chained to Gen A.** `project_event_links` (3 rows) and
  `communication_intent_event_links` (96 rows) both declare
  `event_id INTEGER REFERENCES game_event_stream(event_id)`. The current engine's
  evidence links point back into the "dead" v4 event store.
- **Two delivery models run in parallel.** `signal_outcomes` (Gen B) vs.
  `communication_intents` (Gen C), stitched by an added `intent_id` column.
- **The same concept exists twice in code.** `communication_intent` and
  `decision_case` each have two implementations: `storage/communication_intents.py`
  (~29 KB) and `storage/decision_cases.py` (~25 KB) for Gen B, **and**
  `event_core/domain/communication_intent.py` and `.../decision_case.py`
  (~2 KB each) for Gen C. Elixir doesn't have one notion of "an intent"; it has two.

Roughly **18 tables doing the work of maybe 6–8**, plus duplicated code paths for
the same ideas.

## 2. Snapshot sprawl — the per-aspect pattern

About **15 tables carry a `raw_json` column** (verified), and they follow one
pattern: *a typed, denormalized API dump of an entity over time.* Each new "aspect"
of a member/war/clan got its own snapshot table. The pattern itself is fine; the
duplication is not.

The tells:

- **Battles are stored twice.** `member_battle_facts` (legacy) and Event Core's
  `battle_telemetry` are near-duplicates of the same battles, just keyed
  differently (`member_id` vs `player_tag`). They also disagree on *lifetime*:
  `member_battle_facts` is pruned at 30 days, while `battle_telemetry` is in no purge
  target and grows unbounded. No single source of truth for "a battle happened," and
  no agreed retention for it.
- **War is stored several times over.** War spans **6 tables**
  (`war_current_state`, `war_day_status`, `war_participant_snapshots`,
  `war_participation`, `war_period_clan_status`, `war_races`).
  `war_participant_snapshots` (raw time-series) substantially overlaps
  `war_participation` + `war_day_status`.

The `raw_json`-bearing set today:
`clan_daily_metrics`, `clan_voyage_entries`, `game_mode_contexts`,
`member_battle_facts`, `member_current_state`, `member_state_snapshots`,
`messages`, `prompt_failures`, `tournament_battles`, `war_current_state`,
`war_day_status`, `war_participant_snapshots`, `war_participation`,
`war_period_clan_status`, `war_races`.

Candidate direction for 5.1: **centralize the raw capture, don't delete it.** The
raw JSON is a *deliberate and valuable* record — the data analyst mines it to detect
new signals and new game modes — so it stays, but it belongs in the single
API-response log (`raw_api_payloads`, which already exists), not denormalized into 15
derived tables. Then adopt **one consistent observation convention** and collapse the
duplicate battle/war tables to a single source of truth per entity. See §14.

## 3. `clan_memory` is a mini-CMS

`clan_memory` is one logical entity with **~20 database objects** around it: roughly
ten real satellites (`clan_memory_tags`, `clan_memory_tag_links`,
`clan_memory_member_links`, `clan_memory_event_links`, `clan_memory_evidence_refs`,
`clan_memory_embeddings`, `clan_memory_index_status`, `clan_memory_audit_log`,
`clan_memory_versions`, …) **plus** a full FTS5 index (`clan_memories_fts` + its
shadow tables) **and** a sqlite-vec index (`clan_memory_vec` + its shadow tables).

Tags, member-links, and evidence are defensible for a knowledge layer. The heavy
part is carrying **full `clan_memory_versions` *and* a separate
`clan_memory_audit_log`**. Open question: do memories genuinely need version history,
or just an audit trail? One of those two probably goes. **Deferred:** the
memory/knowledge redesign, including this question, is out of v5.1's scope (§0) and
belongs to the later memory pass.

## 4. What's actually fine — don't refactor for its own sake

Not everything is sediment. These are correct and should be left alone:

- **Identity model (sound, but re-keyed).** The member ↔ Discord linkage
  (`members`, `member_metadata`, `member_aliases`, `discord_users`,
  `discord_links`) with a **confidence score** on the link is good design. The
  *model* stays; v5.1 **re-keys** it to CR tags — the synthetic `member_id` is dropped
  and the immutable player tag becomes the natural key (§7).
- **Tournaments.** A tidy 3-table star. Fine.
- **Ops / catalog.** Looks like "so many tables," but they're independent,
  low-coupling singletons (ingest buffers, reference/catalog data, `llm_calls`, job
  status). They inflate the table count without adding entanglement. Cheapest thing
  to ignore — or relocate to a side DB if we want the primary lean.

## 5. The shape of a good outcome

Today: **75 designed tables** (plus 11 FTS/vec shadow tables and `sqlite_sequence`
no one designed — 86 objects total), three engine generations, duplicated observation
storage, and a memory subsystem heavier than its use. If 5.1:

1. **collapses to a single engine generation**,
2. **standardizes / deduplicates observation storage**, and
3. **defers the memory/knowledge layer to its own pass** (§0),

we plausibly land around **35–45 designed tables with zero capability loss** — the
engine cut (levers 1–2) landing in v5.1 and the memory-layer reduction in the later
pass — and, more importantly, a schema where the **spine is legible from the
sediment**. That legibility, not the table count, is the real goal: it's what lets
Elixir reason against one model instead of three.

---

## 6. Operating constraints — and the freedom they grant

The most important context for how aggressively we can fix the above:

- **Hobby project, not a business.** No SLA, no customers.
- **Downtime is acceptable.** The engine can go dark for a while.
- **No backwards compatibility required.** No external contracts to honor.
- **One user, one developer.** Jamie is the only operator and the only author.

Together these license a **clean break**. We do not have to keep old generations
alive to protect anyone; we can delete, reset schema, and rebuild from source data
without a compatibility layer.

> **Principle — Clean break.** Every generation we move away from gets *deleted*,
> not bridged. Dormant-but-still-referenced is exactly how we ended up with three
> engines. If v5.1 adopts a model, the previous models' tables and code are removed
> in the same arc.

Two directives already on record that any v5.1 plan should satisfy (Part II will
resolve how):

- **Adopt the Event Core (Gen C) pipeline *shape*** —
  `observation → detection → project/case → communication intent → delivery` — as
  the single data flow, and delete Gens A and B.
- **Shed the Event Core's rigidity.** The `eventsourcing` framework (an `Aggregate`
  per domain, Followers with tracked log positions, deterministic-UUID replay,
  strict lifecycle state machines) is heavier than this project needs. Keep
  idempotency and causal evidence as *conventions*, not as a framework to bend to.

---

# Part II — The v5.1 target architecture

The engine is organized around the game itself. Everything Elixir reacts to falls
into **three activity streams** — battle, player, and clan — fed by two kinds of
source signal.

## 7. Two kinds of signal, three streams

The CR API gives us exactly one thing that is already an event, and two things we
can only ever see as current state:

| Stream | Source API | Native shape | How we get events |
|---|---|---|---|
| **Battle** | player battle log | **already an event stream** — every battle, per player, with a real timestamp | mirror battles in directly; no derivation |
| **Player** | player profile (cards, levels, achievements, badges, trophies) | **current state only** | **emit** events by diffing successive observations |
| **Clan** | clan + roster (members, roles, clan score, war league/section) | **current state only** | **emit** events by diffing successive observations |

**These are three separate streams, by design.** Each has its own event
log / table, its own **grain** (a battle, a player-state change, a clan-state
change), and its own **retention interval** — high-volume battles are summarized and
aged out faster; rare clan events are kept long. They are independent in storage;
any relationship *between* them (e.g. a battle that causes an arena-up) is resolved
in recognition, not in the schema.

Two definitions we hold to, because the current codebase blurs them:

- **Player** = a Clash Royale account (tag, cards, levels, achievements). The player
  stream is about **account progression**.
- **Member** = a player who is *currently in POAP KINGS*. Membership itself
  (join / leave / promote) is a **clan-stream** concept.

So the old sketch's "member events" split: a member *leveling up* is a player event;
a member *joining* is a clan event.

*Reconciling with the schema:* today's `members` table holds **any CR player ever
seen** — keyed by a synthetic `member_id`, with a `status` column and rows kept for
players who have left; tenure lives in `clan_memberships`. Under these definitions a
departed player is still a *player* but no longer a *member*.

> **Principle — CR tags are identity.** The immutable Clash Royale **tag is the
> natural key**, not a synthetic id. v5.1 drops `member_id`; `player_tag` becomes the
> primary key of `players` (renamed from `members`), and FKs across the schema move
> from `member_id` to `player_tag`. Name is just metadata on the tag. The same rule
> applies to every tag-bearing entity the API gives us — **player tag, clan tag,
> tournament tag** — so our clan and the four River-Race opponents (and every
> tournament) store in one shape, with no id↔tag translation layer.
>
> The rule, stated once: **if the CR API identifies it with a tag, the tag is the key;
> otherwise a synthetic id is fine.** Internal-only entities with no CR tag — messages,
> detections, `clan_memories`, decision cases, the recognition ledger — keep their own
> ids. Discord identity is already natural-keyed (`discord_user_id` is an immutable
> snowflake), so `discord_links` simply becomes `discord_user_id ↔ player_tag`.

This is not a cosmetic rename — it re-keys the schema. It also *simplifies*: Part II is
already tag-native (emitter dedup keys `player_level_up:{tag}`, the recognition ledger
`arena_up:{tag}`, `war_participation.player_tag`, `clan_daily_metrics.clan_tag`,
`tournaments.tournament_tag`), so this aligns persistence with what the engine already
assumes, and it makes the **tag-keyed `battle_telemetry` the canonical survivor** of the
battle-table duplication (Part I §2). *(Perf footnote: SQLite's fast
`INTEGER PRIMARY KEY` rowid is lost on a `TEXT` PK; negligible at this scale, but a hot
table may keep an internal rowid as long as the **tag stays the logical/foreign key** —
never reintroduce a semantic synthetic id.)*

## 8. The emitter pattern

Player and clan are the same problem — *we only see state, we want events* — so they
share one mechanism. An **emitter** turns a sequence of state observations into a
stream of typed events.

**Contract (deterministic, no side effects):**

```
emit(entity, aspect, previous_state | None, observed_state, observed_at) -> [Event]
```

Given the last known state of one aspect of one entity and a fresh observation, an
emitter returns zero or more events describing what changed. Same inputs → same
outputs.

**Design rules:**

- **The baseline store is a single row per (entity, aspect).** To diff, an emitter
  needs only the *last known state*, not a full history. This is the collapse of
  Part I's snapshot sprawl: the ~15 `raw_json` snapshot tables become one
  current-state baseline used only for diffing. Durability is a *separate* layer: the
  fine-grained event stream is a rolling window, and the **durable history is the
  rollup layer plus the rare long-retention events** (§14). Historical state cannot be
  re-fetched — the CR API only returns current state — so what we choose to roll up is
  what we keep.
- **First-sight emits nothing.** If `previous_state is None`, record the baseline and
  emit no events. Otherwise the first observation of a long-lived account backfills a
  flood of milestone posts. (This is a hard-won lesson already encoded as guard
  logic in the current detectors.)
- **Events are idempotent by identity.** Each event has a deterministic dedup key
  from `(stream, entity, event_type, natural-key-of-change)` — e.g.
  `player_level_up:{tag}:{level}`. Re-processing an observation never double-emits.
- **Timing is honest.** Battle events carry a true `occurred_at`. Emitted events only
  know the change happened in the interval `(previous_observed_at, observed_at]`, so
  they carry `observed_at` and are marked **estimated**. This governs how Elixir is
  allowed to speak ("recently" for emitted, "just now" for battle).

**Unified event envelope (all three streams):**

```
id / dedup_key   stream (battle|player|clan)   event_type
subject_tag      occurred_at (exact | estimated)   observed_at
evidence         (battle id, or the observation that revealed the delta)
payload          (typed facts — presentation-free)
scope            (public | leadership)
```

This keeps the parts of the v5 Event Core that were *right* — deterministic
idempotency, causal evidence, presentation-free facts, scope separation — as plain
conventions, without the `eventsourcing` framework (aggregates / followers / replay).

## 9. The three streams

### 9.1 Battle stream — native

The only natively event-shaped signal. Battles are recorded per player with real
timestamps and full resolution (deck, opponent, crowns, trophy change, game mode).
We mirror them in as-is; the battle stream needs no emitter.

Because battles are **causally upstream** of most player and clan movement (trophies,
arena, wins all move *because of* battles), the battle stream is also the
cross-check: when a battle and a polled delta describe the same real event, the
battle is the source of truth.

**Owns (per-battle moments):** Arena Up, notable wins (e.g. big trophy pushes, crown
records), streak-defining games.

### 9.2 Player stream — emitted

Fed by the player-profile poll; each aspect gets an emitter that diffs current state.

**Owns (cumulative / collection milestones):** level-ups, card unlocks (legendary /
champion), card-level milestones, collection milestones, career-wins milestones,
achievements / badges earned, best-trophies peaks.

**Cost note:** profile polls are **per-player** and therefore expensive; cadence and
which accounts we deep-poll (members always; prospects/opponents sometimes) is a real
constraint on this stream.

### 9.3 Clan stream — emitted

Fed by the clan + roster poll (one call for the whole clan); emitters diff clan-entity
and per-member roster state.

**Owns (membership + clan-entity progression):** member joins, member leaves (when
not a kick), promotions / demotions, clan trophy / score movement, clan moving into a
new league / arena tier, clan milestones.

## 10. Recognition — scoring in code, voice in the subagent

Recognition splits into three jobs, and only the last is the LLM's — the same
discipline v5.1 uses for clan management and Clan Wars (deterministic math in code,
narration in the model):

- **Emission is code** — deterministic state-diff → event. No judgment.
- **Notability is code.** A deterministic scorer decides *whether* an event is worth
  surfacing and at what priority — the anti-spam core. It carries forward the current
  engine's tuned logic (§17.2): per-type base scores, an evidence-accrual window and
  threshold, bypass tiers for big moments, same-tick **coalescing** (one post when
  several milestones land together), and **cohort** detection (3+ members hitting the
  same milestone in a day). This is *not* an LLM judgment call — a pure "is this
  notable?" prompt regresses to spam or silence.
- **Voice is a subagent** — one per stream, specialized to its domain (the clan
  subagent reasons about clan dynamics; the battle subagent about battle patterns; the
  player subagent about progression). It takes the *scored, deduped* candidate(s),
  decides framing and whether to speak, and emits a **communication intent**
  (presentation-free; a downstream surface owns copy and channel — §17.4).
  - *Deferred seam:* a subagent could also accumulate **insight** about its domain
    (patterns, trends) for Elixir to draw on later. That is a hook into the future
    memory/knowledge pass (§0), **not** a subsystem v5.1 designs — flagged here so it's
    a known gap, not an unanchored promise.

**Cross-stream arbitration — a mechanism, not a convention.** The single worst
recurring bug in the current engine is one real-world moment posting twice (champion
unlock; hot-streak *and* trophy-push). Single-ownership *by policy* ("a subagent never
recognizes a moment another stream owns") is not enough — Part I's whole thesis is that
convention drifts, and §11's arena-up deliberately correlates *across* streams, so the
streams are not hermetic. So recognition is made **structural**: every recognized
moment claims a deterministic key in one **shared recognition ledger**
(`arena_up:{tag}:{arena}`, `champion_unlock:{tag}:{card}`); first claim wins, whichever
stream surfaced it. Single-ownership becomes a uniqueness constraint, not a subagent
remembering its lane — the same idempotency discipline as the emitter's dedup keys
(§8), applied one level up at recognition. In plain terms: before posting, a subagent
**claims** the moment's key; the `UNIQUE` constraint means the first claim wins and the
second backs off — one real moment, one post. This ledger is **durable engine state**
(§14.2): if it resets, Elixir re-recognizes and double-posts everything after a restart,
so it is retained at least as long as a moment could plausibly be re-recognized.

## 11. Decisions

- **Three streams, three logs — decided.** Each persistent stream is its own event
  log / table, with its own grain and retention (see §7). Not one tagged log.
  Correlation between streams happens in recognition, not in storage. *(The bounded
  war stream also has its own log — `war_events`, added in the 2026-07-03 review;
  the principle is per-stream logs, and the count is four.)*
- **Arena Up — resolved: battle primary, profile authoritative.** The winning battle
  is the *moment* worth recognizing (exact timestamp, deck, opponent), so the
  **battle stream is the primary source**: a battle whose post-battle trophies cross
  an arena boundary is an arena-up candidate. The *fact* — which arena you're in —
  is authoritative on the player profile, so the **player-profile arena change
  confirms and backstops** it: if a poll gap means we never saw the deciding battle,
  the profile change still catches the arena-up (as an estimated-timing event). Arena
  Up is therefore the canonical **cross-stream correlation**: streams emit
  independently; recognition joins the battle event with the agreeing player-state
  change — one recognized moment, never two posts. Edges to respect: arena thresholds
  are game knowledge that shifts with balance updates, and Path-of-Legend / ranked is
  separate from trophy-road arenas.
- **Player-poll cadence & selection — designed in §15** (adaptive, activity-weighted
  polling with a fairness floor and a rate-limit ceiling).
- **Clan Management — designed in §13.** An independent, leadership-scoped function
  that reads the streams and recommends promotions / demotions / kicks.

## 12. Stream lifecycle — persistent and bounded streams

The three core streams are **persistent**: always on, each retaining events at its
own interval. But the stream / emitter / subagent shape generalizes to **bounded
streams** — instantiated for a specific, time-boxed event, run with a dedicated
subagent for its duration, then retired to a summary.

- **Tournaments.** When the clan runs or watches a tournament, that tournament is its
  own activity stream: its own grain (tournament battles, rounds, standings), its own
  log, and a subagent watching *this* tournament — producing live commentary and a
  final recap, then winding down. (Storage already exists — the tidy tournament
  tables Part I flagged as fine.)
- **Clan Wars.** This resolves the earlier deferred question. A war season / week is
  a **bounded stream** — not a fourth persistent stream, and not merely a frame. It
  spins up for the season, has its own grain (war battles, participation, standings
  by week), runs with a war subagent, and retires to a season summary. War battles
  are still battles (they also land in the battle stream); the war stream is the
  bounded, war-scoped *view* with its own subagent and lifecycle (detailed in §16).

A bounded stream's end may be **known in advance** (a tournament's scheduled finish)
or **discovered at runtime** (a war season ends only when Colosseum week is observed,
§16). Either way the lifecycle is the same — born, run, detect/reach the end, die.

This gives the model one clean extension point: any future "watch this for a while"
event (a special challenge, a global event) is a new bounded stream — same machinery,
lifecycle-managed. **Persistent streams never retire; bounded streams are born, run,
and die.**

---

## 13. Clan Management — the independent action function

Clan management is where Elixir has struggled most. It is **not** a recognition
stream; it is a leadership decision-support loop, and it is designed as a closed,
isolated system.

### 13.1 Why it has been hard

Elixir never had one authoritative view of member state. The metrics leadership
cares about were scattered across the three-generation sprawl (Part I), and the LLM
was asked to judge eligibility from clipped roster context. That produced the exact
failures we've seen — promoting a member for one strong donation week, missing quiet
inactives — because *judgment* was doing a job that belongs to *deterministic
bookkeeping*.

### 13.2 Principle: shared data substrate, isolated decision loop

- **Reads the three persistent streams plus the bounded war stream — it is a consumer,
  not a new poller.** Clan membership/roles/donations from the clan stream, activity
  from the battle/player streams, and participation from the **bounded war stream's
  participation projection** (§16.5). It must **not** re-ingest from the API; a parallel
  data flow here would rebuild Part I's mess inside the new engine.
- **The decision loop is closed and isolated.** Clan management never posts to public
  lanes and never entangles with the recognition subagents. Shared substrate,
  separate brain, leadership-scoped output only.

### 13.3 The deterministic core (code, not LLM)

- **Member-management projection.** One authoritative row per member with the metrics
  leadership actually uses — role, tenure, donations/week, war participation & points,
  and battle-based activity — built from the streams and refreshed weekly (donations
  and war reset weekly, so weekly is the natural grain).
- **Engagement is measured from battle logs, not presence.** Logging in to claim
  daily rewards is not contributing to the clan, so v5.1 deliberately **ignores
  `lastSeen` and does not count logins**. "Active" means *battling*; war battles are
  the strongest contribution signal. Idleness is the sustained *absence of battle
  contribution*, not the absence of a login.
- **Layer 1 — sustained-signal evaluators.** Small deterministic state machines per
  metric ("sustained donor", "war-reliable", "battle-active"), each requiring
  performance held *over time*, not a single good week.
- **Layer 2 — candidacy state machines.** Compose the Layer-1 signals into
  promote / demote / kick eligibility per policy, with **hysteresis**: eligibility is
  earned over N qualifying weeks and lost only on sustained slippage — no
  flip-flopping.
- **Policy in config.** Thresholds and windows stay in `CLAN.md` (configurable and
  portable). The state machines read them; the subagent never invents policy.

### 13.4 The clan-management subagent → Leader Actions

The subagent reads eligibility **state** (not raw metrics) and produces
**evidence-backed recommendations** that surface through Elixir's existing **Leader
Action** structure — the human-in-the-loop loop that already works well and we are
keeping.

- A recommendation is a leader action of type `promotion_recommendation`,
  `demotion_recommendation`, or `kick_recommendation` (these action types already
  exist), proposed to leadership with its evidence.
- **Leaders are the actor.** They **execute, edit, defer, or reject** each action,
  and that feedback is captured (including copy edits as diffs) — real signal for how
  Elixir should recommend next time.
- Recommendations **auto-withdraw when the underlying state changes** — a kick
  candidate who starts battling again drops out of candidacy and the action is pulled,
  so Elixir never nags on a stale case. The deterministic core makes this free.
- Output is **leadership-scoped only** — never public lanes, never the recognition
  subagents.

### 13.5 Advisory, not actuator — how the loop closes

The CR API is **read-only**: every endpoint is `GET`; the only write is a restricted
token-verification `POST`. Elixir **cannot** promote, demote, or kick. So clan
management is decision-support, and the loop closes through a human:

> **Elixir proposes a leader action → a leader executes / edits / rejects it in game
> → the clan stream observes the resulting role/roster change → the action resolves.**

The observed clan-stream event *is* the confirmation the loop closed. The existing
Leader Action outcome evaluation already works this way — snapshot a baseline on
propose, observe the delta later — so this is a re-host of a proven mechanism onto
the v5.1 streams, not a new invention.

### 13.6 What we measure (and deliberately don't)

- **Battle logs are the engagement signal.** Contribution is battling — war battles
  weighted highest — not presence.
- **`lastSeen` is ingested for roster-badge awareness, never as an engagement
  signal.** We record `lastSeen` (`player_current_state.last_seen_api`) so Elixir
  knows when a member is wearing the in-game "idle" roster badge — useful context
  for leadership optics — but it does **not** feed the kick clock. `lastSeen` moves
  when someone merely opens the game to claim rewards; that is not contribution.
  Engagement and idleness stay measured from battle logs, not logins.
- **Durable war contributors get a longer confirmation before a kick card.** A
  member whose 3-season war points or sustained attendance clears the bar earns
  extra idle days before the reactive path proposes a `kick_recommendation`
  (`management.py` `WAR_CONTRIB_*`) — they still surface as watch/at_risk, the card
  is only delayed. War contribution is meaningful; the ladder reflects it.
- **Donations reset weekly**; **war participation (decks used, points)** is the
  strongest sustained-contribution signal. The projection is built around what battle
  and war data actually tell us.

### 13.7 Open decisions

- **Review cadence.** ✅ **Decided — Q1** (`open-questions.md`): weekly batch review
  for promotion/demotion candidacies (Monday 7:00 AM America/Chicago,
  `runtime.md` §3); reactive surfacing for kick-risk transitions only; both through
  the existing leader-action post-policy gate.
- ~~Leadership response capture~~ — **resolved.** The existing Leader Action feedback
  loop (proposed → done / deferred / rejected, copy-edit diffs, outcome evaluation)
  already gives the loop memory. Keep it; re-host it onto the v5.1 streams.

---

## 14. Ingest, retention & preservation

### 14.1 One API client, one raw response log

Every call to the Clash Royale API routes through a single client, and that client is
the **only ingress**. Before anything is processed, it appends the raw response to one
append-only log — endpoint, subject tag, fetched-at, payload hash, body
(`raw_api_payloads` already exists for this). This is a **deliberate, valuable**
practice: a true record of what the API said, which the data analyst mines to detect
new signals and new game modes. We keep it — but in **one place**, not denormalized
into ~15 derived tables (Part I §2).

Raw capture and state admission are deliberately different guarantees. The client
records every decoded response under its true endpoint and subject, including a
payload the engine later rejects. `engine/observations.py` then validates the stable
endpoint shape and requested-entity identity before the response may reach ingest,
baseline diffing, or projections. Rejection preserves the prior known-good state and
last-success poll time; contract failures are counted and recorded in the incident
ledger. In particular, an empty battlelog is valid evidence of no recent battles,
while a missing battlelog is not evidence at all.

Crucially, the raw log is **not** the system of record. It is a **14-day rolling
buffer** (`RAW_PAYLOAD_RETENTION_DAYS = 14`, purged every maintenance cycle), and
historical API state cannot be re-fetched (the CR API only returns *current* state). So
the raw log is a **forward-only analysis buffer** — an earlier draft's claim that we
could "re-derive everything from raw" was exactly backwards.

### 14.2 The layered model

1. **Raw response log** — what the API said. Single, append-only, **14-day** rolling
   buffer. Forward-only analysis + short-window replay.
2. **Current-state baseline** — last known state per (entity, aspect), for diffing (§8).
3. **Event streams** — battle / player / clan. Fine-grained, each on its **own
   retention window** (§14.3).
4. **Rollup / aggregate layer** — daily metrics, battle rollups, recent form, awards,
   season summaries. **Durable — never purged.** This is where high-volume streams
   become long-term history.
5. **Identity, tenure & curated memory** — `players`/links (§7), `clan_memberships`,
   curated `clan_memories`. Durable.
6. **Projections / read models** — for querying (member management, query tools).

Plus **engine control state** that sits outside the derivation chain but must persist:
the **recognition ledger** (§10) — the "one moment → one post" claim table — is
**durable** (reset it and Elixir double-posts after every restart), alongside
follower/emitter cursors.

One ingress, one raw log, everything else derived. **Layers 1–3 are ephemeral by policy;
layers 4–5 and the control state are durable.**

### 14.3 Retention already exists — cite it, don't reinvent

§7's "each stream has its own retention" is **already implemented** in `_PURGE_TARGETS`
(`storage/metadata.py`). v5.1 should adopt and rationalize this policy, not write it as
new. **Superseded in part by Q8 / `schema.md` §1** — the table below is *today's*
policy; the v5.1 values differ where decided (battle events **180 d**, not 30;
war detail **365 d**, not 180; player events 180 d; clan events 365 d):

| Store | Retention |
|---|---|
| `raw_api_payloads`, card-collection snapshots | 14 days |
| player-profile snapshots | 21 days |
| state / battle / deck / usage snapshots, conversation `messages` | 30 days |
| signal outcomes, awareness ticks | 90 days |
| war tables | 180 days |
| tournaments (children cascade) | 365 days |
| **rollups, recent form, awards, tenure, identity, curated memories** | **∞ (durable)** |

Two cleanups fall out: `battle_telemetry` is in *no* purge target and grows unbounded
while its legacy twin `member_battle_facts` is capped at 30 days — whichever survives
needs a **deliberate** retention choice; and the durable artifact for the battle stream
is the **rollup**, not the fine-grained event stream.

### 14.4 Preservation — the cold-archive move

The clean break must not delete history. The durable layers (4–5) hold months that
cannot be re-fetched; the fine-grained layers are short windows anyway. So:

1. **Freeze the current DB as a read-only cold archive** (e.g.
   `elixir-v5-archive-2026H1.db`, never written again) before the cut. That preserves
   *everything* permanently for near-zero effort — the old truth is retired, not
   destroyed.
2. **Carry the durable layer forward** into the new schema: the rollup/aggregate layer,
   tenure (`clan_memberships`), identity, and curated `clan_memories`. This *is* the
   clan history — placements, season records, member arcs. If v5.1 changes a rollup's
   grain or shape this is a one-time **transform**, not a plain copy (and may be slightly
   lossy) — the cold archive is the backstop for anything the transform drops.
3. **Start the fine-grained streams fresh** at the cut. Today's battles/snapshots are
   14–30-day windows by policy (v5.1 widens battles to 180 d — Q8), so there is no
   long-term loss; the archive holds recent detail if ever needed.

This is what makes the clean break safe: precious history is **defined explicitly,
carried forward deliberately, and backed by an immutable archive**.

### 14.5 The read/query layer (in scope)

Layer 6 (projections / read models) is where Elixir *answers questions* — the ~30
member/leader query tools (`resolve_member`, `get_member`, `get_river_race`,
`get_clan_health`, `get_awards`, …). Today these are bound to the tables v5.1 replaces:
a scan found ~190 queries across 16 tables, ~60–70% of them broken by the core-table
swap (§17.1). So porting them is **part of v5.1**, not deferred — they are the read side
of the engine.

Design implications for the read models the tools port onto:

- A **live current-state baseline** per member (today's `member_current_state`) so
  "what are their trophies *now*" is an O(1) read, not a stream fold.
- **Pre-materialized rollups** for anything currently recomputed per call — form,
  win-rates, mode activity, war participation — so reads never scan the event stream.
- War events carry **`race_id` / `section_index`** so war-deck reconstruction and
  attendance are joins, not inference.
- **Role changes and trophy deltas** are first-class events (today diffed from snapshot
  pairs), so history / at-risk / trophy-drop reads come off events, not snapshot
  archaeology.

Deferred is only the conversational *experience* over these tools (the `#clan-chat` /
`#ask-elixir` UX and its memory), not the tools' data port.

## 15. Adaptive player polling

Profile and battle-log calls are **per-player** and are the expensive part of the
engine, so how we spend that budget matters. Today Elixir round-robins the roster —
~4–5 players per tick, every player within ~4 hours. It works, but it's flat: an
actively-battling member is polled no more often than someone who hasn't played in
days.

v5.1 makes polling **activity-weighted**:

- **Temperature per player.** A small per-player state machine — cold → warm → hot —
  driven by observed **battle activity** (new battles since last poll). Active players
  heat up; temperature decays without activity.
- **A cheap heartbeat for the whole roster.** The clan endpoint is a *single* call
  that returns every member's trophies/donations. Poll it frequently to cheaply see
  who's moving and feed that into temperature — without spending per-player calls.
- **A budget-aware priority scheduler.** Each tick spends a bounded per-tick API
  budget (rate-limit ceiling) on the **hottest players first**, with a **fairness
  floor** so cold and brand-new members are still caught within a bounded window.
- **Right cadence per endpoint.** Battle logs are time-sensitive (recognition wants to
  celebrate an arena-up or big win promptly) → poll hot players' battle logs
  aggressively. Deep profiles change slowly → lower base cadence, bumped when a player
  is hot, notably to *confirm* an arena-up (§11's cross-stream rule).

Net: active players get near-real-time coverage while quiet players cost almost
nothing — better than a flat 4-hour sweep, within the same rate limits.

---

## 16. Clan Wars — the bounded war stream

A Clan Wars season is a **bounded stream** (§12), and it is the sharpest example of the
pattern because its length is *discovered*, not declared.

### 16.1 Season lifecycle

- **Born** when a new `seasonId` is observed.
- **Runs** week by week (sections); each week is training days, then war (battle) days.
- **Ends at Colosseum week — detected, not known in advance.** The API never tells us
  up front whether a season is 4 or 5 weeks. The one deterministic rule: **every season
  ends with a Colosseum week.** So if the current week is *not* Colosseum, there is
  always at least one more week; if it *is* Colosseum, it is the finale. This is
  observable in code (`periodType == "colosseum"` on battle days; a `colosseum_week`
  flag on the live war state) and it resolves the 4-vs-5-week ambiguity that has
  tripped Elixir up. Until the flag is true, never forecast the season's end.
- **Dies** after the Colosseum war days complete → retires to a **season recap** (final
  race placements + the War Champ).

Seasons are **contiguous, never dormant.** River Race is always active: a season dies
to its recap and the next season's bounded stream is born immediately — the birth of the
new instance *is* the same event as the prior season's death. Each season is still its
own bounded instance (born on a new `seasonId`, dies at Colosseum); the instances just
run back-to-back.

### 16.2 The war clock (deterministic time core)

The bounded stream has an authoritative, code-computed **clock**: phase, day number,
battle/practice days remaining, hours left in the period, `is_colosseum_week`,
`season_id`, `week`, and `pace_status`. The subagent **reads** this; it never infers
"what moment is it in the war." (This formalizes the existing `time` / "current moment"
block as the stream's clock.) The clock also drives **phase-appropriate behavior** —
e.g. no boat-defense talk during Colosseum, and a shift from win-urgency to recognition
once the weekly race is already won.

### 16.3 The War Champ race (deterministic, season-bounded)

The clan's signature tradition: the season's **top war contributor** is the **War
Champ** and earns a **free Pass Royale** (~$15). Members care intensely and constantly
ask Elixir who's winning — which is a **math** question, so it lives in **code**, never
the LLM:

- A season-bounded standings table: per-member cumulative **points** across all weeks,
  plus decks used / attendance (including a perfect-attendance flag).
- ~~**Criteria are policy and vary by season**~~ — **superseded by Q2**
  (`open-questions.md`): War Champ is **always top points** (a member's war
  contribution is points; fame is a clan-only concept), no variant mechanism;
  the Free Pass **rotates** (never the same player in sequential seasons — falls
  to rank 2, champ keeps the honor).
- Elixir answers "who's winning the Free Pass Royale / who's War Champ so far?" by
  **reading** the standing — it never sums points itself.
- At season close, code determines the War Champ → a season-close event → public
  recognition (a clear POAP KINGS honor) and a durable record.

### 16.4 The competitive frame (the 5-clan race)

A River Race is our clan against four others. The war subagent tracks our **race
position** and produces time-sensitive momentum to `#river-race` using the clock,
`pace_status`, and completion state. Normal weeks have a 10,000-fame finish line.
**Colosseum has no finish line**: it is a four-day period-point contest, and every
battle continues to count toward both clan and member standings. When a normal
weekly race is already won, Elixir drops urgency and shifts to closure and
recognition (never guilt-driven).

### 16.5 War feeds clan management (bounded → persistent)

War participation — decks used, points — is the **strongest contribution signal** for
clan management (§13). So the bounded war stream **feeds the persistent
member-management projection**: strong war performance is recognized publicly, and
chronic war no-shows become a private management signal (the "war-reliable" evaluator).
~~Attendance is dual-purpose — a public nudge to use your decks, and a private
sustained-contribution input.~~ **Superseded by Q3:** no attendance nudge anywhere;
attendance is a private management input only.

### 16.6 Season history

Each retired season leaves a durable recap — our placements, the War Champ, the notable
arcs — which accumulates into a **season history** members value (bragging rights, and
POAPs issued per season). Bounded streams die to summaries; those summaries are the
clan's long memory of its competitive life.

### 16.7 Data sources

Live `currentriverrace` (in-progress; period/section; sometimes omits `seasonId`, which
we infer) plus `riverracelog` (finalized weeks). Fame is cumulative within a season
across weeks; the bounded stream stitches live + finalized.

*Sediment to fix at the cut:* `cr_api.py` calls the correct `/riverracelog` endpoint but
stores its raw payloads under the legacy label `clan_war_log`. The new single raw log
(§14.1) should record it as `riverracelog` so it isn't born with a misleading alias.

### 16.8 Open decisions — ✅ all decided (see `open-questions.md`)

- **War Champ criteria model.** → **Q2:** always top fame, no variant mechanism;
  Free Pass rotation rule (never sequential seasons; falls to rank 2).
- **Attendance nudge placement.** → **Q3:** no nudge, anywhere; participation is a
  private management input only.
- **POAP tie-in.** → **Q4:** none — the POAP platform is paused; design nothing.

---

## 17. Gaps, omissions & migration risks

The doc above is a clean model; Elixir is a large live system. This section is the
**delta** — what the model doesn't yet address and what will bite during a build —
grounded in a full codebase scan (2026-07-02). Ordered by build risk.

### 17.1 The load-bearing tension: the interactive query layer can't be "deferred" while its schema is replaced

§0 defers conversational memory and the interactive lanes to a later pass. But the
**query-tool layer that powers every member/leader answer is coupled to the exact
tables v5.1 replaces.** The scan found ~30 tools / ~190 SQL queries across 16 tables;
replacing the five core tables (`member_current_state`, `member_battle_facts`,
`war_participation`, `war_races`, `member_state_snapshots`) breaks an estimated
**60–70% of Q&A queries**. Slash commands and the content jobs read the same tables.

So "defer interactive" is only true for the *conversation design*. The **read-side
port** — repointing the tools at the new baseline + rollups + streams — is **not
deferrable**; it happens at the cut or Q&A/leadership answers go dark. Concretely, the
new model must expose: a live **current-state baseline** (today's `member_current_state`
role), pre-materialized **form/rollup** reads (today's `member_recent_form`,
recomputed per call), war **participation rollups** with `race_id`/`section_index` on
war battle events (today's war-deck reconstruction infers these), and **role-change /
trophy-delta** as first-class events (today diffed from snapshot pairs).

> **Resolved:** the query-layer port is now *in* v5.1 scope (§0, §14.5); only the
> conversational experience and curated-memory redesign are deferred.

### 17.2 Recognition needs deterministic scoring, not just "a subagent decides"

§10 says a subagent "recognizes what is notable." The current `CommunicationPolicy` is
far more than that, and it's tuned against real spam incidents: an 80-point highlight
**threshold**, a **14-day evidence-accrual** window, per-type **base scores**, six
**bypass** types, **dynamic** scoring (champion unlock 90 vs. legendary 65; trophy-push
scaled by delta), **same-tick coalescing** with a priority sort, **cohort-wave** (3+
members hitting the same milestone in a day), and explicit **double-post guards**. A
pure-LLM "notable?" judgment regresses to spam or silence.

> **Resolved:** §10 now splits recognition into emission (code) → notability scoring
> and coalescing (code) → voice (subagent), carrying this tuning forward as
> deterministic code. The shared recognition ledger prevents duplicates; the notability
> scorer prevents spam — they are different mechanisms.

### 17.3 The doc omits the runtime engine and delivery guarantees

The model names components (streams, emitters, subagents, ledger) but not the
**driver**. Today one tick is: ingest → advance emitters/detectors from tracked
positions → project → recognize → raise intents → deliver. Delivery is **at-least-once**
(compose → send → mark fulfilled *only on confirmed send*; failed intents retry next
tick; intents older than 6h are dropped). v5.1 needs an equivalent orchestrator and
must keep these delivery semantics — otherwise posts silently drop or double-fire.

### 17.4 Channel routing and composition context are under-specified

"A downstream surface owns copy and channel" hides tuned machinery: an
intent-prefix → lane map (celebrate → `#player-highlights`, clan → `#clan-events`,
war → `#river-race`, leadership → `#leader-actions`), and **composition enrichment**
(subject's recent-detection history, recent-win telemetry, naming guards, deterministic
fallback when the LLM returns garbage). v5.1 should map streams → lanes explicitly and
preserve the enrichment that makes posts read well.

### 17.5 Leader-action re-host carries specific coupled behavior

Re-hosting leader actions (§13) is not a lift-and-shift. `MemberLeftDetector` reads
`leader_action_recommendations` for **kick-suppression** (don't post "X left" if they
were kicked within 14 days) — a real cross-coupling between recognition and management.
Plus the 24h outcome-delay and the decision-case lifecycle. These behaviors must be
carried forward deliberately, and they currently read Gen A/B tables slated for
deletion.

### 17.6 Genuine feature gaps — ✅ all resolved (see `open-questions.md`)

- **Award engine.** ~~It's deterministic — fits the "math in code" pattern — but it's
  undesigned.~~ → **Q5:** designed — awards consume stream events; standings
  projections compute, the awards ledger records; grants fire on the war stream's
  season-death event.
- **Manual / non-API evidence.** ~~Either a manual-evidence ingest path or a "bounded
  manual stream" is needed.~~ → **Q6:** Clan Voyages is dead — dropped from the
  system (C6); the arena-relay screenshot readout is retained as an interactive
  channel-router behavior, not an engine stream. No manual-evidence stream is
  designed.
- **Onboarding / verification & system signals.** → **Q7:** port-and-repoint, no
  redesign; identity FKs move to `player_tag` and nothing else changes.

### 17.7 Migration cost the "clean break" understates

- **Tests.** ~2,100 lines of `event_core`-specific tests plus integration across **49
  test files**; most are invalidated by the cut and need rewriting.
- **Baseline seeding.** To avoid a first-sight emission flood (§8), the streams must
  seed their baselines from carried-forward state at the cut, not start empty.
- **Battle-completeness ceiling.** The battlelog endpoint returns only a shallow window
  of most-recent battles per player, so a very active player between polls **loses
  battles** — bounding arena-up detection, war-deck reconstruction, and form. Adaptive
  polling (§15) mitigates but cannot eliminate this; the "mirror every battle" premise
  (§9.1) has a real ceiling. *(Verify exact battlelog depth.)*

### 17.8 Doc drift to fix (not v5.1 scope, but note it)

`AGENTS.md`/`README.md` still describe live website publishing to poapkings.com; the
scan indicates that exited Elixir (~2026-06-21) and is now an external script. Correct
the reference docs so the next reader isn't misled.

---

## Next steps

1. Lock §7–§16 and act on §17 before building.
2. Read layer (§14.5) and notability core (§10) are now **in scope** — build them
   *with* the streams, not after.
3. Resolve open items: clan-management review cadence (§13.7), Clan Wars decisions
   (§16.8), award-engine and manual-evidence gaps (§17.6).
4. **Cold-archive** the current DB (§14.4), then sequence Part I's teardown into this
   shape — durable layer carried forward, fine-grained streams seeded then fresh.
5. Scope the deferred **memory/knowledge pass** (§0): curated `clan_memories`,
   conversational memory design, interactive-conversation UX, and the "insight" seam.
6. Correct doc drift (§17.8) and rewrite the invalidated tests (§17.7).
