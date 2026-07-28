"""memory_store — the v5.1 memory system (docs/reference/v5.1/memory.md).

Rebuilt 2026-07-04 (D1–D5 ratified): memories live in the ENGINE DB
(`elixir-v51.db`) — `memories` + `memory_tags` + `memory_log` + `memories_fts`
replace the old two-database, twenty-object sprawl. Content was migrated
(scripts/migrate_v51/memory_migrate.py); structure was not.

Compatibility surface: the public functions keep their pre-rebuild names and
signatures. Old source_type values (`elixir_inference`, `elixir_synthesis`)
are accepted and mapped to v5.1 kinds; returned rows carry BOTH `kind` (new)
and `source_type`/`is_inference`/`status` (legacy aliases) so existing callers
keep working. Scope `system_internal` maps to `leadership` (the old data had
zero such rows; the old code used it as a viewer superset).

Retrieval is deterministic ranked selection (§2.3): match strength +
confidence + recency decay — constants below. FTS5 serves query search; there
are deliberately NO embeddings in v1 (D2).
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger("memory_store")

# ---------------------------------------------------------------- constants

# Ranked-selection weights (memory.md §2.3). Tunable, pure code.
W_MATCH = 0.5  # subject match strength (member 1.0, channel 0.6, fts 0.8)
W_CONF = 0.3  # stored confidence 0..1
W_RECENCY = 0.2  # decay(updated_at)
RECENCY_HALF_LIFE_DAYS = 45.0
# Machine observations decay; curated knowledge does not.
#
# `inference` rows are Elixir noticing something ("TDuck maxed Princess card",
# "gooba unlocked Tornado spell") — true when written, trivia three months on.
# They are 97% of the corpus and, because the FTS branch scopes by neither
# channel nor age, they dominate text recall: a query for "signature deck"
# returned six rows, ALL from channels deleted in July, dated April-May, one of
# them about a member who has since left the clan.
#
# So inference gets a default lifetime and ages out of recall on its own.
# leader_note / synthesis / system are curated or structural and never expire by
# default — an LOA, a founding date, or an agreed exception must not evaporate.
# An explicit expires_at from the caller always wins.
INFERENCE_TTL_DAYS = 90
_SELF_EXPIRING_KINDS = frozenset({"inference"})
RETIRED_PURGE_GRACE_DAYS = 30

KINDS = {"leader_note", "inference", "system", "synthesis", "conversation_digest"}
# Old → new kind mapping (accepted on write and in filters).
_KIND_MAP = {
    "leader_note": "leader_note",
    "elixir_inference": "inference",
    "inference": "inference",
    "elixir_synthesis": "synthesis",
    "synthesis": "synthesis",
    "system": "system",
    "conversation_digest": "conversation_digest",
}
# New → legacy alias for returned rows (compat).
_LEGACY_SOURCE = {
    "leader_note": "leader_note",
    "inference": "elixir_inference",
    "synthesis": "elixir_synthesis",
    "system": "system",
    "conversation_digest": "system",  # closest legacy bucket
}

SOURCE_TYPES = set(_KIND_MAP)  # legacy export
SCOPES = {"public", "leadership", "system_internal"}
STATUSES = {"active", "archived", "deleted"}


def _utcnow() -> str:
    # Engine Z-convention (cold review #8): the table briefly held three
    # timestamp formats on day one; scripts/migrate_v51/fix_memories_ts.py
    # normalized the migrated rows to match.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon_tag(tag: Optional[str]) -> str:
    from db import _canon_tag as canon

    return canon(tag)


def _json_or_none(data) -> Optional[str]:
    if data is None:
        return None
    return json.dumps(data, default=str, ensure_ascii=False)


# ---------------------------------------------------------------- connection


def get_memory_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """v5.1: memories live in the ENGINE DB — this returns a normal
    operational connection (kept for caller compatibility; the separate
    memory DB is retired). `db_path` override honored for tests."""
    import db as _db

    conn = _db.get_connection(db_path) if db_path else _db.get_connection()
    ensure_memory_schema(conn)
    return conn


def ensure_memory_schema(conn: sqlite3.Connection) -> None:
    """Compatibility assertion; db.schema owns memory-system creation."""
    from db.schema import require_columns

    require_columns(conn, "memories", {"memory_id", "kind", "scope"})
    require_columns(conn, "memory_tags", {"memory_id", "tag"})
    require_columns(conn, "memory_log", {"log_id", "memory_id"})
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'memories_fts'").fetchone():
        raise RuntimeError(
            "database schema contract missing memories_fts; open through db.get_connection()"
        )


def managed_memory_connection(fn: Callable) -> Callable:
    """Mirrors db.managed_connection: when it owns the connection (conn was None)
    it commits on success / rolls back on error / closes; a borrowed connection
    is passed through untouched and never committed here. Decorated writers must
    NOT call conn.commit() themselves — a mid-tick commit on the engine's
    connection (chronicles writes memories during EMIT/RECOGNIZE) would defeat
    the tick's per-step rollback guard."""

    @functools.wraps(fn)
    def wrapper(*args, conn=None, **kwargs):
        owns = conn is None
        conn = conn or get_memory_connection()
        if not owns:
            ensure_memory_schema(conn)
        try:
            result = fn(*args, conn=conn, **kwargs)
            if owns:
                conn.commit()
            return result
        except Exception:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                conn.close()

    return wrapper


