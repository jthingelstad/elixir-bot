"""Shared test fixtures (v5.1).

The autouse fixtures protect the live database and the Anthropic API:

- `_isolate_default_sqlite_db` routes implicit `db.get_connection()` calls to a
  per-test copy of a session-built v5.1 schema template (db refuses schemas
  without the v5.1 spine, so a bare tempfile is not enough). Tests that pass an
  explicit connection or database path keep full control of their storage.
- `_block_real_llm_calls` patches `agent.core._get_client` so any test that
  reaches the bottom-level API call raises a loud RuntimeError.

The schema template is built once per session from
scripts.migrate_v51.schema_v51 (new-table DDL inline; carried-table DDL
exported from the read-only archive, per the migration convention).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ARCHIVE = os.path.join(_REPO_ROOT, "elixir-v5-archive-2026H2.db")
_CR_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "cr")


def load_cr_fixture(name: str):
    """Load a real CR API payload captured from raw_api_payloads (2026-07).

    Available: clan, player_evo, player_plain, battlelog,
    riverrace_training, riverrace_warday, riverrace_colosseum, riverracelog.
    These are REAL payloads — when Supercell drifts the shape, refresh them
    from the raw log (see AGENTS.md 'Testing') instead of hand-editing.
    """
    if not name.endswith(".json"):
        name += ".json"
    with open(os.path.join(_CR_FIXTURES, name)) as f:
        return json.load(f)


@pytest.fixture()
def cr_fixture():
    """Fixture-form of load_cr_fixture for tests that prefer injection."""
    return load_cr_fixture


# --- Global DB invariants -----------------------------------------------
# Cheap structural truths that must hold after ANY engine activity. The
# autouse teardown below runs them against the per-test default DB, so a
# test that corrupts state fails even if its own asserts were too narrow.


@contextlib.contextmanager
def _check(name: str, skipped: list[str]):
    """Scope one invariant check.

    A fixture that builds a minimal schema legitimately lacks some of these
    tables, so an absent table skips *this* check and records the name. Any
    other OperationalError propagates.

    This granularity is the point. Checks used to share one try/except at the
    call site, so when #207 dropped `recognition_ledger` the second check
    raised and every check below it stopped running — silently, for every test,
    for the life of the sweep. `test_invariant_sweep.py` asserts `skipped` is
    empty against the full template so that cannot happen again.
    """
    try:
        yield
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc) and "no such column" not in str(exc):
            raise
        skipped.append(name)


def assert_db_invariants(conn: sqlite3.Connection, label: str = "") -> list[str]:
    """Assert the cheap structural truths, and report which checks were skipped.

    Returns the names of checks whose tables were absent. Against a full-schema
    database that list must be empty.
    """
    problems: list[str] = []
    skipped: list[str] = []

    def q1(sql):
        return conn.execute(sql).fetchone()[0]

    # 1) one open membership per player per clan
    with _check("clan_memberships", skipped):
        dupes = conn.execute(
            "SELECT player_tag, COUNT(*) c FROM clan_memberships "
            "WHERE left_at IS NULL GROUP BY player_tag, clan_tag HAVING c > 1"
        ).fetchall()
        if dupes:
            problems.append(f"duplicate open memberships: {[tuple(d) for d in dupes]}")

        overlaps = q1(
            "SELECT COUNT(*) FROM clan_memberships a JOIN clan_memberships b "
            "ON a.player_tag = b.player_tag AND a.clan_tag = b.clan_tag "
            "AND a.membership_id < b.membership_id "
            "WHERE a.joined_at < COALESCE(b.left_at, '9999-12-31') "
            "AND b.joined_at < COALESCE(a.left_at, '9999-12-31')"
        )
        if overlaps:
            problems.append(f"clan_memberships has {overlaps} overlapping interval(s)")

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        problems.append(f"foreign_key_check has {len(fk_violations)} violation(s)")

    # 2) memories FTS mirror in sync (triggers keep them 1:1)
    with _check("memories_fts", skipped):
        n_mem, n_fts = (
            q1("SELECT COUNT(*) FROM memories"),
            q1("SELECT COUNT(*) FROM memories_fts"),
        )
        if n_mem != n_fts:
            problems.append(f"memories={n_mem} but memories_fts={n_fts}")

    # 3) no space-separated timestamps — the ' ' vs 'T' bug class (a bare
    #    datetime('now') renders '2026-07-04 12:00:00', which compares wrong
    #    against ISO-T strings; 12 live sites were fixed for this).
    for table, col in (
        ("player_events", "observed_at"),
        ("player_events", "created_at"),
        ("clan_events", "observed_at"),
        ("war_events", "observed_at"),
        ("battle_events", "observed_at"),
        ("state_baselines", "observed_at"),
        ("memories", "created_at"),
        ("memories", "updated_at"),
    ):
        with _check(f"{table}.{col}", skipped):
            n = q1(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE '% %'")
            if n:
                problems.append(f"{table}.{col}: {n} space-format timestamp(s)")

    # 4) profile-owned facts cannot be erased by a sparse roster refresh.
    with _check("player_current_state", skipped):
        projection_mismatches = q1(
            "WITH profile AS (SELECT entity_tag AS player_tag, "
            "CAST(json_extract(payload_json, '$.best_trophies') AS INTEGER) AS best, "
            "CAST(json_extract(payload_json, '$.exp_level') AS INTEGER) AS exp "
            "FROM state_baselines WHERE entity_kind='player' AND aspect='profile') "
            "SELECT COUNT(*) FROM profile p JOIN player_current_state cs USING(player_tag) "
            "WHERE (p.best IS NOT NULL AND cs.best_trophies IS NOT p.best) "
            "OR (p.exp > 0 AND cs.exp_level IS NOT p.exp)"
        )
        if projection_mismatches:
            problems.append(
                f"player_current_state has {projection_mismatches} profile-owned mismatch(es)"
            )

    # 5) a daily metric row never nulls a best-trophy value it once carried.
    with _check("player_daily_metrics", skipped):
        best_drops = q1(
            "WITH ordered AS (SELECT player_tag, metric_date, best_trophies, "
            "LAG(best_trophies) OVER (PARTITION BY player_tag ORDER BY metric_date) AS prior "
            "FROM player_daily_metrics) SELECT COUNT(*) FROM ordered "
            "WHERE best_trophies IS NULL AND prior IS NOT NULL"
        )
        if best_drops:
            problems.append(f"player_daily_metrics has {best_drops} best-trophy null regression(s)")

    # 6) internal war timestamps use dashed ISO/date text. Battle timestamps are
    # deliberately raw CR compact and are not part of this check.
    for table, col in (
        ("war_seasons", "started_at"),
        ("war_seasons", "ended_at"),
        ("war_weeks", "created_date"),
        ("war_weeks", "finish_time"),
        ("war_week_clans", "completed_at"),
        ("war_week_clans", "observed_at"),
        ("war_participation", "observed_at"),
        ("war_attendance_days", "observed_at"),
        ("war_events", "observed_at"),
        ("war_events", "window_start"),
        ("war_events", "created_at"),
    ):
        with _check(f"{table}.{col}", skipped):
            n = q1(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
                f"AND (length({col}) < 10 OR substr({col},5,1)<>'-' OR substr({col},8,1)<>'-')"
            )
            if n:
                problems.append(f"{table}.{col}: {n} noncanonical timestamp(s)")

    assert not problems, f"DB invariants violated{f' ({label})' if label else ''}:\n" + "\n".join(
        f"  - {p}" for p in problems
    )
    return skipped


@pytest.fixture(scope="session")
def v51_schema_template(tmp_path_factory):
    """Build the v5.1 schema once; tests copy this template. Uses the local
    archive when present, else the frozen carried_ddl.sql (CI has no archive)."""
    from scripts.migrate_v51.schema_v51 import build

    template = str(tmp_path_factory.mktemp("schema") / "v51-template.db")
    build(template, _ARCHIVE if os.path.exists(_ARCHIVE) else None)
    # build() leaves the DB in WAL mode with the schema in the -wal sidecar;
    # checkpoint so a plain file copy carries the full schema.
    conn = sqlite3.connect(template)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return template


@pytest.fixture(autouse=True)
def _isolate_default_sqlite_db(tmp_path, monkeypatch, v51_schema_template):
    """Route implicit DB access to a per-test v5.1-schema database."""
    db_path = str(tmp_path / "elixir-test.db")
    shutil.copy(v51_schema_template, db_path)
    monkeypatch.setenv("ELIXIR_DB_PATH", db_path)

    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", db_path, raising=False)
    yield db_path

    # Post-test invariant sweep: whatever the test did to the default DB,
    # the structural truths must still hold (cheap — a handful of COUNTs
    # against a small per-test file; failures point at the class of live
    # bug the test quietly reproduced).
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        try:
            assert_db_invariants(conn, label="post-test sweep")
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _flush_async_status_writes(_isolate_default_sqlite_db):
    """Finish best-effort worker writes before each temp database disappears."""
    yield
    from runtime import status as runtime_status

    runtime_status.flush_status_writes()


@pytest.fixture(autouse=True)
def _block_real_llm_calls(_isolate_default_sqlite_db):
    """Fail loudly if any test reaches the Anthropic API."""

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "Unmocked LLM call in test. Mock at the workflow layer "
            "(e.g. patch('elixir.elixir_agent.generate_channel_update')) "
            "or patch agent.memory_tasks.extract_inference_facts before the "
            "code path reaches agent.core._create_chat_completion."
        )

    fake_client = MagicMock()
    fake_client.messages.create = _boom
    # db.record_llm_call / db.record_prompt_failure are facade re-exports;
    # patch the db attributes directly. Pass-through when the test provides
    # an explicit conn; no-op otherwise so nothing pollutes the default DB.
    import db as _db

    real_record_llm = _db.record_llm_call
    real_record_failure = _db.record_prompt_failure

    def _guarded_record_llm(*args, **kwargs):
        if kwargs.get("conn") is not None:
            return real_record_llm(*args, **kwargs)
        return None

    def _guarded_record_failure(*args, **kwargs):
        if kwargs.get("conn") is not None:
            return real_record_failure(*args, **kwargs)
        return None

    with (
        patch("agent.core._get_client", return_value=fake_client),
        patch("db.record_llm_call", side_effect=_guarded_record_llm),
        patch("db.record_prompt_failure", side_effect=_guarded_record_failure),
    ):
        yield


@pytest.fixture()
def engine_conn(_isolate_default_sqlite_db):
    """An engine-discipline connection to the per-test v5.1 DB."""
    conn = sqlite3.connect(_isolate_default_sqlite_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()
