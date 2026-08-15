"""Explicit forward schema evolution for the v5.1 operational database.

The clean-break v5.1 baseline is migration 0. Every compatible database moves
forward here, once, before a runtime module can read or write it. Domain modules
may validate the contract, but they never issue CREATE/ALTER statements.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

CURRENT_SCHEMA_VERSION = 37
EXPECTED_TABLE_COUNT = 64  # v37 adds the bounded player_side_mode_progress projection


def initialize_empty_database(
    conn: sqlite3.Connection,
    archive_path: str | None = None,
) -> None:
    """Build the complete current schema on an empty connection.

    Migration 0 and every ordered post-cut change live under :mod:`db`; both
    runtime cold starts and test fixtures enter through this function so they
    cannot validate different schema definitions.
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if existing is not None:
        raise RuntimeError("initialize_empty_database requires an empty database")

    from db._schema_baseline import create_baseline_schema

    create_baseline_schema(conn, archive_path)
    apply_schema_migrations(conn)


def build_database(db_path: str, archive_path: str | None = None) -> None:
    """Create a current database file without overwriting an existing path."""
    if os.path.exists(db_path):
        raise SystemExit(f"{db_path} already exists — refusing to overwrite")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        initialize_empty_database(conn, archive_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memories_fts%'"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"created {db_path}: {count} designed tables (expected {EXPECTED_TABLE_COUNT})")
    if count != EXPECTED_TABLE_COUNT:
        raise SystemExit("table count mismatch — reconcile with schema.md §2 before proceeding")


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
    "awareness_thoughts": (
        ("loop_number", "INTEGER"),
        ("tool_trace_json", "TEXT"),
    ),
}

REQUIRED_SCHEMA = {
    "awareness_thoughts": {"thought_id", "loop_number", "tool_trace_json"},
    "awareness_posts": {"post_id", "lane", "content_preview", "posted_at"},
    "players": {"player_tag", "display_name"},
    "player_metadata": {
        "player_tag",
        "email",
        "email_verified_at",
        "email_source",
        "cr_years_celebrated",
        "battle_enrichment_enabled",
    },
    "player_current_state": {"player_tag", "last_seen_api"},
    "player_side_mode_progress": {
        "player_tag",
        "progress_key",
        "trophies",
        "best_trophies",
        "observed_at",
    },
    "war_weeks": {"season_id", "section_index", "defense_fame"},
    "card_catalog": {"card_id", "first_seen_at"},
    "api_sentinel_observations": {"observation_id", "first_entity_key"},
    "admin_command_invocations": {
        "invocation_id",
        "command_key",
        "event_type",
        "discord_user_id",
        "channel_id",
        "write_requested",
        "accepted",
        "invoked_at",
    },
    "game_events": {"event_id", "dedup_key", "change_key"},
    "member_outreach": {"outreach_id", "player_tag", "field", "status", "consent"},
    "evergreen_nudges": {"nudge_key", "last_sent_at"},
    "email_verifications": {"player_tag", "code_hash", "expires_at"},
    "tick_history": {"tick_id", "counters_json"},
    "pol_seasons": {"pol_season_id", "closed"},
    "pol_season_results": {"pol_season_id", "player_tag"},
    "memories": {"memory_id", "kind", "scope"},
    "memory_tags": {"memory_id", "tag"},
    "raw_api_payloads": {
        "payload_id",
        "endpoint",
        "entity_key",
        "fetched_at",
        "last_fetched_at",
        "payload_hash",
    },
    "materialization_runs": {
        "materialization_id",
        "run_kind",
        "status",
        "poll_ok",
        "apply_ok",
        "manage_ok",
        "derivations_ok",
        "source_freshness_json",
    },
    "materialization_inputs": {
        "materialization_input_id",
        "materialization_id",
        "receipt_id",
        "endpoint",
        "entity_key",
        "payload_hash",
    },
    "api_observation_receipts": {
        "receipt_id",
        "payload_id",
        "endpoint",
        "entity_key",
        "fetched_at",
        "payload_hash",
        "admission_status",
    },
    "awareness_delivery_intents": {
        "intent_key",
        "lane",
        "content",
        "covers_json",
        "status",
        "attempts",
    },
    "member_management": {
        "player_tag",
        "judgment_status",
        "judgment_reason",
        "evidence_as_of",
        "materialization_id",
    },
    "leader_action_recommendations": {
        "action_id",
        "decision_note",
        "note_interpret_status",
        "note_interpret_json",
        "note_interpret_note_hash",
        "premise_rejected",
        "premise_fingerprint",
    },
    "battle_card_plays": {"battle_dedup_key", "side", "card_id", "player_tag", "battle_time"},
    "battle_enrichment": {
        "battle_dedup_key",
        "player_tag",
        "battle_time",
        "hp_margin",
        "closeness",
        "our_deck_hash",
        "their_deck_hash",
    },
    "deck_profile": {"deck_hash", "family", "archetype", "avg_elixir", "cards_json"},
    "card_facts": {"card_id", "evolution_level", "targets", "role", "is_win_condition"},
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
        conn.execute("UPDATE awareness_thoughts SET loop_number = rowid WHERE loop_number IS NULL")
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
        except TypeError, ValueError:
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

    if "awareness_thoughts" in tables and "shadow" in _columns(conn, "awareness_thoughts"):
        conn.execute("ALTER TABLE awareness_thoughts DROP COLUMN shadow")

    if "awareness_posts" in tables and "legacy_intent_id" in _columns(conn, "awareness_posts"):
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
            "CREATE INDEX idx_awareness_posts_lane ON awareness_posts(lane, posted_at DESC)"
        )
        conn.execute("CREATE INDEX idx_awareness_posts_at ON awareness_posts(posted_at DESC)")