# ---------------------------------------------------------------- validation


class MemoryValidationError(ValueError):
    pass


@dataclass
class MemorySearchResult:
    memory: dict
    rank_score: float
    components: dict


def _norm_kind(source_type: str) -> str:
    kind = _KIND_MAP.get((source_type or "").strip())
    if not kind:
        raise MemoryValidationError(f"invalid source_type/kind: {source_type}")
    return kind


def _norm_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise MemoryValidationError(f"invalid scope: {scope}")
    return "leadership" if scope == "system_internal" else scope


def _allowed_scopes(viewer_scope: str, include_system_internal: bool = False) -> tuple[str, ...]:
    if viewer_scope == "public":
        return ("public",)
    if viewer_scope in ("leadership", "system_internal"):
        return ("public", "leadership")
    raise MemoryValidationError(f"invalid viewer scope: {viewer_scope}")


def _validate_provenance(source_type: str, is_inference: bool, confidence: float) -> None:
    kind = _norm_kind(source_type)
    if not (0.0 <= float(confidence) <= 1.0):
        raise MemoryValidationError("confidence must be between 0.0 and 1.0")
    if kind == "leader_note" and is_inference:
        raise MemoryValidationError("leader_note memories cannot be marked as inference")
    if kind == "inference" and float(confidence) >= 1.0:
        raise MemoryValidationError("elixir_inference confidence must be less than 1.0")


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


# ---------------------------------------------------------------- row shape


def _event_key(event_type: Optional[str], event_id: Optional[str]) -> Optional[str]:
    if event_type and event_id:
        return f"{event_type}:{event_id}"
    return event_type or event_id or None


def _fetch_memory(conn, memory_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    # Legacy aliases so pre-rebuild callers keep working.
    item["source_type"] = _LEGACY_SOURCE.get(item["kind"], item["kind"])
    item["is_inference"] = 1 if item["kind"] == "inference" else 0
    item["status"] = "archived" if item.get("retired_at") else "active"
    item["metadata_json"] = {}
    ek = item.get("source_event_key") or ""
    if ":" in ek:
        item["event_type"], item["event_id"] = ek.split(":", 1)
    else:
        item["event_type"], item["event_id"] = (ek or None), None
    item["channel_id"] = item.get("channel_key")
    item["tags"] = [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (memory_id,)
        ).fetchall()
    ]
    item["evidence_refs"] = []
    return item


def _log(conn, memory_id: int, action: str, actor: str, diff: Optional[dict] = None) -> None:
    conn.execute(
        "INSERT INTO memory_log (memory_id, action, actor, at, diff_json) VALUES (?, ?, ?, ?, ?)",
        (memory_id, action, actor, _utcnow(), _json_or_none(diff)),
    )


