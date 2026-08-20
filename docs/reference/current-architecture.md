# Current Architecture

This is the concise map of Elixir's live architecture. `AGENTS.md` owns the
full operating contract; the locked `v5.1/` documents preserve the clean-break
rationale and may describe components later retired from production.

## Data flow

1. `cr_api.py` is the only Clash Royale ingress. Every successful request gets
   an append-only `api_observation_receipts` row under its true endpoint;
   identical bodies share one hash-deduplicated `raw_api_payloads` content row.
2. `engine/observations.py` admits payloads, and the normalizers/projectors in
   `engine/` convert API-shaped data into canonical tags, timestamps, and
   domain values.
3. Emitters compare canonical state to `state_baselines`. A first sight creates
   a baseline but no event; a real delta is applied through an invariant-checked
   change set and advances the baseline only after its writes succeed.
4. The durable streams are `battle_events`, `player_events`, `clan_events`, and
   `war_events`. Projections and rollups are derived from
   those streams; identity, tenure, awards, and recognition claims are durable
   records rather than disposable projections.
5. `capabilities/` packages canonical domain answers for tools, awareness,
   reports, memory synthesis, and admin. Consumers choose wording
   and presentation; they do not independently recalculate the facts.
6. The awareness stack has two authors and one delivery owner. The daily brain
   deliberates over the whole read; nine scoped responder jobs handle qualifying
   events between brain runs. Both validate complete plans and persist them to
   `awareness_delivery_intents` before delivery. Discord receipts also enter
   `awareness_posts`; clan-chat-only follow-ups are durable intents whose relay
   card must succeed before fulfillment. An allowed no-post result is an explicit
   `choose_silence` tool call, never inferred from an empty model response.
7. Interactive Discord turns use the same capability facts and keep linked
   history in `messages`. `interactive` and `deck_review` may schedule one
   bounded follow-up per turn. Nightly reflection can use those linked messages
   as dossier evidence, but persistence accepts only exact source references.
8. `agent/workflow_registry.py` is the single workflow policy source: model
   family, tool surface, write permission/budget, rounds, output ceiling,
   reasoning effort, and timeout are projections of one `WorkflowSpec` row.

## Ownership boundaries

| Concern | Owner | Durable evidence |
|---|---|---|
| API capture | `cr_api.py` | `api_observation_receipts`, `raw_api_payloads` |
| Canonical state transitions | `engine/` change sets and emitters | baselines, streams, identity tables |
| Shared domain meaning | `capabilities/` | versioned dictionary contracts |
| Proactive judgment and voice | awareness brain + scoped responder | `awareness_thoughts`, `awareness_delivery_intents`, `awareness_posts`, stream cursors |
| Nightly learning and member context | reflection + dossiers | evidence refs, editorial memories, `member_dossiers`, `scheduled_followups` |
| Interactive answers | agent workflows and tool policy | `messages`, scoped memories, LLM telemetry |
| Human clan decisions | management state machines and leadership UI | leader actions and revisits |
| Operational truth | activity registry, runtime status, process logging | `runtime_job_status`, tick history, `logs/elixir-error.log` |

## Retired proactive stack

`engine/recognition/`, `engine/delivery.py` and `engine/legacy_proactive.py` were
removed in #207. The awareness loop is the sole proactive owner, in code as well
as in practice — nothing is kept executable for comparison.

`recognition_ledger` went with them. It had been assumed load-bearing because it
held `award:` keys, but award idempotency is
`UNIQUE(award_type, season_id, section_index, player_tag)` on `awards` with
`INSERT OR IGNORE`; the ledger claim fired *after* and *conditional on* that
insert, and all 17 award claims carried `intent_id = NULL` — they never posted.
Nothing outside the retired package read the table.

`engine/offline.py` survives as the API-free replay harness that
`scripts/replay_gate.py` drives; its optional `legacy_proactive` seam is gone.

## Retired self-monitoring

The `runtime_incidents` ledger (`storage/incidents.py`) and the daily
`engine-health` job (`runtime/health.py`) were removed 2026-07-28; schema v20
drops the table. The ledger recorded **0 rows in 25 days** while
`logs/elixir-error.log` held **159 real errors**, and the health check read the
ledger — so it reported "all clear" through every actual failure.

Detecting production problems is an operator/AGENT-TEAM job, not an internal
function of the clan bot: a component that reports on its own health reports
from inside the failure. Every former `record_incident` call site now logs on
its module's own logger, landing in the ERROR-only rotating log that
`runtime/logging_setup.py` writes. `AGENT-TEAM/error-watch.md` (Operations
Manager) owns reading it, and carries the CR API drift query that
`check_api_drift` used to run.

## Change rule

Add a new fact or decision once, at the layer that owns its meaning. Expose it
through a versioned capability when multiple consumers need it. Add formatting
or compaction at the edge, and verify a cross-layer feature at its final
consumer rather than stopping at the producer.
