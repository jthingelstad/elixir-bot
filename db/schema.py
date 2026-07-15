"""Explicit forward schema evolution for the v5.1 operational database.

The clean-break v5.1 baseline is migration 0. Every compatible database moves
forward here, once, before a runtime module can read or write it. Domain modules
may validate the contract, but they never issue CREATE/ALTER statements.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

CURRENT_SCHEMA_VERSION = 2


_V1_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS awareness_thoughts (
        thought_id TEXT PRIMARY KEY,
        loop_number INTEGER,
        at TEXT NOT NULL,
        read_json TEXT,
        plan_json TEXT,
        tool_trace_json TEXT,
        chose_silence INTEGER NOT NULL DEFAULT 0,
        post_count INTEGER NOT NULL DEFAULT 0,
        skipped_reason TEXT,
        model TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_awareness_thoughts_at ON awareness_thoughts(at DESC)",
    """CREATE TABLE IF NOT EXISTS watches (
        watch_id TEXT PRIMARY KEY,
        opened_at TEXT NOT NULL,
        subject_tag TEXT,
        subject_label TEXT,
        reason TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        expires_at TEXT,
        last_seen_at TEXT,
        resolved_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status, opened_at DESC)",
    """CREATE TABLE IF NOT EXISTS awareness_posts (
        post_id INTEGER PRIMARY KEY,
        lane TEXT NOT NULL,
        content_preview TEXT NOT NULL,
        covers_json TEXT NOT NULL DEFAULT '[]',
        loop_number INTEGER,
        posted_at TEXT NOT NULL,
        discord_message_id TEXT UNIQUE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_awareness_posts_lane ON awareness_posts(lane, posted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_awareness_posts_at ON awareness_posts(posted_at DESC)",
    """CREATE TABLE IF NOT EXISTS runtime_incidents (
        incident_id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        component TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('warn','error')),
        summary TEXT NOT NULL,
        detail TEXT,
        context_json TEXT,
        resolved_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_incidents_open ON runtime_incidents(resolved_at, at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_component ON runtime_incidents(component, at DESC)",
    """CREATE TABLE IF NOT EXISTS game_events (
        event_id INTEGER PRIMARY KEY,
        dedup_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        change_key TEXT NOT NULL,
        subject_tag TEXT,
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
        backfilled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_game_events_change ON game_events(change_key, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_game_events_type ON game_events(event_type, observed_at DESC)",
    """CREATE TABLE IF NOT EXISTS evergreen_nudges (
        nudge_key TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        context TEXT NOT NULL,
        forbidden_terms_json TEXT,
        cooldown_days INTEGER NOT NULL DEFAULT 30,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_sent_at TEXT,
        send_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS email_verifications (
        player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE,
        pending_email TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tick_history (
        tick_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        counters_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pol_seasons (
        pol_season_id TEXT PRIMARY KEY,
        started_at TEXT,
        ended_at TEXT,
        closed INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS pol_season_results (
        pol_season_id TEXT NOT NULL REFERENCES pol_seasons(pol_season_id),
        player_tag TEXT NOT NULL,
        league INTEGER,
        rating INTEGER,
        global_rank INTEGER,
        battles INTEGER,
        wins INTEGER,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (pol_season_id, player_tag)
    )""",
    """CREATE TABLE IF NOT EXISTS memories (
        memory_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN
            ('leader_note','inference','system','synthesis','conversation_digest')),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        summary TEXT,
        scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public','leadership')),
        confidence REAL NOT NULL DEFAULT 0.9,
        member_tag TEXT,
        channel_key TEXT,
        source_event_key TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        retired_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memories_member ON memories(member_tag, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_event ON memories(source_event_key)",
    """CREATE TABLE IF NOT EXISTS memory_tags (
        memory_id INTEGER NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY (memory_id, tag)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag)",
    """CREATE TABLE IF NOT EXISTS memory_log (
        log_id INTEGER PRIMARY KEY,
        memory_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL,
        diff_json TEXT
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        title, summary, body,
        content='memories',
        content_rowid='memory_id'
    )""",
    """CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, title, summary, body)
        VALUES (new.memory_id, new.title, new.summary, new.body);
    END""",
    """CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, summary, body)
        VALUES('delete', old.memory_id, old.title, old.summary, old.body);
    END""",
    """CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, summary, body)
        VALUES('delete', old.memory_id, old.title, old.summary, old.body);
        INSERT INTO memories_fts(rowid, title, summary, body)
        VALUES (new.memory_id, new.title, new.summary, new.body);
    END""",
)

_V1_COLUMNS = {
    "players": (("display_name", "TEXT"),),
    "player_metadata": (
        ("email", "TEXT DEFAULT ''"),
        ("email_verified_at", "TEXT"),
        ("email_source", "TEXT DEFAULT ''"),
        ("cr_years_celebrated", "INTEGER"),
    ),
    "player_current_state": (("last_seen_api", "TEXT"),),
    "war_weeks": (("defense_fame", "INTEGER"),),
    "card_catalog": (("first_seen_at", "TEXT"),),
    "api_sentinel_observations": (("first_entity_key", "TEXT"),),
    "llm_calls": (("prompt_json", "TEXT"), ("response_json", "TEXT")),
    "awareness_thoughts": (
        ("loop_number", "INTEGER"),
        ("tool_trace_json", "TEXT"),
    ),
}

REQUIRED_SCHEMA = {
    "awareness_thoughts": {"thought_id", "loop_number", "tool_trace_json"},
    "awareness_posts": {"post_id", "lane", "content_preview", "posted_at"},
    "watches": {"watch_id", "status"},
    "players": {"player_tag", "display_name"},
    "player_metadata": {
        "player_tag",
        "email",
        "email_verified_at",
        "email_source",
        "cr_years_celebrated",
    },
    "player_current_state": {"player_tag", "last_seen_api"},
    "war_weeks": {"season_id", "section_index", "defense_fame"},
    "card_catalog": {"card_id", "first_seen_at"},
    "api_sentinel_observations": {"observation_id", "first_entity_key"},
    "llm_calls": {"call_id", "prompt_json", "response_json"},
    "runtime_incidents": {"incident_id", "component", "resolved_at"},
    "game_events": {"event_id", "dedup_key", "change_key"},
    "evergreen_nudges": {"nudge_key", "last_sent_at"},
    "email_verifications": {"player_tag", "code_hash", "expires_at"},
    "tick_history": {"tick_id", "counters_json"},
    "pol_seasons": {"pol_season_id", "closed"},
    "pol_season_results": {"pol_season_id", "player_tag"},
    "memories": {"memory_id", "kind", "scope"},
    "memory_tags": {"memory_id", "tag"},
    "memory_log": {"log_id", "memory_id"},
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_v1_columns(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    for table, additions in _V1_COLUMNS.items():
        if table not in tables:
            continue
        columns = _columns(conn, table)
        for column, declaration in additions:
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                columns.add(column)


def _backfill_v1(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    if "card_catalog" in tables and {"first_seen_at", "synced_at"}.issubset(
        _columns(conn, "card_catalog")
    ):
        conn.execute(
            "UPDATE card_catalog SET first_seen_at = synced_at WHERE first_seen_at IS NULL"
        )
    if "api_sentinel_observations" in tables:
        conn.execute(
            "UPDATE api_sentinel_observations SET first_entity_key = entity_key "
            "WHERE first_entity_key IS NULL"
        )
    if "awareness_thoughts" in tables:
        conn.execute(
            "UPDATE awareness_thoughts SET loop_number = rowid WHERE loop_number IS NULL"
        )
    if {"memories", "memories_fts"}.issubset(tables):
        # Creating an external-content FTS table does not index rows that
        # predate the table. Rebuild is idempotent and guarantees migration
        # from a partially lazy-created v5.1 schema cannot strand memories
        # outside search.
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")

    if "communication_intents" not in tables or "awareness_posts" not in tables:
        return
    rows = conn.execute(
        "SELECT intent_id, lane, payload_json, created_at, fulfilled_at, "
        "discord_message_id FROM communication_intents "
        "WHERE intent_type = 'awareness:post' AND status = 'fulfilled'"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row[2] or "{}")
        except (TypeError, ValueError):
            payload = {}
        conn.execute(
            "INSERT OR IGNORE INTO awareness_posts "
            "(lane, content_preview, covers_json, loop_number, posted_at, "
            "discord_message_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row[1],
                str(payload.get("content") or "")[:800],
                json.dumps(payload.get("covers_signal_keys") or []),
                payload.get("loop_number"),
                row[4] or row[3],
                row[5],
            ),
        )


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Remove retired proactive-delivery storage from the live schema.

    The deterministic recognizer remains available to explicit offline
    rehearsals, which create a connection-local TEMP queue. Production keeps
    only the durable recognition ledger (including intentless award claims)
    and the awareness-native thought/post history.
    """
    tables = _tables(conn)
    conn.execute("DROP TABLE IF EXISTS editor_verdicts")
    conn.execute("DROP TABLE IF EXISTS communication_intents")

    if "awareness_thoughts" in tables and "shadow" in _columns(
        conn, "awareness_thoughts"
    ):
        conn.execute("ALTER TABLE awareness_thoughts DROP COLUMN shadow")

    if "awareness_posts" in tables and "legacy_intent_id" in _columns(
        conn, "awareness_posts"
    ):
        # SQLite cannot DROP a column carrying a UNIQUE constraint. Rebuild
        # this small history table while preserving its stable post ids.
        conn.execute("DROP INDEX IF EXISTS idx_awareness_posts_lane")
        conn.execute("DROP INDEX IF EXISTS idx_awareness_posts_at")
        conn.execute("ALTER TABLE awareness_posts RENAME TO awareness_posts_v1")
        conn.execute(
            """CREATE TABLE awareness_posts (
                post_id INTEGER PRIMARY KEY,
                lane TEXT NOT NULL,
                content_preview TEXT NOT NULL,
                covers_json TEXT NOT NULL DEFAULT '[]',
                loop_number INTEGER,
                posted_at TEXT NOT NULL,
                discord_message_id TEXT UNIQUE
            )"""
        )
        conn.execute(
            """INSERT INTO awareness_posts
                   (post_id, lane, content_preview, covers_json, loop_number,
                    posted_at, discord_message_id)
               SELECT post_id, lane, content_preview, covers_json, loop_number,
                      posted_at, discord_message_id
               FROM awareness_posts_v1"""
        )
        conn.execute("DROP TABLE awareness_posts_v1")
        conn.execute(
            "CREATE INDEX idx_awareness_posts_lane "
            "ON awareness_posts(lane, posted_at DESC)"
        )
        conn.execute(
            "CREATE INDEX idx_awareness_posts_at ON awareness_posts(posted_at DESC)"
        )


def apply_schema_migrations(conn: sqlite3.Connection) -> None:
    """Advance a compatible v5.1 database to the current schema atomically."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this build "
            f"(v{CURRENT_SCHEMA_VERSION})"
        )
    if version < 1:
        try:
            for statement in _V1_STATEMENTS:
                conn.execute(statement)
            _add_v1_columns(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_awareness_thoughts_loop "
                "ON awareness_thoughts(loop_number DESC)"
            )
            _backfill_v1(conn)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 1
    if version < 2:
        try:
            _apply_v2(conn)
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    assert_current_schema(conn)


def assert_current_schema(conn: sqlite3.Connection) -> None:
    """Raise with a precise diagnosis when a caller bypasses DB initialization."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    problems: list[str] = []
    if version != CURRENT_SCHEMA_VERSION:
        problems.append(f"version={version}, expected={CURRENT_SCHEMA_VERSION}")
    tables = _tables(conn)
    for table, required in REQUIRED_SCHEMA.items():
        if table not in tables:
            problems.append(f"missing table {table}")
            continue
        missing = required - _columns(conn, table)
        if missing:
            problems.append(f"{table} missing {sorted(missing)}")
    if "memories_fts" not in tables:
        problems.append("missing virtual table memories_fts")
    if problems:
        raise RuntimeError(
            "database schema contract is not current; open it through "
            "db.get_connection(): " + "; ".join(problems)
        )


def require_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str] | tuple[str, ...],
) -> None:
    """Cheap domain-boundary assertion for compatibility ``ensure_*`` calls."""
    missing = set(columns) - _columns(conn, table)
    if missing:
        raise RuntimeError(
            f"database schema contract missing {table}.{sorted(missing)}; "
            "open it through db.get_connection()"
        )


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    """Stable hash of designed tables, indexes, triggers, and virtual tables."""
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'memories_fts_%' "
        "ORDER BY type, name"
    ).fetchall()
    canonical = "\n".join(
        "|".join(
            (str(row[0]), str(row[1]), str(row[2]), re.sub(r"\s+", " ", row[3]).strip())
        )
        for row in rows
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# Updated deliberately whenever the fresh-build schema changes.
CURRENT_SCHEMA_FINGERPRINT = (
    "a359f4eae62a8aba4a6bba6c0bef3e7341d9260369fb6f3c9298c18c453745a4"
)


__all__ = [
    "CURRENT_SCHEMA_FINGERPRINT",
    "CURRENT_SCHEMA_VERSION",
    "apply_schema_migrations",
    "assert_current_schema",
    "require_columns",
    "schema_fingerprint",
]
