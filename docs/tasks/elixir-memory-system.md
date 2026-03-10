# Elixir Memory System

## Overview

Implement a persistent, scalable memory subsystem for **Elixir**, the Clash Royale clan assistant. This system must augment Elixir’s operational continuity and leadership usefulness without contaminating authoritative clan and game records.

Elixir already has structured, authoritative data from the Clash Royale API and clan systems. This new subsystem is for contextual memory: leadership notes, assistant-generated inferences, operational observations, policy history, and event-linked notes.

This is not a replacement for official clan records. It is a separate subsystem with separate storage, retrieval, provenance, and presentation rules.

—

## Objectives

Build a memory subsystem that can:

1. Store structured, context-rich notes and observations about clan members, leadership decisions, clan operations, and major clan events.
2. Link memories to members, roles, war seasons, war weeks, promotions, POAP drops, Free Pass rounds, and other clan milestones.
3. Support efficient retrieval through structured filtering, keyword search, and semantic search.
4. Generate memory-based briefings for leadership and operational continuity.
5. Enforce privacy boundaries between public clan memory and internal leadership memory.
6. Preserve provenance and confidence so that assistant inferences are never treated as authoritative facts.
7. Remain maintainable, testable, and extensible for future features such as coaching, recognition triggers, and operational automation.

—

## Non-Negotiable Design Principles

### 1. Facts and memories must be strictly separated

Facts and memories are fundamentally different and must remain separate in both storage and retrieval.

- **Facts** are authoritative, immutable records from official data sources such as Clash Royale API data and structured clan databases.
- **Memories** are contextual records such as observations, notes, interpretations, decisions, or inferences.

Requirements:

- Facts and memories must **not** be stored in the same table.
- Facts and memories must **not** be retrieved via the same code path.
- Facts and memories must **not** be ranked or presented as equivalent forms of evidence.
- The memory system may link to fact records by reference, but must not duplicate authoritative fact payloads unnecessarily.

### 2. Every memory row must carry provenance

Every memory record must include these required fields:

- `source_type` — enum: `leader_note | elixir_inference | system`
- `is_inference` — boolean
- `confidence` — float between `0.0` and `1.0`

Rules:

- Human-authored leadership notes must use `is_inference = false`
- Elixir-generated inferences must use `is_inference = true`
- Elixir-generated inferences must never have `confidence = 1.0`
- All records must preserve source attribution
- Provenance must be returned during retrieval and available to response composition logic

### 3. Response composition must preserve epistemic clarity

When Elixir composes an answer using both facts and memories:

- facts must be presented as authoritative data
- memories must be presented as attributed contextual information
- inferences must be hedged appropriately
- memories must never be phrased with the same certainty as facts

Examples:

- Fact phrasing:
  - “battle logs show”
  - “war participation data indicates”
  - “donation totals are”
- Memory phrasing:
  - “leadership noted”
  - “an internal note suggests”
  - “it appeared that”
  - “Elixir inferred with moderate confidence”

Elixir must never present an inference as though it were a confirmed fact.

—

## Scope of Work

Implement a memory subsystem with the following capabilities:

- persistence
- structured metadata
- tagging and contextual linking
- access control
- hybrid retrieval
- summarization
- provenance-aware formatting support
- tests
- implementation notes

This task should include real implementation work where possible, not just design documentation.

—

## Architecture Requirements

Design the system as two explicit modules:

### `memory_store`
Responsible for:

- schema
- migrations
- CRUD
- indexing
- structured filtering
- hybrid retrieval
- ranking
- permissions
- provenance integrity
- auditability

### `memory_reasoner`
Responsible for:

- summarization
- inference generation hooks
- retrieval packaging for prompts
- provenance-aware formatting helpers
- response-safe composition support

Keep these boundaries explicit in code.

—

## Data Model Requirements

Use SQLite as the primary store.

### Core memory table

Create a primary memory table with fields equivalent to:

- `id`
- `created_at`
- `updated_at`
- `created_by`
- `updated_by`
- `source_type`
- `is_inference`
- `confidence`
- `scope` — enum like `public | leadership | system_internal`
- `status` — enum like `active | archived | deleted`
- `title` — nullable short label
- `body` — main memory text
- `summary` — nullable normalized short summary
- `member_id` — nullable stable internal member reference
- `member_tag` — nullable Clash Royale member tag
- `role` — nullable role context if relevant
- `channel_id` — nullable communication or context channel
- `war_season_id` — nullable
- `war_week_id` — nullable
- `event_type` — nullable, examples:
  - `poap_drop`
  - `promotion`
  - `free_pass`
  - `discipline`
  - `recognition`
  - `leadership_decision`
  - `attendance`
  - `engagement`
- `event_id` — nullable internal or external event reference
- `retention_class`
- `expires_at` — nullable
- `metadata_json` — extensible structured metadata
- `embedding_model` — nullable
- `embedding_created_at` — nullable

### Related tables

Add normalized support tables as appropriate for:

- `memory_tags`
- `memory_tag_links`
- `memory_member_links` if needed beyond direct member fields
- `memory_event_links`
- `memory_evidence_refs`
- `memory_audit_log` or `memory_versions`

### Audit/versioning

Edits must be inspectable.

Requirements:

- preserve change history for memory edits
- track who changed a memory and when
- keep original provenance intact
- support soft delete rather than immediate destructive deletion

—

## Retrieval Requirements

Implement hybrid retrieval combining:

- **SQLite FTS5** for lexical and BM25-style retrieval
- **sqlite-vec** for vector similarity search
- **OpenAI `text-embedding-3-small`** for embeddings

### Retrieval behavior

When searching memories:

1. Apply permission and scope filters first
2. Apply structured filters second
3. Run lexical search and vector search in parallel where practical
4. Merge results using **Reciprocal Rank Fusion**
5. Return ranked results with provenance attached

### Structured filters

Support filtering by:

- `member_id`
- `member_tag`
- `role`
- `war_season_id`
- `war_week_id`
- `event_type`
- `event_id`
- `scope`
- `source_type`
- `is_inference`
- `status`
- date range

### Search requirements

The retrieval system must support both:

#### exact lookups
Examples:

- player tag lookups
- card names
- POAP drop names
- specific war week identifiers
- specific policy/event names

#### semantic lookups
Examples:

- “members showing disengagement signals”
- “recent leadership concern about war consistency”
- “who has leadership been encouraging lately”
- “players who may need recognition”
- “what changed in leadership policy over the last few weeks”

### Ranking requirements

Implement Reciprocal Rank Fusion with documented behavior.

Document:

- RRF formula used
- rank window
- tie-breaking approach
- any recency boost or stale-memory penalty
- any low-confidence penalty
- handling when vector search is unavailable

Degraded mode must still work with FTS-only retrieval.

—

## Write Path Requirements

Implement write support for at least these creation types:

### 1. Leader note
Human-authored operational or leadership note.

Examples:

- “Leadership noted that player X communicated reduced availability this week.”
- “Promote player Y after sustained war reliability.”

Rules:

- `source_type = leader_note`
- `is_inference = false`
- confidence still required, likely `1.0` or project-defined convention for human notes

### 2. Elixir inference
Assistant-generated interpretation from patterns or repeated behavior.

Examples:

- likely disengagement
- positive consistency trend
- possible coaching opportunity
- possible recognition candidate

Rules:

- `source_type = elixir_inference`
- `is_inference = true`
- `confidence < 1.0`
- store rationale or evidence references where possible
- must not overwrite or mutate fact records

### 3. System memory
System-generated operational memory.

Examples:

- policy update
- recognition event creation
- season summary creation
- moderation state transition

Rules:

- `source_type = system`
- provenance must remain explicit

—

## Evidence Linkage Requirements

Memories should support lightweight evidence linkage without duplicating authoritative records.

Examples of evidence references:

- war week identifier
- donation summary snapshot id
- battle participation snapshot id
- conversation or command reference
- event record id
- policy revision id

Implement a structured way to store and retrieve evidence references.

—

## Access Control and Privacy

Implement scope-aware access boundaries.

At minimum support:

- `public`
- `leadership`
- `system_internal`

Requirements:

- public retrieval contexts must not expose leadership-only memory
- system-internal memory must not be surfaced unless explicitly allowed
- permissions must be enforced in backend/service code, not only in UI
- write permissions should distinguish leadership note creation from system or assistant-generated memory creation

Also support retention controls:

- configurable retention by scope or type
- archive vs delete semantics
- optional expiration of low-confidence inferences
- exclusion of stale or expired memories from normal retrieval unless explicitly requested

—

## Summarization Requirements

Implement memory summarization support for:

- a member
- a war week
- a war season
- a leadership topic
- a date range
- a policy/change history view

Summary output must explicitly distinguish:

1. authoritative facts
2. leadership notes
3. assistant inferences
4. open questions or items needing confirmation

Do not collapse these into a single undifferentiated narrative.

—

## Prompt/Response Assembly Requirements

Implement or scaffold a provenance-aware response assembly layer.

Requirements:

- retrieve facts and memories separately
- package them into distinct sections in internal prompt context
- ensure downstream generation logic can distinguish source classes
- provide formatting helpers or data structures that preserve provenance
- explicitly support hedged language for memories and direct language for facts

### Example internal context shape

Use or approximate a structure like:

- `facts[]`
- `leadership_memories[]`
- `assistant_inferences[]`
- `system_notes[]`

Each returned item should include provenance fields sufficient for safe downstream use.

—

## API / Service Surface

Implement internal service methods or endpoints for at least:

### Write operations
- create memory
- update memory
- archive memory
- soft delete memory
- attach tags
- attach evidence references

### Read operations
- get memory by id
- list memories by member
- list memories by event
- list recent leadership memories
- search memories with filters
- retrieve top relevant memories for a given prompt or context

### Summarization operations
- summarize member memories
- summarize war week memories
- summarize war season memories
- summarize topic/date range memories

