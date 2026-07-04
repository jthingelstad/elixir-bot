# Elixir v5.1 — The Editor (internal output evaluation)

> **Status:** 🟡 Spec'd 2026-07-04, build authorized same day (Jamie: "you
> should build it because this code has never gone through a season end —
> it reduces risk"). Same conventions as the v5.1 set.
> **Owner:** Jamie · **Last worked:** 2026-07-04
>
> **The problem (Jamie):** "We drift far too often here. I don't think a
> static eval is the right shape." Elixir needs an *internal* system to
> evaluate its own output. Proven live within the hour of the design
> conversation: the EddiePlayz welcome invented "a deep card collection" —
> not in the facts — despite the compose ask explicitly forbidding invented
> details. **Prompt-text rules decay on contact; only a checking gate holds.**

## 1. Shape: an organ, not an eval

v5.1's architecture is *observe → judge → act* pointed at the clan. The
Editor is the same machinery pointed at Elixir's own speech. Three parts:

## 2. The inline gate (acts, not measures)

Sits in `engine/delivery.py` where the meta-marker guard already lives (the
Editor generalizes it). After compose, before send:

- **Grounding** — every specific claim in the copy (numbers, possessions,
  events) must trace to the intent's facts JSON. The founding failure class.
- **Substance** — fails template-grade copy (the sikander test): would a
  human who looked at the subject write this?
- **Freshness** — no stock phrases recycled from Elixir's recent posts (the
  critic sees the last ~15 fulfilled intents' copy; "momentum is real" ×3).
- **Lane fit** — tone/length appropriate to the destination lane.

**Verdict flow:** `pass` → send · `revise` → ONE recompose with the critique
appended to the compose context → re-judge → pass/fallback · `fallback` →
the deterministic `render_intent` copy (rehearsed, grounded by construction).

**Fail-open, always:** any Editor error (API failure, timeout, malformed
verdict) → the original copy sends. The Editor can only ever *improve or
defer to the deterministic fallback*; it can never block delivery or dead-end
an intent. Critic model: the observation family (haiku-class); ~10–20
posts/day ⇒ negligible cost.

**Trace:** every verdict persists to `editor_verdicts` (below) and is
visible per-intent in the Observatory (`/recognition/{key}` detail).

## 3. The living rubric (the anti-drift core)

The rubric is **data, not prompt text**: memories tagged `editorial` +
(`exemplar` | `anti-pattern`), each with the post text, the reason, and
provenance. The critic's prompt is assembled fresh per verdict via ranked
retrieval (the v5.1 memory system natively serves this) — top exemplars +
anti-patterns for the event type/lane.

**Auto-feeders (human actions become the rubric):**

| Signal | Feed |
|---|---|
| `prompt_feedback` 👎 (and strong 👍) | daily sweep → anti-pattern / exemplar candidate with the post text |
| An admin deletes an Elixir post (`on_message_delete` in Elixir's lanes) | anti-pattern with the deleted copy + intent facts |
| Leader copy-edits on action cards (`copy_edit_diff_json` — already captured) | paired before/after exemplar |
| Jamie's explicit flags (chat/#ask-elixir) | leader-note editorial memory via the existing tools |

**Founding rubric entries (seeded at build):** the sikander flat welcome
(anti-pattern: template-grade), "momentum is real" ×3 (anti-pattern: stock
phrase), the "64 members" invite (anti-pattern: unverified count), the
EddiePlayz welcome as a *pair* (exemplar structure/warmth + anti-pattern
invented specific), and the pre-cut Andy welcome (exemplar: grounded
concrete first impression).

## 4. The weekly self-review (trend + self-correction)

Scheduled activity `editorial-review` (Sundays 20:07 CT, after the week's
war cycle): samples the week's fulfilled intents, scores each against the
rubric dimensions, then:
- writes a self-assessment memory (kind `synthesis`, tag `editorial`);
- posts ONE report to `#elixir-log`: counts, gate revision rate, a **drift
  line** (distance of the week's output from exemplar class, in plain
  words), and proposed rubric additions;
- proposed additions auto-add at `confidence 0.6` (they influence ranking
  weakly) and are listed in the report for Jamie's veto — silence is
  consent, a 👎 on the report or an explicit note retires them.

## 5. Schema (one small table; no changes to existing tables)

```sql
CREATE TABLE editor_verdicts (
    verdict_id INTEGER PRIMARY KEY,
    intent_id INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','revise','fallback','error')),
    dimensions_json TEXT,            -- per-dimension pass/fail + notes
    original_copy TEXT, final_copy TEXT,
    at TEXT NOT NULL
);
```

## 6. Why building it *before* season close reduces risk (Jamie's call)

The close ceremony is the least-tested LLM compose surface in the system.
With the gate: hallucinated ceremony copy (wrong champ name, invented
margins) gets caught by grounding and falls back to the rehearsed
deterministic lines — which the dry run already validated read well. The
fail-open design means the worst the Editor can do on Sunday is nothing.

## 7. Build plan

1. `engine/editor.py`: `judge(copy, intent_facts, recent_copies, rubric) ->
   Verdict` (one LLM call, strict JSON verdict, fail-open wrapper);
   `build_rubric_context(conn, event_type, lane)` via memory ranking;
   `record_verdict(conn, ...)`.
2. `engine/delivery.py`: wrap the compose step — compose → judge →
   (revise-once | fallback) → send; trace every verdict.
3. Rubric feeders: daily sweep job folded into `engine-health`'s slot
   (separate function, same schedule family); `on_message_delete` listener
   (runtime/app.py) filtered to Elixir's own messages in its lanes;
   copy-edit pairing in the action-card edit path; seed script for the
   founding entries.
4. `editorial-review` activity (Sun 20:07 CT) + report composer.
5. Observatory: verdicts on the intent detail page + an `/editorial` page
   (rubric browser + weekly reports).
6. Tests: verdict-flow goldens (pass/revise/fallback/error paths with a
   stubbed critic), grounding-check candidates (the EddiePlayz case as the
   regression), rubric retrieval, fail-open (critic raises → copy sends),
   feeder unit tests. The critic itself is an LLM call — stub in tests,
   validate the verdict JSON contract.
7. Deploy before season close; the gate's first live week IS the review's
   first report.
