# Current Architecture

This is the concise map of Elixir's live architecture. `AGENTS.md` owns the
full operating contract; the locked `v5.1/` documents preserve the clean-break
rationale and may describe components later retired from production.

## Data flow

1. `cr_api.py` is the only Clash Royale ingress. Every response is appended to
   `raw_api_payloads` under its real endpoint before downstream use.
2. `engine/observations.py` admits payloads, and the normalizers/projectors in
   `engine/` convert API-shaped data into canonical tags, timestamps, and
   domain values.
3. Emitters compare canonical state to `state_baselines`. A first sight creates
   a baseline but no event; a real delta is applied through an invariant-checked
   change set and advances the baseline only after its writes succeed.
4. The durable streams are `battle_events`, `player_events`, `clan_events`,
   `war_events`, and `game_events`. Projections and rollups are derived from
   those streams; identity, tenure, awards, and recognition claims are durable
   records rather than disposable projections.
5. `capabilities/` packages canonical domain answers for tools, awareness,
   reports, memory synthesis, admin, and Observatory. Consumers choose wording
   and presentation; they do not independently recalculate the facts.
6. The unified awareness loop reads new stream positions plus projections and
   capability answers, makes one proactive plan, validates it, sends it, then
   records confirmed receipts in `awareness_posts`. Interactive Discord turns
   use the same capability/tool facts but keep their conversation history in
   `messages`.

## Ownership boundaries

| Concern | Owner | Durable evidence |
|---|---|---|
| API capture | `cr_api.py` | `raw_api_payloads` |
| Canonical state transitions | `engine/` change sets and emitters | baselines, streams, identity tables |
| Shared domain meaning | `capabilities/` | versioned dictionary contracts |
| Proactive judgment and voice | awareness workflow | `awareness_thoughts`, `awareness_posts`, stream cursors |
| Interactive answers | agent workflows and tool policy | `messages`, scoped memories, LLM telemetry |
| Human clan decisions | management state machines and leadership UI | decision cases, leader actions, revisits |
| Operational truth | activity registry, incidents, runtime status | `runtime_job_status`, `runtime_incidents`, tick history |

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

## Change rule

Add a new fact or decision once, at the layer that owns its meaning. Expose it
through a versioned capability when multiple consumers need it. Add formatting
or compaction at the edge, and verify a cross-layer feature at its final
consumer rather than stopping at the producer.