If the codebase exposes HTTP or command APIs, add endpoints or commands in the project’s existing style. Otherwise add clean service-layer interfaces.

—

## Suggested File / Module Deliverables

Adapt naming to the codebase, but aim for something like:

- `memory_store/`
  - schema/migrations
  - repository or DAO layer
  - retrieval logic
  - ranking logic
  - permission checks
- `memory_reasoner/`
  - summary functions
  - inference hooks
  - prompt packaging
  - provenance-aware formatting helpers
- `tests/`
  - schema tests
  - retrieval tests
  - permission tests
  - summarization tests
  - provenance tests

Also add:

- `IMPLEMENTATION_NOTES.md`

—

## Migration Requirements

Provide actual SQLite migrations for:

- core memory table
- tag tables
- evidence reference tables
- audit/version tables
- FTS5 virtual tables
- sqlite-vec structures
- indexes

Index for the expected access patterns.

Likely indexes include combinations involving:

- `scope`
- `status`
- `member_id`
- `member_tag`
- `war_season_id`
- `war_week_id`
- `event_type`
- `created_at`
- `source_type`

—

## Performance Requirements

Optimize for pragmatic local performance and maintainability.

Requirements:

- apply structured narrowing before expensive retrieval where possible
- support incremental embedding generation
- support re-embedding on memory updates
- avoid full-table scans for common access paths
- preserve acceptable latency for operational queries

Do not over-engineer for distributed infrastructure unless clearly required by the existing project architecture.

—

## Failure Handling Requirements

Implement graceful degradation.

Examples:

- if embeddings fail, lexical retrieval must still work
- if vector store is unavailable, FTS retrieval should remain functional
- if a memory is expired or archived, default retrieval should exclude it unless requested
- if a user lacks permission for leadership scope, those rows must never be returned

Document degraded-mode behavior clearly.

—

## Testing Requirements

Write tests covering at minimum:

### Separation
- facts and memories use separate storage paths
- facts and memories use separate retrieval paths

### Provenance
- all memory rows require `source_type`, `is_inference`, and `confidence`
- Elixir inferences cannot be stored with `confidence = 1.0`
- provenance is returned during retrieval

### Permissions
- leadership-only memories are not retrievable in public contexts
- system-internal memories are hidden by default

### Filtering
- filters by member, tag, role, war season, war week, event type, scope, and date range work correctly

### Hybrid retrieval
- lexical exact-match lookups return expected memories
- semantic lookups return relevant memories
- RRF merge behaves deterministically

### Summarization
- summaries distinguish facts, leader notes, and inferences
- summaries preserve attribution and confidence where applicable

### Safe phrasing support
- response composition helpers do not flatten inferences into factual statements
- memory-derived content is marked or formatted for hedged phrasing

—

## Acceptance Criteria

The task is complete only if all of the following are true:

1. Facts and memories are stored separately.
2. Facts and memories are retrieved through separate code paths.
3. Every memory row includes `source_type`, `is_inference`, and `confidence`.
4. Elixir-generated inferences cannot be saved with `confidence = 1.0`.
5. Memories can be linked to members, events, war seasons, and war weeks.
6. Hybrid retrieval is implemented with FTS5, sqlite-vec, and Reciprocal Rank Fusion.
7. Structured filters narrow the search candidate set before or during retrieval.
8. Search results preserve provenance and confidence.
9. Leadership memories are protected from public retrieval contexts.
10. Summaries clearly separate authoritative facts from leadership notes and assistant inferences.
11. The system supports degraded FTS-only operation when embeddings are unavailable.
12. Tests exist for separation, provenance, retrieval, permissions, and summarization.
13. `IMPLEMENTATION_NOTES.md` is added and explains setup, assumptions, migrations, tests, and rollout considerations.

—

## Implementation Notes File

Add an `IMPLEMENTATION_NOTES.md` with:

- what was added
- architectural decisions
- assumptions about existing Elixir models and services
- how to run migrations
- how to configure embeddings
- how to run tests
- degraded-mode behavior
- manual review items before production rollout

—

## Rollout Guidance

Implement incrementally and safely.

Suggested order:

1. inspect current schema and authoritative data paths
2. add memory schema and migrations
3. implement CRUD and permissions
4. implement FTS retrieval
5. implement vector retrieval
6. implement RRF merge
7. implement summary/reasoning layer
8. add tests
9. add implementation notes

Do not refactor authoritative fact storage into the memory subsystem.

—

## Nice-to-Have Features

If time permits, consider:

- duplicate or near-duplicate memory detection
- memory pinning
- confidence decay over time for certain inference types
- reminder/follow-up hooks
- event-driven automatic memory creation
- member memory rollups that preserve provenance boundaries

These are secondary to the core requirements above.

—

## Final Instruction

Do not only provide a design document. Implement as much of this as possible in code within the existing repository conventions.

Where project context is missing, make reasonable assumptions, isolate them clearly, and document them in `IMPLEMENTATION_NOTES.md`.

Prioritize correctness, provenance integrity, maintainability, and clear separation between authoritative records and contextual memory.
