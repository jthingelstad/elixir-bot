# Elixir Memory System — Implementation Notes

## What was added

Implemented an end-to-end contextual memory subsystem with explicit storage and retrieval separation from authoritative clan/game records.

### New modules

- `memory_store/`
  - CRUD and lifecycle operations (`create`, `update`, `archive`, `soft delete`)
  - Provenance validation (`source_type`, `is_inference`, `confidence` rules)
  - Scope-aware permissions (`public`, `leadership`, `system_internal`)
  - Structured filters + hybrid retrieval (FTS + embedding similarity)
  - Reciprocal Rank Fusion merge and deterministic tie-breaks
  - Tag/evidence linkage helpers

- `memory_reasoner/`
  - Summarization helpers that keep source classes separate
  - Prompt packaging into distinct provenance sections
  - Response phrasing helpers that hedge inferences

### Database migration

Added `_migration_8` in `db/__init__.py` with:

- `clan_memories` core table
- Related tables:
  - `clan_memory_tags`
  - `clan_memory_tag_links`
  - `clan_memory_member_links`
  - `clan_memory_event_links`
  - `clan_memory_evidence_refs`
  - `clan_memory_versions`
  - `clan_memory_audit_log`
  - `clan_memory_embeddings`
  - `clan_memory_index_status`
- FTS5 virtual table + sync triggers (`clan_memories_fts`)
- Access-path indexes for scope/status/member/war/event/source lookups
- Best-effort sqlite-vec initialization (`clan_memory_vec`) with degraded-mode flag persistence in `clan_memory_index_status`

## Architectural decisions

1. **Strict separation from authoritative facts**
   - Existing `memory_facts` / `memory_episodes` and all V2 authoritative clan/game paths remain unchanged.
   - New contextual memory lives only in `clan_memory*` tables and `memory_store`/`memory_reasoner` code paths.

2. **Provenance enforcement in two layers**
   - DB `CHECK` constraints for immutable baseline guarantees.
   - Service-layer validation errors for clear developer feedback.

3. **Hybrid retrieval with graceful degradation**
   - Primary lexical retrieval uses FTS5 (`MATCH` + `bm25`).
   - Semantic retrieval uses stored embeddings (`text-embedding-3-small` expected) and cosine similarity.
   - RRF merges both rank lists.
   - If FTS fails, retrieval degrades to substring lexical ranking.
   - If embeddings are absent/unavailable, lexical-only search still works.

4. **Auditability and safe mutation**
   - Updates snapshot previous state into `clan_memory_versions`.
   - All writes add `clan_memory_audit_log` entries.
   - Archive/delete are soft-status transitions, not destructive deletes.

## Assumptions

- Existing authoritative data queries remain the source of truth for member/war/fact questions.
- `sqlite-vec` may not be present in all local/dev runtimes.
- Embeddings are generated externally and supplied via `upsert_embedding`; this repo does not run embedding API calls directly inside the store layer.

## How migrations run

Migrations run automatically through `db.get_connection()`.

No manual migration command is required.

## Embedding configuration

- Preferred embedding model: `text-embedding-3-small`.
- Query embedding can be injected into `search_memories(..., embed_query=callable)`.
- Memory embedding rows are stored in `clan_memory_embeddings` via `upsert_embedding`.

## Running tests

```bash
venv/bin/python -m pytest tests/test_memory_system.py -v
venv/bin/python -m pytest tests/test_db_v2.py -v
```

## Degraded mode behavior

- If `sqlite-vec` cannot initialize, the system marks `sqlite_vec_enabled = 0` and continues.
- If query embedding is unavailable, lexical-only search is used.
- If FTS query errors, lexical substring fallback runs against filtered candidates.
- Expired, archived, and deleted memories are excluded by default unless explicitly included.

## Manual review items before production rollout

1. Confirm `sqlite-vec` extension availability in production runtime.
2. Decide final write-policy boundaries for who can create `leader_note` vs `system` records.
3. Add runtime wiring from command/workflow handlers into `memory_store` write/read methods.
4. Add prompt-integration wiring to consume `memory_reasoner.package_prompt_context` where appropriate.
5. Validate retention policy defaults (`retention_class`, optional expiration for low-confidence inferences).
