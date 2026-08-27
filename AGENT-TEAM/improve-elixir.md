# Improve Elixir — Close the Clan Learning Loop

Your objective is: **over the three full Clash Royale seasons beginning with Season
136, Elixir turns each week's play into trusted human-approved stewardship, one
durable shared story, and one evidence-backed next step for every opted-in member;
then it uses leader feedback and subsequent play to improve the next cycle without
taking authority from people.**

Elixir is POAP KINGS' steward and institutional memory, not primarily its chatbot.
Its flagship product surfaces are the `#actions` leadership queue, the weekly clan
story, and weekly individual performance reports. Ask Elixir and proactive posts are
supporting interfaces. Judge them by usefulness and trust, not by engagement volume.

Accurate, relevant, timely, appropriately quiet, and worthy of trust remain hard
quality guardrails. Measurement and improvement belong together: inspect exact
behavior, define the bar, build the regression, and correct the source when
authorized. Do not grade Elixir from summaries or create a ticket merely to transfer
the finding to a builder.

Adopting this objective does not grant blanket authority for member-visible changes.
Jamie still approves changes to behavior, cadence, persona, outreach, and leadership
policy. Elixir recommends and remembers; people decide.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and this file.
Use the project `awareness-report` skill and the repository eval/feedback tools.

Cadence: daily recent-behavior review. Friday's run is the deeper baseline and
coverage-improvement pass.

## Outcome loops

1. **Stewardship:** clan signals become well-framed `#actions`; leaders decide; the
   decision and outcome become evidence for later comparable judgment.
2. **Clan memory:** weekly activity becomes a grounded shared story, and completed
   weeks become an honest season arc rather than disconnected recaps.
3. **Member improvement:** each opted-in member gets one specific, evidence-backed
   next step; the next comparable report says what changed instead of starting over.
4. **Agent learning:** high-signal feedback becomes an evidence-linked lesson only
   when warranted, changes later comparable behavior, and is checked against natural
   outcomes. The existence of memory machinery is not proof that learning occurred.

## Three-season scorecard

- **Stewardship:** 100% of promotions, demotions, kicks, and consequential outreach
  remain human-approved; zero unauthorized actions occur; stale decisions are
  surfaced rather than bypassed. A well-framed decline is a successful HITL outcome,
  so approval rate is not a target.
- **Clan memory:** every scheduled weekly story and season synthesis is delivered or
  explicitly reconciled, remains grounded in canonical evidence, and needs no factual
  correction. Carry a prior thread forward only when evidence supports continuity.
- **Member improvement:** every verified, opted-in member receives the scheduled
  report. Every report with comparable prior evidence revisits the previous next step
  and explains the observed movement or lack of evidence. Expanding reach means one
  Jamie-approved opt-in opportunity, never repeated or manufactured outreach.
- **Agent learning:** by the end of Season 138, demonstrate at least three complete
  `feedback -> evidence-linked lesson -> changed comparable behavior -> later natural
  evidence` loops. Once a correction is confirmed and resolved, the same adjudicated
  error pattern should not recur within 30 days. Record `insufficient_sample` instead
  of inferring success.
- **Trust:** factual honesty, tool boundaries, deterministic leadership authority,
  privacy, and existing strict eval bars do not regress. Cost is visible per useful
  outcome but is never optimized at the expense of these bars.

Do not use chat activity, post count, action approval rate, feature count, or clan-war
placement alone as the north star. Do not add an autonomous agent team, autonomous
self-modification, a new posting path, or synthetic member activity to satisfy this
objective. Clan results remain important context, but Elixir must not claim it caused
them without evidence.

## Every run

1. Run the shared preflight.
2. Start with exact artifacts:
   - `awareness_posts` joined to `awareness_thoughts` for proactive decisions;
   - `messages` and traces for Ask Elixir and other interactive replies;
   - `leader_action_recommendations` for judgment, copy edits, and outcomes;
   - scheduled-job evidence and exact rendered artifacts for the weekly clan story
     and individual performance reports;
   - `prompt_failures`, thumbs-down feedback, validation failures, and exact Discord
     message IDs when available.
3. Run `scripts/review_agent_feedback.py`, the leader feedback report, the confidence
   report, and the relevant eval harnesses. Always include Ask Elixir alignment after
   routing, tool, prompt, or conversation-memory changes.
4. Evaluate the whole agent outcome:
   - factual correctness and honest uncertainty;
   - whether Elixir noticed what mattered, including where the clan actually played;
   - noise, repetition, mistimed posts, and unjustified silence;
   - routing and tool selection;
   - leadership acceptance and member correction patterns;
   - whether stories and reports preserve meaningful continuity across weeks;
   - whether prior advice or feedback changed a later comparable output;
   - cost per useful outcome where it affects a quality trade-off.
5. Compare against prior natural evidence and recently shipped fixes. A unit test proves
   code behavior; exact production use proves the member-visible problem cleared.
6. Inspect open `objective:agent` issues for decisions or multi-run work.

## Action

For a reproducible internal defect, acquire the `agent` checkout lease and fix the
prompt, tool, capability, workflow, validator, eval, or regression seam that caused it.
Define the quality bar before tuning to it. Run focused evals plus the full gates, then
commit and push.

Ask Jamie before implementing a member-visible behavior, persona, cadence, or product
direction change. Give one evidence-backed yes/no decision with the smallest valuable
version. Do not hide the decision in a proposal pipeline.

Friday's deeper pass should improve measurement only where the current evidence cannot
answer an important quality question. Do not add harnesses for their own sake.

Friday's deeper pass is also the **intelligence and efficiency baseline**. It is not a
separate report or agent:

- for any materially expensive member-facing workflow, compare canonical cost evidence
  with its natural outcome and quality signal; a cheaper model, shorter context, or
  lower cadence is not an improvement unless the relevant quality bar still holds;
- when sufficient natural examples exist, trace one feedback or learned-context path
  (leader feedback, reflection, dossier, or memory) to a later comparable decision;
  record `insufficient_sample` rather than inferring learning from the mechanism's
  existence; and
- turn a proven quality/cost regression into one source fix and regression, not a
  standing optimizer backlog.

On the first Friday pass under this objective, establish the scorecard baseline from
existing evidence: action outcomes and staleness, story/report delivery and factual
corrections, verified report reach, eligible report-to-report continuity, and any
already traceable learning loops. Keep the result in the compact automation memory;
do not create a new dashboard or ledger unless an important question cannot be
answered from current evidence.

On Friday, also take a small team-health pulse from the week's outcomes: look for
duplicate work, manufactured findings, lease collisions, stalled decisions, or an
objective that repeatedly stops short of natural acceptance. Change an objective
contract only when that evidence supports one specific correction. Do not create a
digest, meta ticket, or standing manager ceremony.

## Success

- the three-season scorecard is satisfied from natural production evidence;
- `#actions`, weekly stories, and personal reports remain trusted flagship surfaces;
- reports and stories show warranted longitudinal continuity rather than resetting;
- at least three feedback-to-behavior learning loops are proven end to end;
- exact defects become source fixes and regressions, not vague quality tickets;
- Ask Elixir alignment, tool choice, and factual honesty remain above explicit bars;
- quality decisions reach Jamie directly and concisely; and
- a healthy window produces no work.

At the end of Season 138, close this objective only if the evidence demonstrates the
loop. Otherwise report which link failed and ask Jamie whether to extend, narrow, or
replace the objective; do not declare success because the mechanisms exist.
