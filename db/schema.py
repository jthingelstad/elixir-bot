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

CURRENT_SCHEMA_VERSION = 19


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
    "llm_calls": (("prompt_json", "TEXT"), ("response_json", "TEXT")),
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
    },
    "player_current_state": {"player_tag", "last_seen_api"},
    "war_weeks": {"season_id", "section_index", "defense_fame"},
    "card_catalog": {"card_id", "first_seen_at"},
    "api_sentinel_observations": {"observation_id", "first_entity_key"},
    "llm_calls": {"call_id", "prompt_json", "response_json"},
    "runtime_incidents": {"incident_id", "component", "resolved_at"},
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

    Drift now rides ``runtime.health.check_api_drift`` to #elixir-log, which is
    a live operator channel the health job already posts to, and the AGENT-TEAM
    Data Analyst owns characterizing it. The sentinel observations themselves
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
    unless its raw payload is still retained (see scripts/backfill_opponent_decks.py).
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
    row carried -- see scripts/migrate_tournament_battles.py. They came out
    RICHER than the table held: support cards, elixir leaked and tower HP, none
    of which it had columns for.

    ``get_tournament_battles`` now reads ``battle_events`` and collapses the
    per-polled-player rows back to one row per match, because the two stores
    have different grain: a match between two clan members appears twice here,
    once from each side.
    """
    conn.execute("DROP TABLE IF EXISTS tournament_battles")


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
        "|".join((str(row[0]), str(row[1]), str(row[2]), re.sub(r"\s+", " ", row[3]).strip()))
        for row in rows
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# Updated deliberately whenever the fresh-build schema changes.
CURRENT_SCHEMA_FINGERPRINT = "cec6231f4d76a8b3534b116883dbd56d95bc74d11c002827f0276893a5cdf3bc"


__all__ = [
    "CURRENT_SCHEMA_FINGERPRINT",
    "CURRENT_SCHEMA_VERSION",
    "apply_schema_migrations",
    "assert_current_schema",
    "require_columns",
    "schema_fingerprint",
]