# ---------------------------------------------------------------- writers


@managed_memory_connection
def create_memory(
    *,
    body: str,
    source_type: str,
    is_inference: bool,
    confidence: float,
    created_by: str,
    scope: str = "leadership",
    status: str = "active",
    title: Optional[str] = None,
    summary: Optional[str] = None,
    member_id: Optional[int] = None,
    member_tag: Optional[str] = None,
    role: Optional[str] = None,
    channel_id: Optional[str] = None,
    war_season_id: Optional[str] = None,
    war_week_id: Optional[str] = None,
    event_type: Optional[str] = None,
    event_id: Optional[str] = None,
    retention_class: str = "standard",
    expires_at: Optional[str] = None,
    metadata: Optional[dict] = None,
    idempotent: bool = False,
    conn=None,
) -> dict:
    """Signature preserved from the old store; member_id / role /
    retention_class / war_* are accepted-and-absorbed (war ids fold into the
    event key when no event pair is given; the rest are legacy no-ops)."""
    del member_id, role, retention_class  # legacy params, no v5.1 columns
    if status not in STATUSES:
        raise MemoryValidationError(f"invalid status: {status}")
    _validate_provenance(source_type, is_inference, confidence)
    kind = _norm_kind(source_type)
    now = _utcnow()
    ek = _event_key(event_type, event_id) or _event_key(war_season_id, war_week_id)
    # QA M28: opt-in idempotency — when a stable event key is supplied and the
    # caller asks for it, return the existing active memory instead of inserting
    # a duplicate (the awareness save path uses this; other callers unaffected).
    if idempotent and ek:
        existing = conn.execute(
            "SELECT memory_id FROM memories WHERE source_event_key = ? AND retired_at IS NULL "
            "ORDER BY memory_id DESC LIMIT 1",
            (ek,),
        ).fetchone()
        if existing:
            return _fetch_memory(conn, existing["memory_id"])
    if expires_at is None and kind in _SELF_EXPIRING_KINDS:
        expires_at = _iso_plus_days(now, INFERENCE_TTL_DAYS)
    cur = conn.execute(
        """INSERT INTO memories (kind, title, body, summary, scope, confidence,
               member_tag, channel_key, source_event_key, created_by,
               created_at, updated_at, expires_at, retired_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kind,
            title or (body[:80] if body else "(untitled)"),
            body,
            summary,
            _norm_scope(scope),
            float(confidence),
            _canon_tag(member_tag) if member_tag else None,
            str(channel_id) if channel_id else None,
            ek,
            created_by,
            now,
            now,
            _normalize_date(expires_at),
            now if status in ("archived", "deleted") else None,
        ),
    )
    memory_id = cur.lastrowid
    if metadata:
        _log(conn, memory_id, "created", created_by, {"metadata": metadata, "kind": kind})
    else:
        _log(conn, memory_id, "created", created_by, {"kind": kind})
    # No conn.commit(): managed_memory_connection commits when it owns the conn;
    # on a borrowed (engine tick) conn a mid-step commit breaks step atomicity.
    return _fetch_memory(conn, memory_id)


@managed_memory_connection
def attach_tags(memory_id: int, tags: Iterable[str], *, actor: str, conn=None) -> list[str]:
    clean = sorted({t.strip().lower() for t in (tags or []) if t and t.strip()})
    for tag in clean:
        conn.execute(
            "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
            (memory_id, tag),
        )
    if clean:
        _log(conn, memory_id, "edited", actor, {"tags": clean})
    return clean  # commit owned by managed_memory_connection (never on borrowed conn)


@managed_memory_connection
def attach_evidence_ref(
    memory_id: int,
    *,
    evidence_type: str,
    evidence_ref: str,
    actor: str,
    evidence_label: Optional[str] = None,
    evidence_url: Optional[str] = None,
    metadata: Optional[dict] = None,
    conn=None,
) -> None:
    """v5.1: the evidence-ref satellite is gone (0 rows ever) — the reference
    lands in memory_log so provenance is still recorded."""
    _log(
        conn,
        memory_id,
        "edited",
        actor,
        {
            "evidence": {
                "type": evidence_type,
                "ref": evidence_ref,
                "label": evidence_label,
                "url": evidence_url,
                "metadata": metadata or {},
            },
        },
    )
    # commit owned by managed_memory_connection (never on borrowed conn)


_UPDATABLE = {
    "title",
    "body",
    "summary",
    "scope",
    "confidence",
    "member_tag",
    "channel_id",
    "event_type",
    "event_id",
    "expires_at",
}


@managed_memory_connection
def update_memory(memory_id: int, *, actor: str, conn=None, **updates) -> dict:
    current = _fetch_memory(conn, memory_id)
    if not current:
        raise MemoryValidationError(f"memory not found: {memory_id}")
    cols, args, diff = [], [], {}
    status = updates.pop("status", None)
    for key, value in updates.items():
        if key not in _UPDATABLE or value is None:
            continue
        if key == "member_tag":
            value = _canon_tag(value)
        if key == "channel_id":
            cols.append("channel_key = ?")
            args.append(str(value))
            diff[key] = value
            continue
        if key in ("event_type", "event_id"):
            continue  # handled below as a pair
        if key == "expires_at":
            value = _normalize_date(value)
        if key == "scope":
            value = _norm_scope(value)
        cols.append(f"{key} = ?")
        args.append(value)
        diff[key] = value
    ek = _event_key(updates.get("event_type"), updates.get("event_id"))
    if ek:
        cols.append("source_event_key = ?")
        args.append(ek)
        diff["source_event_key"] = ek
    if status in ("archived", "deleted"):
        cols.append("retired_at = ?")
        args.append(_utcnow())
        diff["status"] = status
    elif status == "active":
        cols.append("retired_at = NULL")
        diff["status"] = status
    if not cols:
        return current
    cols.append("updated_at = ?")
    args.extend([_utcnow(), memory_id])
    conn.execute(f"UPDATE memories SET {', '.join(cols)} WHERE memory_id = ?", args)
    _log(
        conn,
        memory_id,
        "retired" if status in ("archived", "deleted") else "edited",
        actor,
        diff,
    )
    # commit owned by managed_memory_connection (never on borrowed conn)
    return _fetch_memory(conn, memory_id)


def archive_memory(
    memory_id: int, *, actor: str, conn: Optional[sqlite3.Connection] = None
) -> dict:
    return update_memory(memory_id, actor=actor, status="archived", conn=conn)


@managed_memory_connection
def purge_expired_memories(*, conn=None) -> int:
    """Real expiry (memory.md §2.4): hard-delete rows past expires_at, and
    retired rows past the grace window. Called from db-maintenance."""
    now = _utcnow()
    cur = conn.execute(
        """DELETE FROM memories WHERE
             (expires_at IS NOT NULL AND expires_at <= ?)
             OR (retired_at IS NOT NULL
                 AND retired_at <= strftime('%Y-%m-%dT%H:%M:%SZ', ?, ?))""",
        (now, now, f"-{RETIRED_PURGE_GRACE_DAYS} days"),
    )
    # commit owned by managed_memory_connection (never on borrowed conn)
    return cur.rowcount


# ---------------------------------------------------------------- readers


def _filter_where(filters: Optional[dict], args: list) -> str:
    filters = filters or {}
    clauses = []
    if filters.get("member_tag"):
        clauses.append("m.member_tag = ?")
        args.append(_canon_tag(filters["member_tag"]))
    if filters.get("scope"):
        clauses.append("m.scope = ?")
        args.append(_norm_scope(filters["scope"]))
    if filters.get("source_type"):
        clauses.append("m.kind = ?")
        args.append(_norm_kind(filters["source_type"]))
    if filters.get("kind"):
        clauses.append("m.kind = ?")
        args.append(_norm_kind(filters["kind"]))
    ek = _event_key(filters.get("event_type"), filters.get("event_id"))
    if ek:
        if filters.get("event_id"):
            clauses.append("m.source_event_key = ?")
            args.append(ek)
        else:
            clauses.append("(m.source_event_key = ? OR m.source_event_key LIKE ?)")
            args.extend([ek, f"{ek}:%"])
    # War-scoped filters (lane contexts pass war_week_id "SEASON:WEEK" /
    # war_season_id "SEASON"). The rebuild folded these into source_event_key
    # (e.g. "war_battle_day_recap:130:2:4"); the read side silently ignored
    # both filters until 2026-07-04 — every lane got the same unscoped rows.
    if filters.get("war_week_id"):
        wk = str(filters["war_week_id"])
        clauses.append(
            "(m.source_event_key = ? OR m.source_event_key LIKE ? OR m.source_event_key LIKE ?)"
        )
        args.extend([wk, f"%:{wk}", f"%:{wk}:%"])
    if filters.get("war_season_id"):
        season = str(filters["war_season_id"])
        clauses.append(
            "(m.source_event_key = ? OR m.source_event_key LIKE ? OR m.source_event_key LIKE ?)"
        )
        args.extend([season, f"{season}:%", f"%:{season}:%"])
    if filters.get("channel_id"):
        clauses.append("m.channel_key = ?")
        args.append(str(filters["channel_id"]))
    created_after = _normalize_date(filters.get("created_after"))
    created_before = _normalize_date(filters.get("created_before"))
    if created_after:
        clauses.append("m.created_at >= ?")
        args.append(created_after)
    if created_before:
        clauses.append("m.created_at <= ?")
        args.append(created_before)
    return (" AND " + " AND ".join(clauses)) if clauses else ""


@managed_memory_connection
def get_memory(
    memory_id: int,
    *,
    viewer_scope: str = "leadership",
    include_system_internal: bool = False,
    include_archived: bool = False,
    include_deleted: bool = False,
    conn=None,
) -> Optional[dict]:
    scopes = _allowed_scopes(viewer_scope, include_system_internal)
    row = _fetch_memory(conn, memory_id)
    if not row:
        return None
    if row["scope"] not in scopes:
        return None
    if row.get("retired_at") and not (include_archived or include_deleted):
        return None
    if row.get("expires_at") and row["expires_at"] <= _utcnow():
        return None
    return row


@managed_memory_connection
def list_memories(
    *,
    viewer_scope: str = "leadership",
    include_system_internal: bool = False,
    include_archived: bool = False,
    include_deleted: bool = False,
    filters: Optional[dict] = None,
    limit: int = 50,
    conn=None,
) -> list[dict]:
    scopes = _allowed_scopes(viewer_scope, include_system_internal)
    args: list = list(scopes)
    sql = "SELECT m.memory_id FROM memories m WHERE m.scope IN ({})".format(
        ",".join("?" for _ in scopes)
    )
    if not (include_archived or include_deleted):
        sql += " AND m.retired_at IS NULL"
    sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
    args.append(_utcnow())
    sql += _filter_where(filters, args)
    sql += " ORDER BY m.updated_at DESC, m.memory_id DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()
    return [_fetch_memory(conn, r["memory_id"]) for r in rows]


def _iso_plus_days(iso_now: str, days: int) -> str:
    """`iso_now` + N days, in the store's canonical stamp format."""
    from datetime import datetime, timedelta, timezone

    base = datetime.strptime(iso_now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recency(updated_at: str, now: datetime) -> float:
    try:
        age_days = max(0.0, (now - _parse_utc_datetime(updated_at)).total_seconds() / 86400.0)
    except TypeError, ValueError:
        return 0.0
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


@managed_memory_connection
def select_memories(
    *,
    member_tag: Optional[str] = None,
    channel_key: Optional[str] = None,
    query: Optional[str] = None,
    tags: Optional[list] = None,
    viewer_scope: str = "public",
    limit: int = 5,
    conn=None,
) -> list[dict]:
    """Ranked selection for answer-time context (memory.md §2.3):
    score = W_MATCH·match + W_CONF·confidence + W_RECENCY·decay(updated_at).
    Deterministic; no embeddings.

    tags: candidates must carry ALL given tags (the Editor's rubric retrieval,
    editor.md §3 — tag-scoped pools like 'editorial' rank within themselves)."""
    scopes = _allowed_scopes(viewer_scope)
    now = datetime.now(timezone.utc)
    match: dict[int, float] = {}
    tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]

    def scope_sql(alias="m"):
        return (
            f"{alias}.scope IN ({','.join('?' for _ in scopes)}) AND {alias}.retired_at IS NULL "
            f"AND ({alias}.expires_at IS NULL OR {alias}.expires_at > ?)"
        )

    base_args = [*scopes, _utcnow()]
    if member_tag:
        for r in conn.execute(
            f"SELECT memory_id FROM memories m WHERE {scope_sql()} AND m.member_tag = ? "
            "ORDER BY m.updated_at DESC LIMIT 40",
            (*base_args, _canon_tag(member_tag)),
        ).fetchall():
            match[r["memory_id"]] = max(match.get(r["memory_id"], 0.0), 1.0)
    if channel_key:
        for r in conn.execute(
            f"SELECT memory_id FROM memories m WHERE {scope_sql()} AND m.channel_key = ? "
            "ORDER BY m.updated_at DESC LIMIT 40",
            (*base_args, str(channel_key)),
        ).fetchall():
            match[r["memory_id"]] = max(match.get(r["memory_id"], 0.0), 0.6)
    if query and query.strip():
        try:
            for rank, r in enumerate(
                conn.execute(
                    "SELECT rowid AS memory_id FROM memories_fts WHERE memories_fts MATCH ? "
                    "ORDER BY bm25(memories_fts) LIMIT 40",
                    (_fts_sanitize(query),),
                ).fetchall(),
                start=1,
            ):
                strength = 0.8 * (1.0 / (1 + 0.05 * (rank - 1)))
                match[r["memory_id"]] = max(match.get(r["memory_id"], 0.0), strength)
        except sqlite3.OperationalError:
            pass
    if tags:
        # Tag pool is its own candidate source: a tag-scoped call must see the
        # whole pool even when the FTS query matches nothing (base strength is
        # low — confidence + recency rank within the pool).
        for r in conn.execute(
            f"SELECT m.memory_id FROM memories m "
            f"JOIN memory_tags mt ON mt.memory_id = m.memory_id "
            f"WHERE {scope_sql()} AND mt.tag = ? "
            "ORDER BY m.updated_at DESC LIMIT 40",
            (*base_args, tags[0]),
        ).fetchall():
            match[r["memory_id"]] = max(match.get(r["memory_id"], 0.0), 0.4)
    # Recency backstop ONLY for unfiltered calls (no member/channel/query):
    # with subject filters given, candidates come from matches alone — the
    # old unconditional backstop let a fresh high-conf memory about member B
    # outscore member A's sparse matches and inject wrong-subject facts into
    # A's answer context (cold review 2026-07-04 #5).
    if not (member_tag or channel_key or (query and query.strip()) or tags):
        for r in conn.execute(
            f"SELECT memory_id FROM memories m WHERE {scope_sql()} "
            "ORDER BY m.updated_at DESC LIMIT 20",
            base_args,
        ).fetchall():
            match.setdefault(r["memory_id"], 0.0)

    scored = []
    now_iso = _utcnow()
    for memory_id, strength in match.items():
        row = _fetch_memory(conn, memory_id)
        if not row or row["scope"] not in scopes:
            continue
        # Retirement/expiry must be re-checked HERE, not only in the candidate
        # queries. The FTS branch above selects straight out of memories_fts and
        # carries no scope_sql() predicate, so before this guard an archived or
        # expired memory that matched the query was scored and injected into the
        # prompt — while the member/channel/tag branches correctly excluded it.
        #
        # That silently defeated soft expiry: runtime/jobs/_memory.py sets
        # expires_at on stale or contradicted synthesis rows precisely so they
        # "vanish from readers", and 4 of the 6 conversational lanes retrieve via
        # FTS. A fact Elixir had decided was wrong could still be recalled.
        #
        # This is the single choke point every candidate source passes through,
        # so guarding it also covers any source added later.
        if row.get("retired_at"):
            continue
        if row.get("expires_at") and str(row["expires_at"]) <= now_iso:
            continue
        if tags and not set(tags) <= set(row.get("tags") or []):
            continue
        score = (
            W_MATCH * strength
            + W_CONF * float(row.get("confidence") or 0.0)
            + W_RECENCY * _recency(row.get("updated_at") or row.get("created_at"), now)
        )
        scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], x[1]["memory_id"]))
    out = []
    for score, row in scored[: max(1, int(limit))]:
        row["rank_score"] = round(score, 4)
        out.append(row)
    return out


