from __future__ import annotations

import functools
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from db import _canon_tag, _enable_sqlite_vec, _json_or_none, _utcnow

# ---------------------------------------------------------------------------
# Durable-memory connection seam (elixir-v5-memory.db)
#
# clan_memories* live in their own DB, separate from the operational store. The
# memory connection does NOT run the v4 migration chain / schema-compat guard
# (db.get_connection), which would wipe a memory-only file; it just ensures the
# clan_memory schema and loads sqlite-vec. foreign_keys stays OFF because the
# memory DB has no `members` table for clan_memories.member_id to reference.
#
# CLAN_MEMORY_SCHEMA_SQL mirrors the CURRENT (post-migration-29) clan_memory
# schema. If a future migration alters any clan_memory* table, update this too —
# test_memory_db_schema_matches_operational guards against drift.
# ---------------------------------------------------------------------------
CLAN_MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clan_memories (
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    source_type TEXT NOT NULL,
    is_inference INTEGER NOT NULL,
    confidence REAL NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT,
    body TEXT NOT NULL,
    summary TEXT,
    member_id INTEGER,
    member_tag TEXT,
    role TEXT,
    channel_id TEXT,
    war_season_id TEXT,
    war_week_id TEXT,
    event_type TEXT,
    event_id TEXT,
    retention_class TEXT NOT NULL DEFAULT 'standard',
    expires_at TEXT,
    metadata_json TEXT,
    embedding_model TEXT,
    embedding_created_at TEXT,
    FOREIGN KEY(member_id) REFERENCES members(member_id) ON DELETE SET NULL,
    CHECK(source_type IN ('leader_note', 'elixir_inference', 'elixir_synthesis', 'system')),
    CHECK(scope IN ('public', 'leadership', 'system_internal')),
    CHECK(status IN ('active', 'archived', 'deleted')),
    CHECK(is_inference IN (0, 1)),
    CHECK(confidence >= 0.0 AND confidence <= 1.0),
    CHECK(source_type != 'elixir_inference' OR (is_inference = 1 AND confidence < 1.0))
);

CREATE TABLE IF NOT EXISTS clan_memory_tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clan_memory_tag_links (
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES clan_memory_tags(tag_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id, tag_id)
);

CREATE TABLE IF NOT EXISTS clan_memory_member_links (
    memory_member_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
    member_tag TEXT,
    relation_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clan_memory_event_links (
    memory_event_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, event_type, event_id)
);

