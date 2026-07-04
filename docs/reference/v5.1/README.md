# Elixir v5.1 — Design & Build Spec

This directory is the working set for the **v5.1 re-architecture** of Elixir's engine.
`architecture.md` holds the agreed *design* (the why and the shape). The remaining
documents turn that design into a **build-ready spec** — the concrete detail Claude
Code (or any builder) needs so it *implements* the design instead of *inventing* it.

> **Status:** ✅ **Build-ready — all gates closed (2026-07-03).** Design locked;
> build spec fully drafted 2026-07-02; decisions Q1–Q8 recorded; all
> ratifications settled 2026-07-03 (arena_up **85/bypass**; runtime §8 defaults
> **as drafted**; weekly review **Monday 7:00 AM America/Chicago**;
> `management.md` §5 defaults **as drafted**); two review passes applied
> (`feedback.md` rev 4 + rev 5 — the rev-5 cold read added `war_events`,
> `management.md`, and 17 clarity fixes). Builder entry point:
> `migration.md` Phase 0.
> **Owner:** Jamie · **Last worked:** 2026-07-03

## Reading order

| # | Document | What it is | Status |
|---|---|---|---|
| 1 | [`architecture.md`](architecture.md) | The design: the three-generation problem, the clean break, streams + emitter, recognition, clan management, ingest/retention, adaptive polling, Clan Wars, and the gaps register (§17). The *why* and the *shape*. | ✅ Locked |
| 2 | [`open-questions.md`](open-questions.md) | The decision record: Q1–Q8 (from `architecture.md` §13.7/§16.8/§17.6 plus implicit ones), all **decided 2026-07-02**, plus tracked constraints C1–C6. Downstream docs cite entries by number. | ✅ Decided |
| 3 | [`schema.md`](schema.md) | The concrete data model — `CREATE TABLE` for every **new or changed** table by layer (carried-as-is tables ship via the archive's live DDL, exported at Phase 2), the §8 gone-at-the-cut list (26 dropped + 7 transformed), and the full tool→read-model coverage matrix (schema.md §9, satisfying architecture §14.5). Tag-keyed per §7; honors Q1–Q8/C1–C6. | ✅ Ready |
| 4 | [`events.md`](events.md) | The event catalog — every `event_type` per stream, payload, dedup key, owner; derived battle moments; the complete C2 mapping of all 25 Gen C detection types. | ✅ Ready |
| 5 | [`recognition.md`](recognition.md) | The notability spec — scorer constants ported verbatim (threshold 80, 14-day accrual, bypass/coalescing/cohort), the ledger keys, fail-closed routing, composition guards. `arena_up` 85/bypass ratified (§9). | ✅ Ready |
| 6 | [`runtime.md`](runtime.md) | The engine loop — 7-step tick, cursor + poison-event discipline, at-least-once delivery (6 h expiry carried), the §15 adaptive poll scheduler (`poll_state`), activity registry changes. Defaults ratified (§8). | ✅ Ready |
| 7 | [`management.md`](management.md) | The clan-management rules — Layer-1 evaluator qualifying rules, Layer-2 candidacy transitions (promote/demote/kick) with hysteresis, guards, and the ratified defaults. Added 2026-07-03 (the review found §13.3 had state names but no transition logic). | ✅ Ready |
| 8 | [`migration.md`](migration.md) | Phases 0–9: archive freeze, T1–T14 carry-forward transforms (T14 = calendar ledger seed), teardown, read-layer port, parity checks, go-live bake, test rewrite, acceptance criteria. | ✅ Ready |
| — | [`feedback.md`](feedback.md) | External review log (the parallel Claude Code review that hardened the design). Living. | ✅ Living |

## Ratifications — ✅ all settled (Jamie, 2026-07-03)

1. `recognition.md` §9 — `arena_up` base score **85, bypass**. ✅ Ratified.
2. `runtime.md` §8 — tick interval **10 min**, `POLL_BUDGET_PER_TICK = 40`,
   poison-skip after **3**, weekly review **Monday 7:00 AM America/Chicago**.
   ✅ Ratified.
3. `feedback.md` rev 3 — drift found during the build-spec pass. ✅ Reviewed;
   rev 4 and rev 5 log the two completeness passes and their fixes.
4. `management.md` §5 — the clan-management defaults table (donor threshold,
   qualifying windows, kick confirm days). ✅ Ratified as drafted; constants
   land in `CLAN.md` at the cut (migration Phase 5).

## Readiness

`architecture.md` is design-complete but **not build-ready on its own** — §17 and its
own "Next steps" say so. This doc set closes the gap. Each build-spec document maps to
a specific readiness gap identified in `architecture.md`:

- No concrete schema → **`schema.md`**
- No enumerated event types/payloads → **`events.md`**
- Notability "a subagent decides" instead of the ported scorer (§17.2) → **`recognition.md`**
- Runtime loop omitted (§17.3) → **`runtime.md`**
- Channel routing under-specified (§17.4) → **`recognition.md`**
- No phased plan / acceptance criteria → **`migration.md`**
- Open decisions (§13.7, §16.8, §17.6) → **`open-questions.md`**

Items 2–7 are written, the open questions are resolved, and the ratifications are
settled — **the set is ready to hand to Claude Code as a build spec.** Entry point
for the builder: `migration.md` Phase 0.

## Conventions

- **Source of truth:** `architecture.md` owns the *rationale*; the build-spec docs own
  the *detail*. If they ever disagree, the detail docs win for implementation, and the
  discrepancy is a bug to reconcile.
- **Tag-keyed identity** (`architecture.md` §7) applies throughout: CR tags are the
  natural key; internal-only entities keep synthetic ids.
- **Ground everything** against the live DB and codebase before asserting it, the same
  way `architecture.md` and `feedback.md` were built.