def _fts_sanitize(query: str) -> str:
    # Bare-word AND query; strips FTS operators so user text can't error the MATCH.
    words = [w for w in "".join(c if c.isalnum() else " " for c in query).split() if w]
    return " ".join(words[:12]) or '""'


@managed_memory_connection
def search_memories(
    query: str,
    *,
    viewer_scope: str = "leadership",
    include_system_internal: bool = False,
    filters: Optional[dict] = None,
    limit: int = 10,
    embed_query: Optional[Callable] = None,
    conn=None,
) -> list[MemorySearchResult]:
    """FTS-ranked search (embed_query accepted-and-ignored — D2)."""
    del embed_query
    scopes = _allowed_scopes(viewer_scope, include_system_internal)
    args: list = list(scopes)
    where = "m.scope IN ({}) AND m.retired_at IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)".format(
        ",".join("?" for _ in scopes)
    )
    args.append(_utcnow())
    where += _filter_where(filters, args)
    q = (query or "").strip()
    now = datetime.now(timezone.utc)
    results: list[MemorySearchResult] = []
    if q:
        try:
            rows = conn.execute(
                f"""SELECT m.memory_id, bm25(memories_fts) AS fts_score
                    FROM memories_fts JOIN memories m ON m.memory_id = memories_fts.rowid
                    WHERE memories_fts MATCH ? AND {where}
                    ORDER BY fts_score LIMIT ?""",
                (_fts_sanitize(q), *args, limit * 4),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:  # degraded substring fallback
            low = q.lower()
            rows = [
                {"memory_id": r["memory_id"], "fts_score": 0.0}
                for r in conn.execute(
                    f"SELECT m.memory_id, m.title, m.summary, m.body FROM memories m WHERE {where} "
                    "ORDER BY m.updated_at DESC LIMIT 400",
                    args,
                ).fetchall()
                if low in f"{r['title'] or ''}\n{r['summary'] or ''}\n{r['body'] or ''}".lower()
            ][: limit * 4]
    else:
        rows = conn.execute(
            f"SELECT m.memory_id, 0.0 AS fts_score FROM memories m WHERE {where} "
            "ORDER BY m.updated_at DESC LIMIT ?",
            (*args, limit * 4),
        ).fetchall()
    for rank, r in enumerate(rows, start=1):
        memory = _fetch_memory(conn, r["memory_id"])
        if not memory:
            continue
        strength = 1.0 / (1 + 0.1 * (rank - 1))
        score = (
            W_MATCH * strength
            + W_CONF * float(memory.get("confidence") or 0.0)
            + W_RECENCY * _recency(memory.get("updated_at") or memory.get("created_at"), now)
        )
        results.append(
            MemorySearchResult(
                memory=memory,
                rank_score=score,
                components={"fts_rank": rank, "match": strength},
            )
        )
    results.sort(key=lambda x: (-x.rank_score, x.memory["memory_id"]))
    return results[: max(1, int(limit))]


__all__ = [
    "MemoryValidationError",
    "MemorySearchResult",
    "create_memory",
    "update_memory",
    "archive_memory",
    "attach_tags",
    "attach_evidence_ref",
    "get_memory",
    "list_memories",
    "select_memories",
    "search_memories",
    "purge_expired_memories",
    "get_memory_connection",
    "managed_memory_connection",
    "ensure_memory_schema",
]
