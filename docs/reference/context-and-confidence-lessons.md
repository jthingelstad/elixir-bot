# Context, testing, and confidence lessons

Long-form lessons pulled out of `AGENTS.md` on 2026-08-05. They are real and
still apply; they live here because `AGENTS.md` is loaded into every agent
session and these are reference reading, not rules you need in front of you
while working.

## Reality-based testing (the three levers beyond the suite)

Unit tests target one delta with minimal dicts; these three run the engine against reality and catch what hand-built fixtures can't. Run the first two before deploying engine changes:

1. **Replay gate** — `uv run --locked python scripts/replay_gate.py`. Snapshots the live DB, clears baselines, and replays the real raw-payload window twice through the awareness-only offline engine. Pass 1 inventories historical drift (current code may derive events an older deployment missed); pass 2 is the hard gate and must add exactly zero events, battles, or legacy claims under the same code. Ends with the current-data-relative season-close rehearsal + global invariants. All gates must PASS.
2. **Time-travel simulator** — `uv run --locked python scripts/simulate.py`. A deterministic synthetic war week (skewed 09:37Z reset, a join, a leave, a level-up, war battles, section rollover) through the production `run_tick` path at ~2 s/simulated-week. It proves event correctness, drift anchoring, poll fairness, zero legacy claims, absence of the retired delivery queue, and that the awareness read sees hard-post stream events.
3. **Real-payload fixtures** — `tests/fixtures/cr/*.json`, loaded via `load_cr_fixture` (tests/conftest.py) and asserted by `tests/test_cr_fixture_shapes.py`. When Supercell drifts a payload shape, these fail with a clear diff. Refresh stale fixtures by re-exporting from `raw_api_payloads` — never hand-edit them.

`assert_db_invariants` (tests/conftest.py) is the shared floor under all of it — an autouse sweep after every test, plus a gate inside both scripts: unique open memberships, one ledger claim per key, FTS mirror in sync, canonical timestamps, and projection consistency.


## Confidence layer (where failures go; how to know Elixir is healthy)

The bugs that keep biting are seam/first-use failures that fail *silently*. Three
tools make them visible:

1. **The error log** — `logs/elixir-error.log` (ERROR+ with tracebacks, written
   by `runtime/logging_setup.py`, rotated at 2 MB × 5). Abandoned runtime work
   and cross-table consistency failures log there on their module's own logger
   with a stable `<component> failed: k=v` prefix. Expected parsing, user/tool
   errors, and optional enrichment use bounded fallbacks or lower levels instead
   of flooding it; see `docs/reference/error-handling.md`. It is small enough
   (~6 lines/day) to read whole, which is the point.
   **Elixir does not monitor itself.** A `runtime_incidents` ledger and a daily
   `engine-health` job tried, and the ledger recorded 0 rows in 25 days while
   the log held 159 real errors — so the check reported "all clear" through
   every failure. Both were retired 2026-07-28 (schema v20). Detection is an
   operator job: **AGENT-TEAM/error-watch.md**, owned by the Operations Manager.
2. **Entrypoint smoke** (`tests/test_entrypoints_smoke.py`) — static + dynamic
   check that every function's names resolve and every compose/card/tool
   entrypoint is invocable. Catches the NameError/lazy-import class at test time.
3. **`scripts/confidence_report.py`** — one command (`--json`, non-zero exit on
   findings) that unifies grouped errors from the error log + smoke/integration
   test status + the latest post-quality scorecard, plus the `liveness` silence
   alarm (an error log cannot report a failure that produced no error, and the
   worst outages were quiet). "Is Elixir healthy?" in one answer. Run it
   before/after any change; the external Operations and Quality Manager routines
   execute it. The scorecard samples the active awareness and assistant-message
   paths read-only. Agents turn confirmed findings into GitHub issues; the report
   never creates a second work queue or silently changes production memory.

### Review discipline

A green suite is necessary, not sufficient. Before deploying a substantive change, do a **cold adversarial review** of the diff — read it as a skeptic hunting for what breaks, not as the author confirming what works. After deploying, do a **live behavioral audit**: watch what the running system actually does (tick counters, the error log, posted messages) rather than what the code says it should do. The 2026-07-04 end-to-end review is the reference case: the suite was green, yet the live audit found a season-breaking gap (the awards consumer was never built — two work streams each assumed the other owned it) and the cold review found ten more real defects (delivery commit ordering, per-lane fail-stop, timestamp-format mismatches, CSRF host matching). An `engine-health` daily activity once tried to institutionalize the live audit's checks in-product; it was retired 2026-07-28 because a check that only covers known failure classes, run by the system it is checking, manufactures false calm (it read a ledger that never recorded a row). The watching lives outside the runtime now — `AGENT-TEAM/error-watch.md` — and new changes still need fresh adversarial eyes. Never mark a cross-stream feature done without verifying the consumer end-to-end.


## Auditing a read block: ask what it could ever CHANGE

Volatility is the wrong test. `management.materialization.source_freshness` changed
every tick — per-member battlelog and profile ages — and looked alive. Across 20
sampled ticks, **907 of 907 member rows said `status: ready` with no reasons**: it
churned constantly and could never change a decision. ~3,700 tokens a tick, 30% of
the whole read, telling the brain that nothing was wrong once per member. It now
sends a count plus the exceptions in full (98% smaller).

The check that finds this: pull real payloads out of `llm_calls.prompt_json`, size
every field, and for the big ones ask what value would make the brain act
differently. If the answer is "nothing it has ever contained," summarise it and keep
the exceptions.

**Two ways this audit goes wrong, both hit in practice:**

- **Regex "unused" is a false negative.** `game_context` matched none of 111 posts
  and looked identical to the waste above. Checking its literal values showed
  "Merge Tactics", "Ronin" and "legendary" all reaching output. Match values, not
  themes, before calling a block dead.
- **Some blocks work by preventing output.** `channel_memory`,
  `recent_agent_writes`, `recent_member_spotlights` and `posting_pulse` exist to
  stop repetition — absence from posts is them succeeding. Output-matching cannot
  measure them at all.

**A rarely-used block is not automatically a tool.** Moving it means the brain must
know to ask, and models under-fetch: on replayed rounds Haiku ended the tool loop
early in 3 of 5, and `game_context` is what tells the brain a new card exists at all —
without it `lookup_cards` is never called. Price it before moving: `award_races` is
1,192 tokens at 23% usage, and a triggered tool round re-reads the whole ~57K
context, so the tool version saves nothing and adds a way to fail.