CREATE TABLE IF NOT EXISTS clan_memory_evidence_refs (
    evidence_ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_label TEXT,
    evidence_url TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clan_memory_versions (
    memory_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    title TEXT,
    body TEXT,
    summary TEXT,
    status TEXT,
    scope TEXT,
    metadata_json TEXT,
    confidence REAL,
    UNIQUE(memory_id, version_number)
);

CREATE TABLE IF NOT EXISTS clan_memory_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS clan_memory_embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES clan_memories(memory_id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clan_memory_index_status (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO clan_memory_index_status (key, value) VALUES ('sqlite_vec_enabled', '0');

CREATE VIRTUAL TABLE IF NOT EXISTS clan_memories_fts USING fts5(
    title,
    summary,
    body,
    content='clan_memories',
    content_rowid='memory_id'
);

CREATE TRIGGER IF NOT EXISTS clan_memories_ai AFTER INSERT ON clan_memories BEGIN
    INSERT INTO clan_memories_fts(rowid, title, summary, body)
    VALUES (new.memory_id, new.title, new.summary, new.body);
END;

CREATE TRIGGER IF NOT EXISTS clan_memories_ad AFTER DELETE ON clan_memories BEGIN
    INSERT INTO clan_memories_fts(clan_memories_fts, rowid, title, summary, body)
    VALUES('delete', old.memory_id, old.title, old.summary, old.body);
END;

CREATE TRIGGER IF NOT EXISTS clan_memories_au AFTER UPDATE ON clan_memories BEGIN
    INSERT INTO clan_memories_fts(clan_memories_fts, rowid, title, summary, body)
    VALUES('delete', old.memory_id, old.title, old.summary, old.body);
    INSERT INTO clan_memories_fts(rowid, title, summary, body)
    VALUES (new.memory_id, new.title, new.summary, new.body);
END;

CREATE INDEX IF NOT EXISTS idx_clan_memories_scope_status_created
    ON clan_memories(scope, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clan_memories_member
    ON clan_memories(member_id, member_tag, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clan_memories_war
    ON clan_memories(war_season_id, war_week_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clan_memories_event
    ON clan_memories(event_type, event_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clan_memories_source
    ON clan_memories(source_type, is_inference, confidence, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clan_memory_evidence_lookup
    ON clan_memory_evidence_refs(memory_id, evidence_type, evidence_ref);
CREATE INDEX IF NOT EXISTS idx_clan_memory_member_links_lookup
    ON clan_memory_member_links(member_id, member_tag, relation_type);
CREATE INDEX IF NOT EXISTS idx_clan_memory_event_links_lookup
    ON clan_memory_event_links(event_type, event_id);
"""


def _memory_db_path() -> str:
    """Resolve the memory DB path per-call so the test conftest's env/config
    overrides are always honored."""
    from event_core import config

    return os.environ.get("ELIXIR_V5_MEMORY_DB") or config.MEMORY_DB


def _ensure_memory_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CLAN_MEMORY_SCHEMA_SQL)
    conn.commit()


def get_memory_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection to the durable-memory DB (clan_memories*).

    Unlike db.get_connection this does NOT run the v4 migration chain or the
    schema-compat guard — those are for the operational DB and would wipe a
    memory-only file. It ensures the clan_memory schema and loads sqlite-vec.
    """
    path = os.fspath(db_path or _memory_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # foreign_keys intentionally OFF: the memory DB has no `members` table.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    _ensure_memory_schema(conn)
    _enable_sqlite_vec(conn)  # loads vec0 + flips clan_memory_index_status
    return conn


def managed_memory_connection(fn: Callable) -> Callable:
    """Like db.managed_connection but defaults to the durable-memory DB."""
    @functools.wraps(fn)
    def wrapper(*args, conn=None, **kwargs):
        close = conn is None
        conn = conn or get_memory_connection()
        try:
            return fn(*args, conn=conn, **kwargs)
        finally:
            if close:
                conn.close()
    return wrapper


SOURCE_TYPES = {"leader_note", "elixir_inference", "elixir_synthesis", "system"}
SCOPES = {"public", "leadership", "system_internal"}
STATUSES = {"active", "archived", "deleted"}


class MemoryValidationError(ValueError):
    pass


@dataclass
class MemorySearchResult:
    memory: dict
    rank_score: float
    components: dict


def _validate_provenance(source_type: str, is_inference: bool, confidence: float) -> None:
    if source_type not in SOURCE_TYPES:
        raise MemoryValidationError(f"invalid source_type: {source_type}")
    if not (0.0 <= float(confidence) <= 1.0):
        raise MemoryValidationError("confidence must be between 0.0 and 1.0")
    if source_type == "leader_note" and is_inference:
        raise MemoryValidationError("leader_note memories cannot be marked as inference")
    if source_type == "elixir_inference" and not is_inference:
        raise MemoryValidationError("elixir_inference memories must set is_inference=true")
    if source_type == "elixir_inference" and float(confidence) >= 1.0:
        raise MemoryValidationError("elixir_inference confidence must be less than 1.0")
    # elixir_synthesis: arc memories written by the weekly synthesis job.
    # These are canonical, cross-referenced, human-digestible summaries — not
    # hedged inferences — so they can carry confidence=1.0 and is_inference=0.


def _allowed_scopes(viewer_scope: str, include_system_internal: bool = False) -> tuple[str, ...]:
    if viewer_scope == "public":
        return ("public",)
    if viewer_scope == "leadership":
        scopes = ["public", "leadership"]
        if include_system_internal:
            scopes.append("system_internal")
        return tuple(scopes)
    if viewer_scope == "system_internal":
        return ("public", "leadership", "system_internal")
    raise MemoryValidationError(f"invalid viewer scope: {viewer_scope}")


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return text
    return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_filter_where(filters: Optional[dict], args: list) -> str:
    filters = filters or {}
    clauses = []
    if filters.get("member_id") is not None:
        clauses.append("m.member_id = ?")
        args.append(filters["member_id"])
    if filters.get("member_tag"):
        clauses.append("m.member_tag = ?")
        args.append(_canon_tag(filters["member_tag"]))
    for key in ("role", "war_season_id", "war_week_id", "event_type", "event_id", "scope", "source_type", "status"):
        value = filters.get(key)
        if value is not None:
            clauses.append(f"m.{key} = ?")
            args.append(value)
    if filters.get("is_inference") is not None:
        clauses.append("m.is_inference = ?")
        args.append(1 if filters.get("is_inference") else 0)
    created_after = _normalize_date(filters.get("created_after"))
    created_before = _normalize_date(filters.get("created_before"))
    if created_after:
        clauses.append("m.created_at >= ?")
        args.append(created_after)
    if created_before:
        clauses.append("m.created_at <= ?")
        args.append(created_before)
    return (" AND " + " AND ".join(clauses)) if clauses else ""


def _fetch_memory(conn, memory_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM clan_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["metadata_json"] = json.loads(item["metadata_json"] or "{}")
    item["tags"] = [
        r["tag"] for r in conn.execute(
            "SELECT t.tag FROM clan_memory_tags t JOIN clan_memory_tag_links l ON l.tag_id = t.tag_id WHERE l.memory_id = ? ORDER BY t.tag ASC",
            (memory_id,),
        ).fetchall()
    ]
    evidence = conn.execute(
        "SELECT evidence_type, evidence_ref, evidence_label, evidence_url, metadata_json, created_at "
        "FROM clan_memory_evidence_refs WHERE memory_id = ? ORDER BY evidence_ref_id ASC",
        (memory_id,),
    ).fetchall()
    item["evidence_refs"] = [
        {
            **dict(r),
            "metadata_json": json.loads(r["metadata_json"] or "{}"),
        }
        for r in evidence
    ]
    return item


@managed_memory_connection
def create_memory(*, body: str, source_type: str, is_inference: bool, confidence: float,
                  created_by: str, scope: str = "leadership", status: str = "active",
                  title: Optional[str] = None, summary: Optional[str] = None,
                  member_id: Optional[int] = None, member_tag: Optional[str] = None,
                  role: Optional[str] = None, channel_id: Optional[str] = None,
                  war_season_id: Optional[str] = None, war_week_id: Optional[str] = None,
                  event_type: Optional[str] = None, event_id: Optional[str] = None,
                  retention_class: str = "standard", expires_at: Optional[str] = None,
                  metadata: Optional[dict] = None, conn=None) -> dict:
    if scope not in SCOPES:
        raise MemoryValidationError(f"invalid scope: {scope}")
    if status not in STATUSES:
        raise MemoryValidationError(f"invalid status: {status}")
    _validate_provenance(source_type, is_inference, confidence)
    now = _utcnow()
    cur = conn.execute(
        "INSERT INTO clan_memories (created_at, updated_at, created_by, updated_by, source_type, is_inference, confidence, "
        "scope, status, title, body, summary, member_id, member_tag, role, channel_id, war_season_id, war_week_id, event_type, "
        "event_id, retention_class, expires_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            now,
            created_by,
            created_by,
            source_type,
            1 if is_inference else 0,
            float(confidence),
            scope,
            status,
            title,
            body,
            summary,
            member_id,
            _canon_tag(member_tag) if member_tag else None,
            role,
            str(channel_id) if channel_id else None,
            war_season_id,
            war_week_id,
            event_type,
            event_id,
            retention_class,
            _normalize_date(expires_at),
            _json_or_none(metadata or {}),
        ),
    )
    memory_id = cur.lastrowid
    conn.execute(
        "INSERT INTO clan_memory_audit_log (memory_id, changed_at, changed_by, action, payload_json) VALUES (?, ?, ?, 'create', ?)",
        (memory_id, now, created_by, _json_or_none({"source_type": source_type, "scope": scope})),
    )
    conn.commit()
    return _fetch_memory(conn, memory_id)


@managed_memory_connection
def attach_tags(memory_id: int, tags: Iterable[str], *, actor: str, conn=None) -> list[str]:
    clean = sorted({t.strip().lower() for t in (tags or []) if t and t.strip()})
    for tag in clean:
        conn.execute("INSERT OR IGNORE INTO clan_memory_tags (tag, created_at) VALUES (?, ?)", (tag, _utcnow()))
        tag_row = conn.execute("SELECT tag_id FROM clan_memory_tags WHERE tag = ?", (tag,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO clan_memory_tag_links (memory_id, tag_id, created_at) VALUES (?, ?, ?)",
            (memory_id, tag_row["tag_id"], _utcnow()),
        )
    conn.execute(
        "INSERT INTO clan_memory_audit_log (memory_id, changed_at, changed_by, action, payload_json) VALUES (?, ?, ?, 'attach_tags', ?)",
        (memory_id, _utcnow(), actor, _json_or_none({"tags": clean})),
    )
    conn.commit()
    return clean


@managed_memory_connection
def attach_evidence_ref(memory_id: int, *, evidence_type: str, evidence_ref: str,
                        actor: str, evidence_label: Optional[str] = None,
                        evidence_url: Optional[str] = None, metadata: Optional[dict] = None,
                        conn=None) -> None:
    conn.execute(
        "INSERT INTO clan_memory_evidence_refs (memory_id, evidence_type, evidence_ref, evidence_label, evidence_url, metadata_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (memory_id, evidence_type, evidence_ref, evidence_label, evidence_url, _json_or_none(metadata or {}), _utcnow()),
    )
    conn.execute(
        "INSERT INTO clan_memory_audit_log (memory_id, changed_at, changed_by, action, payload_json) VALUES (?, ?, ?, 'attach_evidence', ?)",
        (memory_id, _utcnow(), actor, _json_or_none({"evidence_type": evidence_type, "evidence_ref": evidence_ref})),
    )
    conn.commit()


@managed_memory_connection
def update_memory(memory_id: int, *, actor: str, conn=None, **updates) -> dict:
    current = _fetch_memory(conn, memory_id)
    if not current:
        raise MemoryValidationError(f"memory not found: {memory_id}")
    merged = dict(current)
    merged.update({k: v for k, v in updates.items() if v is not None})
    _validate_provenance(merged["source_type"], bool(merged["is_inference"]), float(merged["confidence"]))

    version = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS version_no FROM clan_memory_versions WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()["version_no"] + 1
    conn.execute(
        "INSERT INTO clan_memory_versions (memory_id, version_number, changed_at, changed_by, title, body, summary, status, scope, metadata_json, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            version,
            _utcnow(),
            actor,
            current.get("title"),
            current.get("body"),
            current.get("summary"),
            current.get("status"),
            current.get("scope"),
            _json_or_none(current.get("metadata_json") or {}),
            current.get("confidence"),
        ),
    )
    allowed = {
        "title", "body", "summary", "status", "scope", "confidence", "source_type", "is_inference", "member_id", "member_tag",
        "role", "channel_id", "war_season_id", "war_week_id", "event_type", "event_id", "retention_class", "expires_at", "metadata_json",
    }
    cols = []
    args = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        if key == "member_tag" and value:
            value = _canon_tag(value)
        if key == "is_inference":
            value = 1 if value else 0
        if key == "metadata":
            key = "metadata_json"
            value = _json_or_none(value)
        if key == "expires_at":
            value = _normalize_date(value)
        cols.append(f"{key} = ?")
        args.append(value)
    cols.extend(["updated_at = ?", "updated_by = ?"])
    args.extend([_utcnow(), actor, memory_id])
    conn.execute(f"UPDATE clan_memories SET {', '.join(cols)} WHERE memory_id = ?", args)
    conn.execute(
        "INSERT INTO clan_memory_audit_log (memory_id, changed_at, changed_by, action, payload_json) VALUES (?, ?, ?, 'update', ?)",
        (memory_id, _utcnow(), actor, _json_or_none({"fields": sorted([k for k in updates if k in allowed])})),
    )
    conn.commit()
    return _fetch_memory(conn, memory_id)


def archive_memory(memory_id: int, *, actor: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    return update_memory(memory_id, actor=actor, status="archived", conn=conn)


def soft_delete_memory(memory_id: int, *, actor: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    return update_memory(memory_id, actor=actor, status="deleted", conn=conn)


@managed_memory_connection
def get_memory(memory_id: int, *, viewer_scope: str = "leadership", include_system_internal: bool = False,
               include_archived: bool = False, include_deleted: bool = False, conn=None) -> Optional[dict]:
    scopes = _allowed_scopes(viewer_scope, include_system_internal=include_system_internal)
    row = _fetch_memory(conn, memory_id)
    if not row:
        return None
    if row["scope"] not in scopes:
        return None
    if row["status"] == "deleted" and not include_deleted:
        return None
    if row["status"] == "archived" and not include_archived:
        return None
    if row.get("expires_at") and row["expires_at"] <= _utcnow():
        return None
    return row


@managed_memory_connection
def list_memories(*, viewer_scope: str = "leadership", include_system_internal: bool = False,
                  include_archived: bool = False, include_deleted: bool = False,
                  filters: Optional[dict] = None, limit: int = 50, conn=None) -> list[dict]:
    scopes = _allowed_scopes(viewer_scope, include_system_internal=include_system_internal)
    args: list = list(scopes)
    sql = (
        "SELECT m.memory_id FROM clan_memories m WHERE m.scope IN ({}) ".format(",".join("?" for _ in scopes))
    )
    if not include_archived:
        sql += " AND m.status != 'archived'"
    if not include_deleted:
        sql += " AND m.status != 'deleted'"
    sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
    args.append(_utcnow())
    sql += _build_filter_where(filters, args)
    sql += " ORDER BY m.created_at DESC, m.memory_id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [_fetch_memory(conn, row["memory_id"]) for row in rows]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@managed_memory_connection
def upsert_embedding(memory_id: int, embedding: list[float], *, model: str = "text-embedding-3-small", conn=None) -> None:
    now = _utcnow()
    payload = json.dumps(embedding)
    conn.execute(
        "INSERT INTO clan_memory_embeddings (memory_id, embedding_model, vector_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(memory_id) DO UPDATE SET embedding_model = excluded.embedding_model, vector_json = excluded.vector_json, updated_at = excluded.updated_at",
        (memory_id, model, payload, now, now),
    )
    conn.execute(
        "UPDATE clan_memories SET embedding_model = ?, embedding_created_at = ? WHERE memory_id = ?",
        (model, now, memory_id),
    )
    conn.commit()


def _rrf_merge(lexical: list[int], vector: list[int], *, k: int = 60) -> dict[int, dict]:
    scores: dict[int, dict] = {}
    for rank, memory_id in enumerate(lexical, start=1):
        item = scores.setdefault(memory_id, {"rrf": 0.0, "lexical_rank": None, "vector_rank": None})
        item["rrf"] += 1.0 / (k + rank)
        item["lexical_rank"] = rank
    for rank, memory_id in enumerate(vector, start=1):
        item = scores.setdefault(memory_id, {"rrf": 0.0, "lexical_rank": None, "vector_rank": None})
        item["rrf"] += 1.0 / (k + rank)
        item["vector_rank"] = rank
    return scores


@managed_memory_connection
def search_memories(query: str, *, viewer_scope: str = "leadership", include_system_internal: bool = False,
                    filters: Optional[dict] = None, limit: int = 10,
                    embed_query: Optional[Callable[[str], Optional[list[float]]]] = None,
                    conn=None) -> list[MemorySearchResult]:
    candidates = list_memories(
        viewer_scope=viewer_scope,
        include_system_internal=include_system_internal,
        filters=filters,
        limit=max(200, limit * 10),
        conn=conn,
    )
    if not candidates:
        return []
    ids = [m["memory_id"] for m in candidates]

    lexical_ranks: list[int] = []
    q = (query or "").strip()
    if q:
        try:
            placeholders = ",".join("?" for _ in ids)
            fts_rows = conn.execute(
                "SELECT rowid AS memory_id, bm25(clan_memories_fts) AS score "
                "FROM clan_memories_fts WHERE clan_memories_fts MATCH ? AND rowid IN ({}) ORDER BY score ASC LIMIT ?".format(placeholders),
                (q, *ids, limit * 4),
            ).fetchall()
            lexical_ranks = [r["memory_id"] for r in fts_rows]
        except sqlite3.OperationalError:
            # FTS fallback (degraded mode)
            ranked = []
            low = q.lower()
            for item in candidates:
                haystack = f"{item.get('title','')}\n{item.get('summary','')}\n{item.get('body','')}".lower()
                if low in haystack:
                    ranked.append((haystack.count(low), item["memory_id"]))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            lexical_ranks = [x[1] for x in ranked[: limit * 4]]
    else:
        lexical_ranks = ids[: limit * 4]

    vector_ranks: list[int] = []
    query_embedding = embed_query(q) if (q and embed_query) else None
    if query_embedding:
        emb_rows = conn.execute(
            "SELECT memory_id, vector_json FROM clan_memory_embeddings WHERE memory_id IN ({})".format(",".join("?" for _ in ids)),
            ids,
        ).fetchall()
        scored = []
        for row in emb_rows:
            score = _cosine(query_embedding, json.loads(row["vector_json"]))
            scored.append((score, row["memory_id"]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        vector_ranks = [memory_id for _, memory_id in scored[: limit * 4]]

    fused = _rrf_merge(lexical_ranks, vector_ranks)
    now = datetime.now(timezone.utc)
    by_id = {m["memory_id"]: m for m in candidates}
    results = []
    for memory_id, parts in fused.items():
        memory = by_id.get(memory_id)
        if not memory:
            continue
        score = parts["rrf"]
        created = _parse_utc_datetime(memory["created_at"])
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        recency_boost = 1.0 + max(0.0, (30 - age_days) / 300.0)
        confidence_penalty = 1.0 if not memory["is_inference"] else max(0.4, float(memory["confidence"]))
        score = score * recency_boost * confidence_penalty
        results.append(MemorySearchResult(memory=memory, rank_score=score, components=parts))
    results.sort(key=lambda r: (-r.rank_score, r.memory["created_at"], r.memory["memory_id"]))
    return results[:limit]


__all__ = [
    "MemoryValidationError",
    "MemorySearchResult",
    "create_memory",
    "update_memory",
    "archive_memory",
    "soft_delete_memory",
    "attach_tags",
    "attach_evidence_ref",
    "get_memory",
    "list_memories",
    "upsert_embedding",
    "search_memories",
]
