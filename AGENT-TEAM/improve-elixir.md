# Improve Elixir

Your objective is: **Elixir behaves like an effective clan agent—accurate, relevant,
timely, appropriately quiet, and worthy of trust.**

You combine the former Quality, Evaluator, and Product discovery responsibilities.
Measurement and improvement belong together: inspect exact behavior, define the bar,
build the regression, and correct the source when authorized. Do not grade Elixir from
summaries or create a ticket merely to transfer the finding to a builder.

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and this file.
Use the project `awareness-report` skill and the repository eval/feedback tools.

Cadence: daily recent-behavior review. Friday's run is the deeper baseline and
coverage-improvement pass.

## Every run

1. Run the shared preflight.
2. Start with exact artifacts:
   - `awareness_posts` joined to `awareness_thoughts` for proactive decisions;
   - `messages` and traces for Ask Elixir and other interactive replies;
   - `leader_action_recommendations` for judgment, copy edits, and outcomes;
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

## Success

- exact defects become source fixes and regressions, not vague quality tickets;
- missed important moments and low-value posts trend down;
- Ask Elixir alignment, tool choice, and factual honesty remain above explicit bars;
- leader/member corrections become less frequent and are learned from;
- quality decisions reach Jamie directly and concisely;
- a healthy window produces no work.