def _apply_v3(conn: sqlite3.Connection) -> None:
    """Make data-readiness a durable part of the management contract."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS materialization_runs (
            materialization_id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','complete','partial','failed')),
            poll_ok INTEGER NOT NULL DEFAULT 0,
            apply_ok INTEGER NOT NULL DEFAULT 0,
            manage_ok INTEGER NOT NULL DEFAULT 0,
            source_freshness_json TEXT NOT NULL DEFAULT '{}',
            counters_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_materialization_runs_started "
        "ON materialization_runs(started_at DESC)"
    )
    columns = _columns(conn, "member_management")
    additions = (
        (
            "judgment_status",
            "TEXT NOT NULL DEFAULT 'unknown' CHECK (judgment_status IN ('unknown','ready','held'))",
        ),
        ("judgment_reason", "TEXT"),
        ("evidence_as_of", "TEXT"),
        (
            "materialization_id",
            "INTEGER REFERENCES materialization_runs(materialization_id)",
        ),
    )
    for column, declaration in additions:
        if column not in columns:
            conn.execute(f"ALTER TABLE member_management ADD COLUMN {column} {declaration}")
            columns.add(column)


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Close the provenance and delivery transaction boundaries.

    ``raw_api_payloads`` remains the bounded, content-deduplicated payload
    compatibility surface.  Receipts record every successful HTTP response;
    materialization inputs link admitted observations to the generation that
    applied them; awareness intents make a multi-post plan retryable per post.
    """
    raw_columns = _columns(conn, "raw_api_payloads")
    if "last_fetched_at" not in raw_columns:
        conn.execute("ALTER TABLE raw_api_payloads ADD COLUMN last_fetched_at TEXT")
        conn.execute(
            "UPDATE raw_api_payloads SET last_fetched_at = fetched_at WHERE last_fetched_at IS NULL"
        )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_observation_receipts (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload_id INTEGER REFERENCES raw_api_payloads(payload_id)
                ON DELETE SET NULL,
            endpoint TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            admission_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (admission_status IN
                    ('pending','accepted','rejected','not_applicable','legacy')),
            admission_errors_json TEXT NOT NULL DEFAULT '[]'
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_receipts_lookup "
        "ON api_observation_receipts(endpoint, entity_key, payload_hash, receipt_id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_receipts_fetched "
        "ON api_observation_receipts(fetched_at DESC)"
    )
    conn.execute(
        """INSERT INTO api_observation_receipts
               (payload_id, endpoint, entity_key, fetched_at, payload_hash,
                admission_status)
           SELECT payload_id, endpoint, entity_key, fetched_at, payload_hash, 'legacy'
           FROM raw_api_payloads
           WHERE NOT EXISTS (
               SELECT 1 FROM api_observation_receipts r
               WHERE r.payload_id = raw_api_payloads.payload_id
                 AND r.admission_status = 'legacy'
           )"""
    )

    run_columns = _columns(conn, "materialization_runs")
    run_additions = (
        (
            "run_kind",
            "TEXT NOT NULL DEFAULT 'scheduled' "
            "CHECK (run_kind IN ('scheduled','interactive','offline'))",
        ),
        ("derivations_ok", "INTEGER NOT NULL DEFAULT 1"),
    )
    for column, declaration in run_additions:
        if column not in run_columns:
            conn.execute(f"ALTER TABLE materialization_runs ADD COLUMN {column} {declaration}")
            run_columns.add(column)

    conn.execute(
        """CREATE TABLE IF NOT EXISTS materialization_inputs (
            materialization_input_id INTEGER PRIMARY KEY,
            materialization_id INTEGER NOT NULL
                REFERENCES materialization_runs(materialization_id) ON DELETE CASCADE,
            receipt_id INTEGER REFERENCES api_observation_receipts(receipt_id)
                ON DELETE SET NULL,
            endpoint TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            source TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE(materialization_id, endpoint, entity_key, observed_at, payload_hash)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_materialization_inputs_run "
        "ON materialization_inputs(materialization_id, materialization_input_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_materialization_inputs_receipt "
        "ON materialization_inputs(receipt_id) WHERE receipt_id IS NOT NULL"
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS awareness_delivery_intents (
            intent_key TEXT PRIMARY KEY,
            lane TEXT NOT NULL,
            content TEXT NOT NULL,
            covers_json TEXT NOT NULL DEFAULT '[]',
            post_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sending','fulfilled')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_attempt_at TEXT,
            fulfilled_at TEXT,
            discord_message_id TEXT,
            last_error TEXT,
            loop_number INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_awareness_delivery_pending "
        "ON awareness_delivery_intents(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_awareness_delivery_fulfilled "
        "ON awareness_delivery_intents(fulfilled_at DESC)"
    )

    post_columns = _columns(conn, "awareness_posts")
    if "intent_key" not in post_columns:
        conn.execute(
            "ALTER TABLE awareness_posts ADD COLUMN intent_key TEXT "
            "REFERENCES awareness_delivery_intents(intent_key)"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_awareness_posts_intent "
        "ON awareness_posts(intent_key) WHERE intent_key IS NOT NULL"
    )

    # Membership identity is a database invariant, not merely a replay/test
    # assertion. A Clash Royale account can have only one open clan tenure.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_clan_memberships_one_open "
        "ON clan_memberships(player_tag) WHERE left_at IS NULL"
    )


def _apply_v5(conn: sqlite3.Connection) -> None:
    """Add the editorial-critic verdict ledger.

    ``editor_verdicts`` records the post-compose editor's judgment on each
    awareness post before it is sent: the verdict (pass/revise/fallback/error),
    the per-dimension notes, and the original vs. final copy. It is an
    observability ledger, not decision-critical — the deliver path is fail-open,
    so a missing row never blocks a post. Keyed to the live awareness delivery
    (``awareness_delivery_intents.intent_key`` + ``loop_number`` + ``lane``);
    the retired ``editor_verdicts`` table (an old proactive-delivery artifact,
    dropped in V2) shared only the name.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS editor_verdicts (
            verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            intent_key TEXT REFERENCES awareness_delivery_intents(intent_key),
            loop_number INTEGER,
            lane TEXT NOT NULL,
            verdict TEXT NOT NULL DEFAULT 'pass'
                CHECK (verdict IN ('pass','revise','fallback','error')),
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            critique TEXT,
            original_copy TEXT,
            final_copy TEXT,
            covers_json TEXT NOT NULL DEFAULT '[]',
            model TEXT,
            at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_editor_verdicts_intent "
        "ON editor_verdicts(intent_key) WHERE intent_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_editor_verdicts_loop "
        "ON editor_verdicts(loop_number DESC, verdict_id DESC)"
    )


def _apply_v6(conn: sqlite3.Connection) -> None:
    """Add ``member_outreach`` — per-member state for Elixir's DM outreach to
    collect missing profile info (email first).

    One row per (player_tag, field). Tracks the outreach lifecycle
    (eligible → proposed → sent → awaiting_reply → verifying → fulfilled, with
    opted_out / failed / skipped terminals), explicit consent, attempt count,
    cooldown (``next_eligible_at``), and the link to the leader-gate #actions card
    (``leader_action_id``) so a member is DMed only after a human approves. No
    member is ever contacted from this table alone; it is the durable state the
    send/receive phases build on.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS member_outreach (
            outreach_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_tag TEXT NOT NULL,
            field TEXT NOT NULL DEFAULT 'email',
            status TEXT NOT NULL DEFAULT 'eligible'
                CHECK (status IN ('eligible','proposed','sent','awaiting_reply',
                    'verifying','fulfilled','opted_out','failed','skipped')),
            consent TEXT
                CHECK (consent IS NULL OR consent IN ('pending','opted_in','opted_out')),
            attempts INTEGER NOT NULL DEFAULT 0,
            discord_user_id TEXT,
            leader_action_id INTEGER,
            pending_email TEXT,
            last_asked_at TEXT,
            next_eligible_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(player_tag, field)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_member_outreach_status "
        "ON member_outreach(status, next_eligible_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_member_outreach_player ON member_outreach(player_tag)"
    )


def _apply_v7(conn: sqlite3.Connection) -> None:
    """Make a leader's free-text on an #actions card a real feedback loop.

    A note the leader types is captured deterministically, then interpreted
    asynchronously (out of the delivery transaction) into exactly one structured
    effect. Two column groups on ``leader_action_recommendations`` back this:

    - ``note_interpret_*`` — the interpretation lifecycle: ``note_interpret_status``
      (pending → interpreted / failed / undone / none), ``note_interpret_json``
      (the effect, the human-readable "reading" echoed on the card, and a
      prior-state snapshot for exact Undo), and ``note_interpret_note_hash`` (hash
      of the interpreted note text, so an edited/re-added note re-interprets and an
      unchanged one is skipped).
    - ``premise_*`` — the "invalidate premise" effect: when a leader rejects the
      recommendation's premise ("no longer relevant"), ``premise_rejected`` is set
      and ``premise_fingerprint`` pins the evidence anchor, so re-nomination stays
      blocked while the evidence is unchanged and re-fires only on materially new
      evidence.
    """
    columns = _columns(conn, "leader_action_recommendations")
    additions = (
        ("note_interpret_status", "TEXT"),
        ("note_interpret_json", "TEXT"),
        ("note_interpret_note_hash", "TEXT"),
        ("premise_rejected", "INTEGER NOT NULL DEFAULT 0"),
        ("premise_fingerprint", "TEXT"),
    )
    for column, declaration in additions:
        if column not in columns:
            conn.execute(
                f"ALTER TABLE leader_action_recommendations ADD COLUMN {column} {declaration}"
            )
            columns.add(column)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leader_action_note_interpret "
        "ON leader_action_recommendations(note_interpret_status)"
    )


def _apply_v8(conn: sqlite3.Connection) -> None:
    """Drop the ``editor_verdicts`` ledger with the post-compose editorial critic.

    The critic (added in ``_apply_v5``) was wired into the awareness delivery
    path 2026-07-16, backed out the same day (it held the delivery write lock and
    false-grounded on thin facts), and removed entirely 2026-07-17 along with its
    ``ELIXIR_EDITOR_GATE`` flag. Nothing wrote the ledger after the back-out, so
    the table only ever held rows during the one-day live window. The editorial
    *feeders* (``engine.editor.record_*``) stay — they never used this table.
    """
    conn.execute("DROP TABLE IF EXISTS editor_verdicts")


def _apply_v9(conn: sqlite3.Connection) -> None:
    """Drop ``recognition_ledger`` with the deterministic recognition pipeline.

    The recognizers/scorer stopped running 2026-07-10 when the awareness loop
    became the sole proactive owner; the package was removed 2026-07-28 (#207).
    The ledger was a claim store answering "has this moment already been
    narrated?" for that pipeline alone.

    It was briefly assumed load-bearing because it held ``award:`` keys and sat
    on the never-purged list. It was not: award idempotency is
    ``UNIQUE(award_type, season_id, section_index, player_tag)`` on ``awards``
    with INSERT OR IGNORE, and the ledger claim fired *after* and *conditional
    on* that insert. All 17 award claims carried ``intent_id = NULL`` — they
    never posted. Nothing outside the retired package read the table back.
    """
    conn.execute("DROP TABLE IF EXISTS recognition_ledger")


def _apply_v10(conn: sqlite3.Connection) -> None:
    """Drop ``elixir_improvement_suggestions`` — Elixir no longer grades its own
    homework in-product (#209).

    A script reviewed Elixir's own data, scored findings into this table, and
    promoted them to GitHub. Two problems: it is a second queue that must be kept
    in sync with GitHub (the real one), and the Observatory panel reading it
    showed rows from 2026-06-19 for over a month because only a manual script
    ever wrote them. Improvement discovery moved to the Quality Manager's weekly
    AGENT-TEAM pass, which runs in Codex outside the product runtime, reads the
    evidence, and files issues directly.
    """
    conn.execute("DROP TABLE IF EXISTS elixir_improvement_suggestions")


def _apply_v11(conn: sqlite3.Connection) -> None:
    """Superseded by v15.

    Added a tournament-scoped ``tournament_events`` stream; ``tournament_battles``
    already served that purpose with real readers, so nothing ever read it and
    v15 drops it. Kept as a no-op so the version ladder stays contiguous for any
    database that ran v11 before v15 existed.
    """


def _apply_v12(conn: sqlite3.Connection) -> None:
    """Drop ``memory_log`` — an audit trail with no reader (#215).

    It recorded every create/tag/update against ``memories`` (529 rows and
    growing ~154/week, the fastest of any memory table). A whole-repo trace
    found **zero** production SELECTs: the only references outside the migration
    scripts are a column guard and a name in a table-grouping set for the DB
    status report.

    The decision test the issue asked for was "surface a real bounded audit use,
    apply explicit retention, or remove it safely." No use surfaced, and the
    evidence is worse than neutral: ``attach_evidence_ref`` writes ONLY here, so
    its output was already unrecoverable by design. An audit nobody can read is
    not an audit.

    If memory provenance is wanted later, the honest version is a memory-detail
    view in the Observatory backed by a table with a reader -- not a write-only
    log accumulating forever.
    """
    conn.execute("DROP TABLE IF EXISTS memory_log")


def _apply_v13(conn: sqlite3.Connection) -> None:
    """Drop ``system_signals`` — an announcement queue with no drain (#212).

    It carried two unrelated things. ``capability_unlock`` rows were nine
    hardcoded release announcements re-queued on every boot; release news now
    goes out through the real flow (RELEASES.md + #announcements + email via
    scripts/cut_release.py), and the last one shipped 2026-04-17.
    ``api_*_sentinel`` rows were operator alerts about Clash Royale API drift.

    Neither could be delivered. The publisher was removed from Discord with a
    comment saying the Observatory owned it; the Observatory never did, so the
    only caller of ``_publish_pending_system_signal_updates`` sat inside an
    admin command with no dispatch branch. `[].tournamentTag` appeared in
    player_battlelog on 2026-07-24 and nobody was told.

    Drift moved to ``runtime.health.check_api_drift``, posting to #elixir-log.
    (That check lasted five days: the whole health job was retired in v20, and
    the drift query moved out of the product runtime to the AGENT-TEAM Error
    Watch runbook in ``AGENT-TEAM/run-elixir.md``. Understand Clash Royale owns
    the resulting interpretation and source correction.) The sentinel observations themselves
    (``api_sentinel_observations``) are untouched — only the undeliverable queue
    in front of them goes.
    """
    conn.execute("DROP TABLE IF EXISTS system_signals")


def _apply_v14(conn: sqlite3.Connection) -> None:
    """Drop four verified-dead tables (#211).

    ``event_threads`` and ``post_quality_runs`` were never in this schema at
    all — the v5.1 migration script created them and they were never carried
    into ``db/schema.py``. Migration orphans; nothing in the running bot knew
    they existed.

    ``watches`` was declared, indexed and guarded, with a reader
    (``open_watches``) that had no callers — and **zero** ``INSERT INTO
    watches`` anywhere in the repo, tests included. The live watch concept
    moved into ``memories`` (`Watch:` / `Hold:` titles); this was the abandoned
    original.

    ``clan_daily_battle_rollups`` froze at battle_date 2026-07-03 when
    ``_recompute_clan_daily_battle_rollups`` lost its last caller in the v5.1
    cut, while its player-level sibling stayed current. An earlier QA fix had
    already re-pointed the live trend path at ``battle_events`` (see the note at
    storage/trends.py), so nothing read the stale numbers — the remaining reader
    contributed only a row count.
    """
    for table in (
        "event_threads",
        "post_quality_runs",
        "watches",
        "clan_daily_battle_rollups",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _apply_v15(conn: sqlite3.Connection) -> None:
    """Drop ``tournament_events`` — a stream I added and nothing read (#210).

    The grain call was right: battles inside a tournament must not pollute the
    clan stream. The implementation was not. A tournament-scoped battle record
    ALREADY existed as ``tournament_battles``, with real readers (the recap and
    card stats), so this was a third table recording the same battles in a
    different shape, written in parallel rather than as their source. It shipped
    with 0 rows and no reader — the exact "durable store nobody drains" pattern
    the rest of this sprint spent the day removing.

    The clan-level ``tournament_finished`` event on ``clan_events`` stays: it is
    genuinely read, as a hard-post the awareness loop narrates.
    """
    conn.execute("DROP TABLE IF EXISTS tournament_events")


def _apply_v16(conn: sqlite3.Connection) -> None:
    """Capture the OPPONENT's deck on every battle (#216).

    The CR battle log returns 8 cards for both ``team[0]`` and ``opponent[0]``.
    Ingest read only the polled player's, so ``battle_events`` recorded who a
    member fought (``opponent_tag``) but never what they fought. That made the
    question Elixir most wants to answer about a loss -- "what are you losing
    TO?" -- unanswerable: ``get_member_recent_losses`` documents itself as
    aggregating opponent cards, takes a ``top_cards`` argument it never used,
    and selected a hardcoded ``NULL AS opponent_deck_json``.

    Not reconstructable after the fact. A tag can be re-scouted, but the deck an
    opponent brought to one specific battle exists only in that battle log entry
    and ages out. Every battle polled before this migration keeps a NULL here
    unless its raw payload is still retained (see scripts/backfill_battle_fields.py).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(battle_events)")}
    if "opponent_deck_json" not in columns:
        conn.execute("ALTER TABLE battle_events ADD COLUMN opponent_deck_json TEXT")


_V17_BATTLE_COLUMNS = (
    # --- the polled player -------------------------------------------------
    ("support_cards_json", "TEXT"),  # tower troop; 100% coverage, never stored
    ("elixir_leaked", "REAL"),
    ("king_tower_hp", "INTEGER"),
    ("princess_towers_hp_json", "TEXT"),
    ("global_rank", "INTEGER"),
    ("clan_tag", "TEXT"),  # their clan AT BATTLE TIME, not their clan today
    ("rounds_json", "TEXT"),  # duel sub-decks
    # --- the opponent ------------------------------------------------------
    ("opponent_name", "TEXT"),
    ("opponent_clan_tag", "TEXT"),
    ("opponent_clan_name", "TEXT"),
    ("opponent_clan_badge_id", "INTEGER"),
    ("opponent_support_cards_json", "TEXT"),
    ("opponent_elixir_leaked", "REAL"),
    ("opponent_king_tower_hp", "INTEGER"),
    ("opponent_princess_towers_hp_json", "TEXT"),
    ("opponent_global_rank", "INTEGER"),
    ("opponent_starting_trophies", "INTEGER"),
    ("opponent_trophy_change", "INTEGER"),
    ("opponent_rounds_json", "TEXT"),
    # --- the battle --------------------------------------------------------
    ("modifiers_json", "TEXT"),
    ("boat_battle_side", "TEXT"),
    ("boat_battle_won", "INTEGER"),
    ("new_towers_destroyed", "INTEGER"),
    ("prev_towers_destroyed", "INTEGER"),
    ("remaining_towers", "INTEGER"),
)


def _apply_v17(conn: sqlite3.Connection) -> None:
    """Store the WHOLE battle, not a summary of it (#216).

    An audit of 58,105 battles against the live API found `battle_events` kept
    17 of the ~30 facts each battle carries. The rest were parsed and dropped.
    Four of them had readers already asking for them and getting a hardcoded
    NULL back -- the same starvation that hid the missing opponent deck:

      * `supportCards` (the tower troop) -- 100% coverage, and
        `get_member_current_deck` returns `support_cards: []` unconditionally
        even though a whole aggregation pipeline sits behind it. 6.9% of use is
        non-default (Dagger Duchess, Royal Chef, Cannoneer), so this is real
        deck identity, not a constant.
      * opponent `name` / `clan` -- selected as `NULL AS opponent_name` /
        `NULL AS opponent_clan_tag` in two readers.
      * duel `rounds` -- `NULL AS team_rounds_json`, so war-deck analysis counts
        duel battles it cannot read the decks of.

    And three with no reader yet but no way to reconstruct later:
    `elixirLeaked` (the only per-battle SKILL signal the API gives that is not
    the outcome), king/princess tower HP (margin of defeat -- today a 0-1
    nail-biter and a 3-crown blowout are both just outcome='L'), and the boat
    battle result fields.

    Deliberately NOT stored: `isLadderTournament`, which was False on all
    58,105 battles observed. If Supercell ever populates it, the API sentinel
    catches the drift; a column that is constant is noise.

    Known limit: 2v2 keeps only opponent[0]. The second opponent's deck is
    dropped, as it always has been.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(battle_events)")}
    for name, decl in _V17_BATTLE_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE battle_events ADD COLUMN {name} {decl}")


def _apply_v18(conn: sqlite3.Connection) -> None:
    """Store the equipped tower troop on player_current_state (#216).

    Separate from v17 only because v17 had already run on the live database by
    the time this gap surfaced; a shipped migration is not edited in place.

    Same starvation as the rest: `get_member_current_deck` normalizes and
    returns a `support_cards` list, fed by `NULL AS
    current_deck_support_cards_json`, so it was unconditionally empty. The
    player endpoint provides `currentDeckSupportCards`.
    """
    state = {row[1] for row in conn.execute("PRAGMA table_info(player_current_state)")}
    if "current_deck_support_cards_json" not in state:
        conn.execute(
            "ALTER TABLE player_current_state ADD COLUMN current_deck_support_cards_json TEXT"
        )


def _apply_v19(conn: sqlite3.Connection) -> None:
    """Drop ``tournament_battles`` -- battle_events is the battle store (#216).

    This table existed for exactly one reason: it kept BOTH decks, and
    ``battle_events`` kept only the polled player's. That gap is what Jamie
    noticed when I described the table as "only justified by the opponent's
    deck", and closing it (v16) removed the table's reason to exist.

    Its 7 rows predated ``battle_events`` (which starts 2026-05-07) so they were
    replayed into the main stream first, from the full ``raw_json`` payload each
    row carried. They came out
    RICHER than the table held: support cards, elixir leaked and tower HP, none
    of which it had columns for.

    ``get_tournament_battles`` now reads ``battle_events`` and collapses the
    per-polled-player rows back to one row per match, because the two stores
    have different grain: a match between two clan members appears twice here,
    once from each side.
    """
    conn.execute("DROP TABLE IF EXISTS tournament_battles")


def _apply_v20(conn: sqlite3.Connection) -> None:
    """Drop ``runtime_incidents`` -- the log was always the record.

    The ledger existed so a swallowed failure left a queryable row instead of
    vanishing, and every best-effort ``except`` in the codebase wrote to it. It
    did not work. In 25 days it recorded **0 rows** while
    ``logs/elixir-error.log`` held **159 real errors** -- and the daily
    ``engine-health`` job consulted the ledger, so it reported "all clear"
    through every one of them. A #thinking permission outage ran for hours
    behind that all-clear.

    Detecting production problems is an operator/AGENT-TEAM job, not an internal
    function of the clan bot, so the ledger and the health check both go. Every
    ``record_incident`` call site is now a ``log.exception``/``log.error`` on
    that module's own logger, which lands in the rotating ERROR-only log that
    ``runtime/logging_setup.py`` writes and the Operations Manager reads.
    """
    conn.execute("DROP TABLE IF EXISTS runtime_incidents")


def _apply_v21(conn: sqlite3.Connection) -> None:
    """Remove the retired decision-case schema after the #216 cutover.

    ``leader_action_recommendations`` has been the sole active decision store
    since the code-only cutover. Operations deployed and exercised the final
    writer with no ``case_id`` dependency before this contract migration was
    allowed to ship. The 39 legacy case rows are intentionally discarded per
    the product decision; leader-action history remains intact.
    """
    conn.execute("DROP INDEX IF EXISTS idx_leader_actions_case")
    conn.execute("DROP TABLE IF EXISTS decision_cases")
    columns = _columns(conn, "leader_action_recommendations")
    if "case_id" in columns:
        conn.execute("ALTER TABLE leader_action_recommendations DROP COLUMN case_id")


def _apply_v22(conn: sqlite3.Connection) -> None:
    """Contract two dead tables and eight write-only conversation columns.

    The code-only cutover shipped first and was exercised in production on v21.
    Rebuild the three tables that referenced ``discord_channels`` so foreign-key
    enforcement stays enabled while the obsolete parent table is removed. All
    transcript rows, summaries, identifiers, and active indexes are preserved.
    """
    tables = _tables(conn)
    required = {"conversation_threads", "messages", "channel_state"}
    missing = required - tables
    if missing:
        raise RuntimeError(f"v22 contract migration missing tables: {sorted(missing)}")

    dead_thread_columns = {
        "channel_id",
        "discord_user_id",
        "member_id",
        "created_at",
        "last_active_at",
    }
    dead_state_columns = {
        "last_elixir_post_at",
        "last_topics_json",
        "recent_style_notes_json",
    }
    rebuild_conversations = bool(
        dead_thread_columns & _columns(conn, "conversation_threads")
        or dead_state_columns & _columns(conn, "channel_state")
        or "discord_channels" in tables
    )

    if rebuild_conversations:
        conn.execute(
            """CREATE TABLE conversation_threads_v22 (
                thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                UNIQUE(scope_type, scope_key)
            )"""
        )
        conn.execute(
            "INSERT INTO conversation_threads_v22 (thread_id, scope_type, scope_key) "
            "SELECT thread_id, scope_type, scope_key FROM conversation_threads"
        )
        conn.execute(
            """CREATE TABLE messages_v22 (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT UNIQUE,
                thread_id INTEGER NOT NULL
                    REFERENCES conversation_threads_v22(thread_id) ON DELETE CASCADE,
                channel_id TEXT,
                discord_user_id TEXT
                    REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
                member_id INTEGER,
                author_type TEXT NOT NULL,
                workflow TEXT,
                event_type TEXT,
                content TEXT NOT NULL,
                summary TEXT,
                created_at TEXT NOT NULL,
                raw_json TEXT,
                intent_id INTEGER
            )"""
        )
        conn.execute(
            """INSERT INTO messages_v22 (
                message_id, discord_message_id, thread_id, channel_id,
                discord_user_id, member_id, author_type, workflow, event_type,
                content, summary, created_at, raw_json, intent_id
            )
            SELECT
                message_id, discord_message_id, thread_id, channel_id,
                discord_user_id, member_id, author_type, workflow, event_type,
                content, summary, created_at, raw_json, intent_id
            FROM messages"""
        )
        conn.execute(
            """CREATE TABLE channel_state_v22 (
                channel_id TEXT PRIMARY KEY,
                last_summary TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO channel_state_v22 (channel_id, last_summary) "
            "SELECT channel_id, last_summary FROM channel_state"
        )

        # Children go first so DROP never invokes an FK action against retained
        # rows. Renaming the new parent updates messages_v22's FK target.
        conn.execute("DROP TABLE messages")
        conn.execute("DROP TABLE channel_state")
        conn.execute("DROP TABLE conversation_threads")
        conn.execute("DROP TABLE IF EXISTS discord_channels")

        conn.execute("ALTER TABLE conversation_threads_v22 RENAME TO conversation_threads")
        conn.execute("ALTER TABLE messages_v22 RENAME TO messages")
        conn.execute("ALTER TABLE channel_state_v22 RENAME TO channel_state")
        conn.execute(
            "CREATE INDEX idx_threads_scope ON conversation_threads(scope_type, scope_key)"
        )
        conn.execute(
            "CREATE INDEX idx_messages_thread_time ON messages(thread_id, created_at DESC)"
        )
        conn.execute("CREATE INDEX idx_messages_intent ON messages(intent_id, created_at DESC)")

    conn.execute("DROP TABLE IF EXISTS arena_relay_screenshot_observations")


def _apply_v23(conn: sqlite3.Connection) -> None:
    """Add privacy-minimal durable telemetry for Discord command use (#227)."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS admin_command_invocations (
            invocation_id INTEGER PRIMARY KEY,
            command_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            discord_user_id TEXT,
            channel_id TEXT,
            write_requested INTEGER NOT NULL DEFAULT 0
                CHECK (write_requested IN (0, 1)),
            accepted INTEGER NOT NULL DEFAULT 1
                CHECK (accepted IN (0, 1)),
            invoked_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_command_invocations_command_time "
        "ON admin_command_invocations(command_key, invoked_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_command_invocations_time "
        "ON admin_command_invocations(invoked_at DESC)"
    )


def _apply_v24(conn: sqlite3.Connection) -> None:
    """Remove a live-only, unread awareness column exposed by schema parity (#228).

    ``prompt_json`` was added directly to the deployed ``awareness_thoughts``
    table before the versioned schema ladder owned that table. No current or
    historical repository reader/writer uses it; LLM prompt capture belongs to
    ``llm_calls.prompt_json``. The fresh schema never declared the column, so
    dropping it makes the deployed semantic contract match fresh builds.
    """
    if "prompt_json" in _columns(conn, "awareness_thoughts"):
        conn.execute("ALTER TABLE awareness_thoughts DROP COLUMN prompt_json")


def _apply_v25(conn: sqlite3.Connection) -> None:
    """Normalize battle timestamps to the project's ISO form, and key member
    memories by real player tags.

    **battle_time was CR-compact.** The Clash Royale API returns
    '20260418T153949.000Z' and the ingest path stored it verbatim, so a column
    compared as TEXT held a format that sorts against ISO in a way that is
    silently wrong: char 4 is '0' (48) against '-' (45), so a compact value is
    GREATER than any ISO value from the same year. An ISO "last 7 days" bound
    therefore matched every row and returned the whole history with plausible
    looking numbers. The repo had learned this four times in four local
    workarounds and centralized it zero times; converting at ingest
    (engine.normalize.canonical_utc_timestamp) removes the format, and this
    converts the rows already stored so no reader needs to straddle both.

    The target keeps the trailing 'Z'. A naive '2026-07-30T17:30:30' is UTC by
    convention only, which is how engine/profiles.py came to compare a LOCAL
    date.today() against UTC battle times and silently drop every battle after
    ~19:00 Chicago (Jamie, 2026-07-30: "the naive timestamp would just allow
    timezone bugs"). observed_at in this same table already carries the Z.

    battle_events.observed_at had the same contamination in 7 rows written
    2026-04-18, which is why `SELECT MAX(observed_at)` returned April against a
    table whose newest row was July.

    **memories.member_tag held names, not tags.** `canon_tag` only uppercases
    and prefixes '#', so a caller passing a display name produced a
    valid-looking key: '#KING_THING', '#VIJAY', '#28'. 1,868 of 3,203 non-null
    values were names, and members ended up split across both spellings — King
    Thing had 15 memories under his real tag and 88 under '#KING_THING', so a
    lookup by tag reached 15% of what Elixir actually knew about him. Rows are
    re-keyed onto the real tag by matching the name against players.
    """
    # 1. battle_time -> ISO-Z. The compact body is 'YYYYMMDDTHHMMSS'; any
    #    fractional/zone suffix is dropped, matching canonical_utc_timestamp().
    conn.execute(
        """UPDATE battle_events
              SET battle_time =
                  substr(battle_time, 1, 4) || '-' || substr(battle_time, 5, 2) || '-' ||
                  substr(battle_time, 7, 2) || 'T' || substr(battle_time, 10, 2) || ':' ||
                  substr(battle_time, 12, 2) || ':' || substr(battle_time, 14, 2) || 'Z'
            WHERE battle_time IS NOT NULL AND battle_time NOT LIKE '____-__-__T%'"""
    )
    # 2. The 7 poisoned observed_at rows, same shape.
    conn.execute(
        """UPDATE battle_events
              SET observed_at =
                  substr(observed_at, 1, 4) || '-' || substr(observed_at, 5, 2) || '-' ||
                  substr(observed_at, 7, 2) || 'T' || substr(observed_at, 10, 2) || ':' ||
                  substr(observed_at, 12, 2) || ':' || substr(observed_at, 14, 2) || 'Z'
            WHERE observed_at IS NOT NULL AND observed_at NOT LIKE '____-__-__T%'"""
    )
    # 2b. observed_at also holds 49 NAIVE rows from before the Z-convention
    #     settled. Same ambiguity as the compact ones — UTC only if you already
    #     know — so the column ends this migration with exactly one shape.
    conn.execute(
        """UPDATE battle_events
              SET observed_at = observed_at || 'Z'
            WHERE observed_at IS NOT NULL
              AND observed_at LIKE '____-__-__T%'
              AND observed_at NOT LIKE '%Z'"""
    )
    # 3. Re-key name-keyed memories onto the real player tag, in three passes
    #    from strictest to loosest. Each requires EXACTLY ONE matching player,
    #    so an ambiguous name is left alone rather than attached to the wrong
    #    member. The passes exist because the bogus keys were built from
    #    whatever name string the caller happened to hold:
    #      1. the materialized display_name ("King Thing" -> '#KING_THING')
    #      2. the raw current_name, which is where the un-folded forms live
    #         ('#²⁸' when display_name is already "28", '#SEBASTIÁN')
    #      3. either name with separators stripped ('#KINGTHING')
    for expr in (
        "COALESCE(p.display_name, p.current_name)",
        "p.current_name",
    ):
        conn.execute(
            f"""UPDATE memories
                   SET member_tag = (
                       SELECT p.player_tag FROM players p
                        WHERE '#' || UPPER(REPLACE({expr}, ' ', '_'))
                              = UPPER(REPLACE(memories.member_tag, ' ', '_'))
                   )
                 WHERE member_tag IS NOT NULL
                   AND member_tag NOT IN (SELECT player_tag FROM players)
                   AND (
                       SELECT COUNT(*) FROM players p
                        WHERE '#' || UPPER(REPLACE({expr}, ' ', '_'))
                              = UPPER(REPLACE(memories.member_tag, ' ', '_'))
                   ) = 1"""
        )
    strip = "REPLACE(REPLACE(REPLACE({0}, ' ', ''), '_', ''), '-', '')"
    for expr in ("COALESCE(p.display_name, p.current_name)", "p.current_name"):
        conn.execute(
            f"""UPDATE memories
                   SET member_tag = (
                       SELECT p.player_tag FROM players p
                        WHERE '#' || UPPER({strip.format(expr)})
                              = UPPER({strip.format("memories.member_tag")})
                   )
                 WHERE member_tag IS NOT NULL
                   AND member_tag NOT IN (SELECT player_tag FROM players)
                   AND (
                       SELECT COUNT(*) FROM players p
                        WHERE '#' || UPPER({strip.format(expr)})
                              = UPPER({strip.format("memories.member_tag")})
                   ) = 1"""
        )


def _apply_v26(conn: sqlite3.Connection) -> None:
    """Repair battle dedup keys broken by the v25 timestamp normalization.

    `engine/ingest.py` builds the key as
    `{player_tag}:{battle_time}:{opponent_tag}`, so the key is only stable while
    battle_time's FORMAT is stable. v25 converted that column from CR-compact to
    ISO-Z and rewrote the column but not the key, so the same battle re-polled
    afterwards produced a DIFFERENT key:

        #20JJJ2CCRU:20260730T031613.000Z:#UQQ20UJU2   (stored before v25)
        #20JJJ2CCRU:2026-07-30T03:16:13Z:#UQQ20UJU2   (built after v25)

    INSERT OR IGNORE saw no collision and inserted a second row. The battlelog
    re-serves a player's last 25 battles every poll, so this duplicated a real
    battle exactly once each — 1,348 rows before it was caught, inflating every
    count over battle_events (card plays, activity windows, war participation).
    Kick logic was spared only because it reads MAX(battle_time), not COUNT(*).

    Keeps the earliest row per real battle so original observed_at survives,
    then REBUILDS every key from the columns rather than string-patching the old
    format — that way the key is derived from the same values it claims to
    describe, and cannot drift from them again.
    """
    conn.execute(
        """DELETE FROM battle_events
            WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM battle_events
                 GROUP BY player_tag, battle_time, COALESCE(opponent_tag, '')
            )"""
    )
    conn.execute(
        """UPDATE battle_events
              SET dedup_key = player_tag || ':' || battle_time || ':'
                              || COALESCE(opponent_tag, '')
            WHERE dedup_key <> player_tag || ':' || battle_time || ':'
                               || COALESCE(opponent_tag, '')"""
    )


def _apply_v27(conn: sqlite3.Connection) -> None:
    """Battle Intelligence Feature 1: the computed foundation.

    Two derived, dedup-keyed tables (docs/plans/battle-intelligence-1-data.md):

    * ``battle_card_plays`` — every card played on BOTH sides of every 1v1
      battle (member + opponent), form-aware. The subject member's tag is
      stamped on both sides for index locality; ``battle_time``/``outcome``/
      ``mode_group``/``is_competitive`` are denormalized from ``battle_events``
      so card win-rate queries never join back.
    * ``battle_enrichment`` — one row per 1v1 battle. The full column shape ships
      now (so Features 2-3 fill columns, never ``ALTER``); Feature 1 populates
      only the computed ones (``hp_margin``/``closeness``/``discipline_delta``/
      ``level_gap``/``our_deck_hash``/``their_deck_hash``). ``battle_time`` is
      denormalized (rebuildable, and the table is derived anyway) so the
      per-member time-axis index needs no join. The Feature-2/3 columns stay
      NULL until those features land.

    Both key on ``battle_events.dedup_key`` verbatim (the v25/v26 lesson); the
    Stage-A worker backfills rows with ``INSERT OR IGNORE``. Duels
    (``rounds_json``) and 2v2 (``teammate_tag``) get no rows.

    The ``loss_nature`` CHECK keeps an explicit ``IS NULL OR`` guard so Feature 1's
    computed-only inserts (which leave it NULL) pass on every SQLite build.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS battle_card_plays (
            battle_dedup_key TEXT NOT NULL REFERENCES battle_events(dedup_key),
            side             TEXT NOT NULL CHECK (side IN ('member', 'opponent')),
            card_id          INTEGER NOT NULL,
            level            INTEGER,
            evolution_level  INTEGER,
            star_level       INTEGER,
            player_tag       TEXT NOT NULL,
            battle_time      TEXT NOT NULL,
            outcome          TEXT,
            mode_group       TEXT,
            is_competitive   INTEGER,
            PRIMARY KEY (battle_dedup_key, side, card_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bcp_card ON battle_card_plays(card_id, side, battle_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bcp_member_card ON battle_card_plays(player_tag, card_id, side)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS battle_enrichment (
            battle_dedup_key TEXT PRIMARY KEY REFERENCES battle_events(dedup_key),
            player_tag       TEXT NOT NULL,
            battle_time      TEXT NOT NULL,
            hp_margin        INTEGER,
            closeness        INTEGER,
            discipline_delta REAL,
            level_gap        REAL,
            our_deck_hash    TEXT,
            their_deck_hash  TEXT,
            expected_advantage INTEGER,
            performance      INTEGER,
            loss_nature      TEXT CHECK (loss_nature IS NULL OR loss_nature IN
                             ('structural', 'piloting', 'level', 'close', 'unclear')),
            notable          INTEGER NOT NULL DEFAULT 0,
            confidence       TEXT,
            commentary       TEXT,
            coaching_note    TEXT,
            verdict          TEXT,
            model            TEXT,
            prompt_version   INTEGER,
            input_hash       TEXT,
            enriched_at      TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_be_player_time ON battle_enrichment(player_tag, battle_time)"
    )


def _apply_v28(conn: sqlite3.Connection) -> None:
    """Battle Intelligence Feature 2: deck profiles + the matchup matrix.

    * ``deck_profile`` — one row per form-aware ``deck_hash``: its ``family`` and
      conventional ``archetype`` label (from the deterministic ``_classify``
      RULES, not a model, so no era versioning is needed — a deck's archetype is
      a pure function of its cards) plus computed ``avg_elixir``. Filled for every
      observed deck, both sides, at $0.
    * ``matchup_expectation`` — the 6-family x 6-family matrix (36 cells). Filled
      from MEASURED clan battle outcomes, not a model: all 36 cells clear n>=30 on
      live data, so the clan's own history IS the matrix (the blend would let
      measured dominate anyway). Recomputed weekly (calibration = recompute).

    Feature 1's ``battle_enrichment.expected_advantage`` / ``performance`` columns
    already exist; the Stage-B worker fills them by snapshotting the cell advantage
    for each battle's (our_family, their_family). Pure SQL/rules, no LLM.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deck_profile (
            deck_hash      TEXT PRIMARY KEY,
            family         TEXT NOT NULL,
            archetype      TEXT NOT NULL,
            win_condition  TEXT,
            avg_elixir     REAL,
            cards_json     TEXT NOT NULL,
            scored_at      TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deck_profile_family ON deck_profile(family)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS matchup_expectation (
            our_family        TEXT NOT NULL,
            their_family      TEXT NOT NULL,
            advantage         INTEGER NOT NULL CHECK (advantage BETWEEN -2 AND 2),
            measured_win_rate REAL,
            n                 INTEGER NOT NULL,
            basis             TEXT,
            computed_at       TEXT NOT NULL,
            PRIMARY KEY (our_family, their_family)
        )"""
    )


def _apply_v29(conn: sqlite3.Connection) -> None:
    """Battle Intelligence Feature 3: the per-battle prose allowlist.

    The prose columns (``loss_nature``/``notable``/``confidence``/``commentary``/
    ``verdict``/``model``/``prompt_version``/``input_hash``/``enriched_at``) already
    exist on ``battle_enrichment`` (Feature 1 shipped the full shape). This
    migration adds only the one bit of source-of-truth state Feature 3 needs — the
    allowlist flag on ``player_metadata`` — and seeds the two members the prose
    gate opens for (King Thing, raquaza). Prose is gated to allowlist ∩
    ``battle_time >= 2026-07-20`` as a cost/iteration control; opening it wider is
    a one-line query change once the prompt is graded. No new table (count stays 63).
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(player_metadata)")}
    if "battle_enrichment_enabled" not in columns:
        conn.execute(
            "ALTER TABLE player_metadata "
            "ADD COLUMN battle_enrichment_enabled INTEGER NOT NULL DEFAULT 0"
        )
    # Seed the allowlist by flag only — no INSERT, so a fresh/empty build (no
    # players yet) doesn't trip the player_metadata->players foreign key. Both
    # seeds are active members with a metadata row on the live DB; the UPDATE is a
    # no-op on a fresh build.
    for tag in ("#20JJJ2CCRU", "#UL2V9QRG0"):
        conn.execute(
            "UPDATE player_metadata SET battle_enrichment_enabled = 1 WHERE player_tag = ?",
            (tag,),
        )


def _apply_v31(conn: sqlite3.Connection) -> None:
    """Deck Intelligence: a current-meta deck snapshot the clan's own data cannot supply.

    46 members are a thin slice of the meta, and measurement here is skill-confounded:
    clan-wide deck win rates carry as much player-composition as deck quality (player
    skill spans 36.4%-70.2%), and with only 2 shared-deck observations there is no
    evidence a deck transfers between members. So "what decks should I consider?" cannot
    be answered from clan battles at all — a member in a rut needs decks NOBODY here
    plays, which is exactly where local data is silent.

    Filled by ``scripts/refresh_meta_snapshot.py`` (Opus 5 + web search), refreshed on
    balance patches. ``cards_json`` holds catalog-RESOLVED ``[[card_id, form], ...]``:
    the model answers in card names and the resolver rejects anything the catalog does
    not know, so a hallucinated card cannot reach a recommendation. Names that fail to
    resolve are kept in ``unresolved_json`` so a rename shows up as data, not silence.

    Advisory, never authoritative: every meta deck is still gated by what the member
    OWNS and can field at level before it is ever shown.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meta_decks (
            meta_deck_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            archetype       TEXT,
            family          TEXT,
            tier            TEXT,
            cards_json      TEXT NOT NULL,
            unresolved_json TEXT,
            win_condition   TEXT,
            note            TEXT,
            source_url      TEXT,
            model           TEXT,
            snapshot_at     TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meta_decks_snapshot ON meta_decks(snapshot_at DESC)"
    )


def _apply_v30(conn: sqlite3.Connection) -> None:
    """Battle Intelligence v2 Layer 1: enriched card behavior facts.

    ``card_facts`` holds the behavior primitives the CR API does not expose (it gives
    only id/name/elixirCost/rarity/maxLevel — nothing about what a card targets, whether
    it flies, or what role it plays). Filled by ``scripts/enrich_card_facts.py`` using
    Opus 5 + web search, keyed **form-aware** on ``(card_id, evolution_level)`` because
    Evo/Hero forms behave differently (Evo Bats gain lifesteal; Evo Knight gains a dash).

    Deliberately **primitives only** — the deck-level roles (air answer, tank answer,
    splash answer) are DERIVED from these in ``engine/card_roles.py``, so the model's
    error surface stays small and the roles are auditable/fixable in one place without
    re-enriching. Tiers rather than raw HP/damage: raw stats are level-dependent and move
    every balance patch, tiers are stable and are all the reads need.

    Current-state, not a slowly-changing dimension: intrinsic card behavior is very
    stable (only a rework changes it), and meta strength is already measured in
    ``matchup_expectation``. Era-faithfulness comes from snapshotting the DERIVED
    per-battle tags, the same move Feature 1 uses for ``expected_advantage``.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS card_facts (
            card_id                INTEGER NOT NULL,
            evolution_level        INTEGER NOT NULL DEFAULT 0,
            unit_domain            TEXT,
            targets                TEXT,
            attack_style           TEXT,
            splash_hits_air        INTEGER NOT NULL DEFAULT 0,
            dps_tier               TEXT,
            hp_tier                TEXT,
            unit_count             TEXT,
            range_type             TEXT,
            role                   TEXT,
            spell_tier             TEXT,
            is_win_condition       INTEGER NOT NULL DEFAULT 0,
            fragile_to_small_spell INTEGER NOT NULL DEFAULT 0,
            special_json           TEXT,
            source                 TEXT,
            note                   TEXT,
            model                  TEXT,
            enriched_at            TEXT,
            PRIMARY KEY (card_id, evolution_level)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_facts_role ON card_facts(role)")

    # Deck-level derived facts (computed from card_facts by the Stage-B worker).
    for column, decl in (
        ("air_answer_count", "INTEGER"),
        ("tank_answer_count", "INTEGER"),
        ("splash_answer_count", "INTEGER"),
        ("swarm_count", "INTEGER"),
        ("bait_unit_count", "INTEGER"),
        ("has_big_spell", "INTEGER"),
        ("has_small_spell", "INTEGER"),
        ("facts_complete", "INTEGER"),
    ):
        if column not in {r[1] for r in conn.execute("PRAGMA table_info(deck_profile)")}:
            conn.execute(f"ALTER TABLE deck_profile ADD COLUMN {column} {decl}")

    # Per-battle structural tags (snapshotted; replace Feature 3's prose).
    for column, decl in (
        ("air_matchup", "TEXT"),
        ("wincon_pressure", "TEXT"),
        ("spell_bait_exposed", "INTEGER"),
        ("level_validity", "TEXT"),
        ("decisive_factor", "TEXT"),
    ):
        if column not in {r[1] for r in conn.execute("PRAGMA table_info(battle_enrichment)")}:
            conn.execute(f"ALTER TABLE battle_enrichment ADD COLUMN {column} {decl}")


def apply_schema_migrations(conn: sqlite3.Connection) -> None:
    """Advance a compatible v5.1 database to the current schema atomically."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this build (v{CURRENT_SCHEMA_VERSION})"
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
        version = 2
    if version < 3:
        try:
            _apply_v3(conn)
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 3
    if version < 4:
        try:
            _apply_v4(conn)
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 4
    if version < 5:
        try:
            _apply_v5(conn)
            conn.execute("PRAGMA user_version = 5")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 5
    if version < 6:
        try:
            _apply_v6(conn)
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 6
    if version < 7:
        try:
            _apply_v7(conn)
            conn.execute("PRAGMA user_version = 7")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 7
    if version < 8:
        try:
            _apply_v8(conn)
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 8
    if version < 9:
        try:
            _apply_v9(conn)
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 9
    if version < 10:
        try:
            _apply_v10(conn)
            conn.execute("PRAGMA user_version = 10")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 10
    if version < 11:
        try:
            _apply_v11(conn)
            conn.execute("PRAGMA user_version = 11")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 11
    if version < 12:
        try:
            _apply_v12(conn)
            conn.execute("PRAGMA user_version = 12")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 12
    if version < 13:
        try:
            _apply_v13(conn)
            conn.execute("PRAGMA user_version = 13")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 13
    if version < 14:
        try:
            _apply_v14(conn)
            conn.execute("PRAGMA user_version = 14")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 14
    if version < 15:
        try:
            _apply_v15(conn)
            conn.execute("PRAGMA user_version = 15")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 15
    if version < 16:
        try:
            _apply_v16(conn)
            conn.execute("PRAGMA user_version = 16")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 16
    if version < 17:
        try:
            _apply_v17(conn)
            conn.execute("PRAGMA user_version = 17")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 17
    if version < 18:
        try:
            _apply_v18(conn)
            conn.execute("PRAGMA user_version = 18")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 18
    if version < 19:
        try:
            _apply_v19(conn)
            conn.execute("PRAGMA user_version = 19")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 19
    if version < 20:
        try:
            _apply_v20(conn)
            conn.execute("PRAGMA user_version = 20")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 20
    if version < 21:
        try:
            _apply_v21(conn)
            conn.execute("PRAGMA user_version = 21")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 21
    if version < 22:
        try:
            # sqlite3 does not implicitly open a transaction for DDL. Start one
            # explicitly so the multi-table contract is all-or-nothing.
            conn.execute("BEGIN IMMEDIATE")
            _apply_v22(conn)
            conn.execute("PRAGMA user_version = 22")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 23:
        try:
            _apply_v23(conn)
            conn.execute("PRAGMA user_version = 23")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 24:
        try:
            _apply_v24(conn)
            conn.execute("PRAGMA user_version = 24")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 25:
        try:
            _apply_v25(conn)
            conn.execute("PRAGMA user_version = 25")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 26:
        try:
            _apply_v26(conn)
            conn.execute("PRAGMA user_version = 26")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 27:
        try:
            _apply_v27(conn)
            conn.execute("PRAGMA user_version = 27")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 28:
        try:
            _apply_v28(conn)
            conn.execute("PRAGMA user_version = 28")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 29:
        try:
            _apply_v29(conn)
            conn.execute("PRAGMA user_version = 29")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 30:
        try:
            _apply_v30(conn)
            conn.execute("PRAGMA user_version = 30")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 31:
        try:
            _apply_v31(conn)
            conn.execute("PRAGMA user_version = 31")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 32:
        try:
            _apply_v32(conn)
            conn.execute("PRAGMA user_version = 32")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 33:
        try:
            _apply_v33(conn)
            conn.execute("PRAGMA user_version = 33")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 34:
        try:
            _apply_v34(conn)
            conn.execute("PRAGMA user_version = 34")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if version < 35:
        try:
            _apply_v35(conn)
            conn.execute("PRAGMA user_version = 35")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 35
    if version < 36:
        try:
            _apply_v36(conn)
            conn.execute("PRAGMA user_version = 36")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        version = 36
    if version < 37:
        try:
            _apply_v37(conn)
            conn.execute("PRAGMA user_version = 37")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    assert_current_schema(conn)


# Columns cut in v32. Named here rather than inline so the list is auditable and a
# re-run is a no-op: DROP COLUMN raises if the column is already gone.
_V32_DROPPED_COLUMNS = (
    # The archetype-matchup verdict. Player-adjusted, family matchup is worth ~3
    # points of win rate against 22 for card levels, and the one apparent standout
    # (siege) turned out to be who plays siege. Nothing member-facing consumes it.
    "expected_advantage",
    "performance",
    # The retired per-battle LLM prose layer (Feature 3). Its job, its feature flag
    # and its grading script are already gone; only the columns were left behind,
    # holding 97 rows of commentary out of 13,348 and a tool note describing an
    # allowlist that no longer exists. coaching_note and verdict never held a single
    # row in their entire lifetime.
    "commentary",
    "coaching_note",
    "verdict",
    "loss_nature",
    "notable",
    "confidence",
    "model",
    "prompt_version",
    "input_hash",
    # Write-only structural tags: computed on every battle, stored, selected by the
    # coaching query, and deliberately never reported, because both are measurably
    # flat against outcome. Cutting the column also cuts the per-battle computation.
    "air_matchup",
    "wincon_pressure",
    "spell_bait_exposed",
)


def _apply_v32(conn: sqlite3.Connection) -> None:
    """Delete Battle Intelligence v1's vestigial surface.

    Two things accumulated here. The archetype matchup machinery, which measured out
    at ~3 points once adjusted for who plays which archetype and is no longer read by
    anything a member sees. And the remains of the per-battle prose feature, which was
    removed without taking its schema with it.

    Deferring either is what turns a known dead column into an hour of archaeology in
    six months, so both go now rather than being carried as "harmless".
    """
    conn.execute("DROP TABLE IF EXISTS matchup_expectation")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(battle_enrichment)")}
    for column in _V32_DROPPED_COLUMNS:
        if column in existing:
            conn.execute(f"ALTER TABLE battle_enrichment DROP COLUMN {column}")


# Tables whose entity_key must hold the canonical bare-upper form. Both are
# written by the same function (db.record_api_observation), so they normalize
# together or the payload/receipt pair stops joining on the same key.
_V33_ENTITY_KEY_TABLES = ("api_observation_receipts", "raw_api_payloads")
_V33_CANON = "UPPER(LTRIM(TRIM(entity_key), '#'))"


def _apply_v33(conn: sqlite3.Connection) -> None:
    """Store entity_key already canonical, so its index can actually be used.

    The receipt lookup in engine/readiness.py compared
    ``UPPER(LTRIM(entity_key,'#')) = ?``. Wrapping the column in functions makes
    idx_api_receipts_lookup unusable past its first column, so SQLite searched on
    ``endpoint=?`` alone, scanned every receipt for that endpoint and then built a
    temp B-tree for the ORDER BY — 0.651 ms per lookup against 18,901 rows, and
    getting worse as the table grows. Measured after this change: 0.001 ms, with
    the plan collapsing to a covering-index search and no sort.

    The normalization was already true of every stored tag (0 rows carried a '#'
    or stray whitespace) — it was being re-derived on every read for an invariant
    the data already held. The only values that actually change here are the
    literal 'global' sentinel rows for the cards/events endpoints, which no read
    path compares by case.
    """
    for table in _V33_ENTITY_KEY_TABLES:
        # A UNIQUE(endpoint, entity_key, payload_hash) sits on raw_api_payloads.
        # If two rows differed ONLY by the case of entity_key they would collide,
        # and the UPDATE would fail mid-migration. Refuse up front instead.
        #
        # The test is COUNT(DISTINCT entity_key), not COUNT(*): receipts are
        # append-only with no unique constraint, so repeated (endpoint, key, hash)
        # triples are the normal case — the same payload fetched twice. Only two
        # DIFFERENT keys canonicalizing to one is a merge.
        collisions = conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT endpoint, {_V33_CANON} AS k, payload_hash
                    FROM {table}
                    GROUP BY endpoint, k, payload_hash
                    HAVING COUNT(DISTINCT entity_key) > 1
                )"""
        ).fetchone()[0]
        if collisions:
            raise RuntimeError(
                f"v33: {table} has {collisions} entity_key groups that differ only "
                f"by case or '#'; normalizing would merge distinct rows. Resolve "
                f"them by hand before migrating."
            )
        conn.execute(
            f"UPDATE {table} SET entity_key = {_V33_CANON} WHERE entity_key <> {_V33_CANON}"
        )


def _apply_v34(conn: sqlite3.Connection) -> None:
    """Drop llm_calls from the clan database.

    Telemetry moved to its own file on 2026-08-03 (storage/telemetry.py) because
    every model call was taking the clan database's single write lock — the same
    lock whose contention we were trying to measure. The clan copy has been
    frozen since the cutover and nothing reads or writes it: the facade writer
    delegates to the telemetry file, and the Observatory panels read it through
    _llm_conn().

    Leaving it was worse than useless. A frozen table full of real rows reads as
    live data — querying it shows spend collapsing to zero on the cutover date,
    which looks exactly like a cost win.

    Refuses to drop if the telemetry file exists but holds FEWER rows than the
    clan copy, which would mean the history copy never completed. If the
    telemetry file is absent entirely this is a fresh or restored database with
    no history worth keeping, and the drop proceeds.
    """
    tables = _tables(conn)
    if "llm_calls" not in tables:
        return
    clan_rows = int(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])
    if clan_rows:
        import os as _os

        path = _os.getenv("ELIXIR_TELEMETRY_DB_PATH") or "elixir-telemetry.db"
        if _os.path.exists(path):
            probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                copied = int(probe.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])
            except sqlite3.Error:
                copied = 0
            finally:
                probe.close()
            if copied < clan_rows:
                raise RuntimeError(
                    f"v34: refusing to drop llm_calls — the clan DB holds {clan_rows} rows "
                    f"but the telemetry file at {path} holds {copied}. Copy the history "
                    f"first; this migration destroys the only other copy."
                )
    conn.execute("DROP TABLE IF EXISTS llm_calls")


def _apply_v35(conn: sqlite3.Connection) -> None:
    """Drop the API sentinel's touch-maintained columns.

    Until 2026-08-04 every field of every API payload did a SELECT and an UPDATE
    to refresh `last_seen_at` — about 362,000 statements a day to maintain 776
    rows and discover roughly three genuinely new things a month. The value was
    always the INSERT; the touching existed to keep `last_seen_at` current for
    one Observatory page, and that page was deleted. The sentinel now writes only
    on novelty, so the column would freeze — and a frozen timestamp that looks
    live is exactly the trap the clan-DB llm_calls copy was.

    `announced_signal_key` goes with it: nothing has ever written it, so the
    AGENT-TEAM runbooks' `WHERE announced_signal_key IS NULL` filter was a no-op
    that quietly hid 30 legacy rows. Those runbooks are updated in the same
    change.

    `sample_json` and `first_entity_key` STAY — engine/emitters/game.py reads
    both to build the clan-facing event and badge posts.

    SQLite refuses DROP COLUMN on an indexed column, so the two indexes come
    down first; the endpoint index is rebuilt on first_seen_at, which is the
    column anyone actually orders by now.
    """
    if "api_sentinel_observations" not in _tables(conn):
        return
    existing = {r[1] for r in conn.execute("PRAGMA table_info(api_sentinel_observations)")}
    conn.execute("DROP INDEX IF EXISTS idx_api_sentinel_endpoint")
    conn.execute("DROP INDEX IF EXISTS idx_api_sentinel_announced")
    for column in ("last_seen_at", "announced_signal_key"):
        if column in existing:
            conn.execute(f"ALTER TABLE api_sentinel_observations DROP COLUMN {column}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_sentinel_endpoint "
        "ON api_sentinel_observations(endpoint, first_seen_at DESC)"
    )


def _apply_v36(conn: sqlite3.Connection) -> None:
    """Repair Rage friendlies carried forward under the pre-taxonomy label.

    The May 3 rolling backup retains every source payload behind these rows:
    four matches, mirrored into eight player rollups, all ``type=friendly``
    with no event or tournament tag.  The current classifier therefore calls
    them ``friendly``; only the durable pre-June-19 rollups still say ``other``.

    Do not generalize this UPDATE to every friendly-looking name.  Rollups do
    not retain battle type or event/tournament tags, and several modes with a
    ``_Friendly`` suffix legitimately ran as special events.  This exact
    id/name/old-group cohort is the one whose source semantics were recovered.
    """
    collisions = conn.execute(
        """SELECT COUNT(*)
           FROM player_daily_battle_rollups stale
           JOIN player_daily_battle_rollups current
             ON current.player_tag = stale.player_tag
            AND current.battle_date = stale.battle_date
            AND current.game_mode_id = stale.game_mode_id
            AND current.mode_group = 'friendly'
           WHERE stale.game_mode_id = 72000071
             AND stale.game_mode_name = 'Rage_Friendly'
             AND stale.mode_group = 'other'"""
    ).fetchone()[0]
    if collisions:
        raise RuntimeError(
            "v36: Rage_Friendly historical repair would collide with "
            f"{collisions} existing friendly rollup rows; resolve the duplicate "
            "days before migrating"
        )
    conn.execute(
        """UPDATE player_daily_battle_rollups
           SET mode_group = 'friendly'
           WHERE game_mode_id = 72000071
             AND game_mode_name = 'Rage_Friendly'
             AND mode_group = 'other'"""
    )


def _apply_v37(conn: sqlite3.Connection) -> None:
    """Materialize bounded current Player.progress values for game-mode reads.

    ``Player.progress`` is an open-ended map, so keeping its complete JSON blob
    in ``player_current_state`` would create an unbounded projection.  The
    capability needs only one row per player/key with current and best values.
    Backfill from retained current profile receipts makes the new read immediately
    useful after migration rather than waiting for the next adaptive poll. The
    normalized profile baseline intentionally excludes this open-ended map.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS player_side_mode_progress (
            player_tag TEXT NOT NULL REFERENCES players(player_tag) ON DELETE CASCADE,
            progress_key TEXT NOT NULL,
            trophies INTEGER,
            best_trophies INTEGER,
            arena_id INTEGER,
            arena_name TEXT,
            arena_raw_name TEXT,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (player_tag, progress_key)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_side_mode_progress_observed "
        "ON player_side_mode_progress(observed_at DESC)"
    )
    conn.execute(
        """INSERT INTO player_side_mode_progress
               (player_tag, progress_key, trophies, best_trophies, arena_id,
                arena_name, arena_raw_name, observed_at)
           WITH latest_profile_receipt AS (
               SELECT entity_key, MAX(COALESCE(last_fetched_at, fetched_at)) AS fetched_at
               FROM raw_api_payloads
               WHERE endpoint = 'player'
               GROUP BY entity_key
           )
           SELECT '#' || receipt.entity_key,
                  entry.key,
                  json_extract(entry.value, '$.trophies'),
                  json_extract(entry.value, '$.bestTrophies'),
                  json_extract(entry.value, '$.arena.id'),
                  json_extract(entry.value, '$.arena.name'),
                  json_extract(entry.value, '$.arena.rawName'),
                  COALESCE(receipt.last_fetched_at, receipt.fetched_at)
           FROM raw_api_payloads AS receipt
           JOIN latest_profile_receipt AS latest
             ON latest.entity_key = receipt.entity_key
            AND latest.fetched_at = COALESCE(receipt.last_fetched_at, receipt.fetched_at)
           JOIN players ON players.player_tag = '#' || receipt.entity_key
           JOIN json_each(receipt.payload_json, '$.progress') AS entry
           WHERE receipt.endpoint = 'player'
             AND json_type(entry.value) = 'object'
           ON CONFLICT(player_tag, progress_key) DO UPDATE SET
               trophies = excluded.trophies,
               best_trophies = excluded.best_trophies,
               arena_id = excluded.arena_id,
               arena_name = excluded.arena_name,
               arena_raw_name = excluded.arena_raw_name,
               observed_at = excluded.observed_at"""
    )


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
    """Stable hash of the semantic schema contract.

    Existing databases reached the same structure through ``ALTER TABLE``
    while fresh databases declare current columns directly. SQLite preserves
    those histories in ``sqlite_master.sql`` (column order, comments, and
    formatting), so hashing raw DDL falsely reports drift. This contract hashes
    columns, checks, keys, indexes, foreign keys, strict/without-rowid flags,
    triggers, and virtual-table declarations while ignoring cosmetic DDL history.
    """

    def quoted(name: str) -> str:
        return "'" + name.replace("'", "''") + "'"

    def normalized_sql(sql: str | None) -> str | None:
        if sql is None:
            return None
        without_comments = re.sub(r"--[^\n]*", " ", sql)
        return re.sub(r"\s+", " ", without_comments).strip()

    def check_constraints(sql: str) -> list[str]:
        """Extract balanced CHECK expressions without depending on DDL layout."""
        body = re.sub(r"--[^\n]*", " ", sql)
        checks: list[str] = []
        for match in re.finditer(r"\bCHECK\s*\(", body, re.IGNORECASE):
            start = match.end() - 1
            depth = 0
            quote: str | None = None
            index = start
            while index < len(body):
                char = body[index]
                if quote is not None:
                    if char == quote:
                        if index + 1 < len(body) and body[index + 1] == quote:
                            index += 2
                            continue
                        quote = None
                elif char in {"'", '"'}:
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        expression = re.sub(r"\s+", " ", body[start + 1 : index]).strip()
                        checks.append(expression)
                        break
                index += 1
        return sorted(checks)

    table_flags = {
        str(row[1]): {"without_rowid": int(row[4]), "strict": int(row[5])}
        for row in conn.execute("PRAGMA table_list")
        if str(row[1]) != "sqlite_schema"
    }
    table_contract: dict[str, object] = {}
    contract: dict[str, object] = {"tables": table_contract, "other_objects": []}
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'memories_fts_%' ORDER BY name"
    ).fetchall()
    for table_name, table_sql in tables:
        name = str(table_name)
        sql = str(table_sql or "")
        if re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE", sql, re.IGNORECASE):
            table_contract[name] = {
                "virtual_sql": normalized_sql(sql),
                **table_flags.get(name, {}),
            }
            continue

        columns = sorted(
            (
                str(row[1]),
                str(row[2] or ""),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in conn.execute(f"PRAGMA table_xinfo({quoted(name)})")
        )
        foreign_keys = sorted(
            (
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                int(row[1]),
            )
            for row in conn.execute(f"PRAGMA foreign_key_list({quoted(name)})")
        )
        indexes = []
        for index_row in conn.execute(f"PRAGMA index_list({quoted(name)})"):
            index_name = str(index_row[1])
            origin = str(index_row[3])
            index_columns = [
                (
                    int(row[0]),
                    "column"
                    if int(row[1]) >= 0
                    else "rowid"
                    if int(row[1]) == -1
                    else "expression",
                    None if row[2] is None else str(row[2]),
                    int(row[3]),
                    str(row[4]),
                    int(row[5]),
                )
                for row in conn.execute(f"PRAGMA index_xinfo({quoted(index_name)})")
            ]
            master = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            indexes.append(
                {
                    "name": index_name if origin == "c" else None,
                    "unique": int(index_row[2]),
                    "origin": origin,
                    "partial": int(index_row[4]),
                    "columns": index_columns,
                    "sql": normalized_sql(master[0]) if master else None,
                }
            )
        indexes.sort(key=lambda value: json.dumps(value, sort_keys=True))
        table_contract[name] = {
            "columns": columns,
            "checks": check_constraints(sql),
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            **table_flags.get(name, {}),
        }

    contract["other_objects"] = [
        (str(row[0]), str(row[1]), str(row[2]), normalized_sql(row[3]))
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('trigger','view') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Updated deliberately whenever the fresh-build schema changes.
CURRENT_SCHEMA_FINGERPRINT = "bc84d8a92755d79193a80b8915f333eac294565802ac166dc662874537f7aef5"


__all__ = [
    "CURRENT_SCHEMA_FINGERPRINT",
    "CURRENT_SCHEMA_VERSION",
    "EXPECTED_TABLE_COUNT",
    "apply_schema_migrations",
    "assert_current_schema",
    "build_database",
    "initialize_empty_database",
    "require_columns",
    "schema_fingerprint",
]
