"""Shared test fixtures (v5.1).

The autouse fixtures protect the live databases and the Anthropic API:

- `_isolate_default_sqlite_db` routes implicit `db.get_connection()` calls to a
  per-test copy of a session-built v5.1 schema template (db refuses schemas
  without the v5.1 spine, so a bare tempfile is not enough). Tests that pass an
  explicit connection or database path keep full control of their storage.
- `_isolate_memory_db` routes the durable-memory store (elixir-v5-memory.db)
  to a per-test tempfile.
- `_block_real_llm_calls` patches `agent.core._get_client` so any test that
  reaches the bottom-level API call raises a loud RuntimeError.

The schema template is built once per session from
scripts.migrate_v51.schema_v51 (new-table DDL inline; carried-table DDL
exported from the read-only archive, per the migration convention).
"""
from __future__ import annotations

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

def assert_db_invariants(conn: sqlite3.Connection, label: str = "") -> None:
    from engine.recognition.compose import FAIL_CLOSED_LANE, PREFIX_LANE

    problems: list[str] = []

    def q1(sql):
        return conn.execute(sql).fetchone()[0]

    # 1) one open membership per player per clan
    dupes = conn.execute(
        "SELECT player_tag, COUNT(*) c FROM clan_memberships "
        "WHERE left_at IS NULL GROUP BY player_tag, clan_tag HAVING c > 1"
    ).fetchall()
    if dupes:
        problems.append(f"duplicate open memberships: {[tuple(d) for d in dupes]}")

    # 2) recognition ledger: one claim per key (one-claim-wins)
    if q1("SELECT COUNT(*) FROM recognition_ledger") != q1(
        "SELECT COUNT(DISTINCT recognition_key) FROM recognition_ledger"
    ):
        problems.append("recognition_ledger has duplicate recognition_keys")

    # 3) memories FTS mirror in sync (triggers keep them 1:1)
    try:
        n_mem, n_fts = q1("SELECT COUNT(*) FROM memories"), q1(
            "SELECT COUNT(*) FROM memories_fts"
        )
        if n_mem != n_fts:
            problems.append(f"memories={n_mem} but memories_fts={n_fts}")
    except sqlite3.OperationalError:
        pass  # schema without FTS (some minimal fixtures)

    # 4) no space-separated timestamps — the ' ' vs 'T' bug class (a bare
    #    datetime('now') renders '2026-07-04 12:00:00', which compares wrong
    #    against ISO-T strings; 12 live sites were fixed for this).
    for table, col in (
        ("player_events", "observed_at"),
        ("player_events", "created_at"),
        ("clan_events", "observed_at"),
        ("war_events", "observed_at"),
        ("battle_events", "observed_at"),
        ("state_baselines", "observed_at"),
        ("recognition_ledger", "claimed_at"),
        ("communication_intents", "created_at"),
        ("communication_intents", "expires_at"),
        ("memories", "created_at"),
        ("memories", "updated_at"),
    ):
        n = q1(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE '% %'")
        if n:
            problems.append(f"{table}.{col}: {n} space-format timestamp(s)")

    # 5) intents only on known lanes / statuses
    known_lanes = set(PREFIX_LANE.values()) | {FAIL_CLOSED_LANE}
    bad = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT lane FROM communication_intents WHERE lane IS NOT NULL"
        )
        if r[0] not in known_lanes
    ]
    if bad:
        problems.append(f"unknown intent lanes: {bad}")
    bad_status = [
        r[0]
        for r in conn.execute("SELECT DISTINCT status FROM communication_intents")
        if r[0] not in {"pending", "fulfilled", "failed", "expired"}
    ]
    if bad_status:
        problems.append(f"unknown intent statuses: {bad_status}")

    assert not problems, (
        f"DB invariants violated{f' ({label})' if label else ''}:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )


@pytest.fixture(scope="session")
def v51_schema_template(tmp_path_factory):
    """Build the v5.1 schema once; tests copy this template."""
    if not os.path.exists(_ARCHIVE):
        pytest.exit(
            f"v5.1 test schema needs the read-only archive at {_ARCHIVE} "
            "(carried-table DDL exports from it — migration.md Phase 2)."
        )
    from scripts.migrate_v51.schema_v51 import build

    template = str(tmp_path_factory.mktemp("schema") / "v51-template.db")
    build(template, _ARCHIVE)
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
        except sqlite3.OperationalError:
            pass  # test replaced/gutted the schema deliberately
        finally:
            conn.close()


@pytest.fixture(scope="session")
def v51_full_ddl(v51_schema_template):
    """The full 53-table DDL (new + carried), harvested from the template."""
    conn = sqlite3.connect(f"file:{v51_schema_template}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'clan_memory_vec%' "
        # memories_fts shadow tables are (re)created by the virtual-table
        # CREATE itself — dumping their DDL too makes the replay collide.
        "AND name NOT LIKE 'memories_fts_%'"
    ).fetchall()
    conn.close()
    return ";\n".join(r[0] for r in rows)


@pytest.fixture(autouse=True)
def _seed_empty_dbs_on_get_connection(monkeypatch, v51_full_ddl):
    """WORKAROUND for a db/__init__.py ordering bug (recorded in the Phase-8
    report): `_enable_sqlite_vec` creates `clan_memory_vec` on the connection
    BEFORE `get_connection`'s spine/empty check, so an empty DB is seen as
    "vec tables only" and refused — the intended empty-DB NEW_DDL path
    (db/__init__.py:535) is unreachable. Until that ordering is fixed, seed
    the full v5.1 schema into empty targets so legacy fixtures behave as the
    empty-DB path intends. Remove this fixture when db/ is fixed."""
    import db as _db

    real = _db.get_connection

    def patched(db_path=None):
        path = os.fspath(db_path or _db.DB_PATH)
        if path == ":memory:":
            conn = sqlite3.connect(path)
            conn.executescript(v51_full_ddl)
            conn.commit()
            _db._configure_connection(conn, path)
            return conn
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            conn = sqlite3.connect(path)
            conn.executescript(v51_full_ddl)
            conn.commit()
            conn.close()
        return real(path)

    monkeypatch.setattr(_db, "get_connection", patched)
    yield


@pytest.fixture(autouse=True)
def _isolate_memory_db(tmp_path, monkeypatch):
    """Route the durable-memory store away from the live elixir-v5-memory.db.

    memory_store resolves ELIXIR_V5_MEMORY_DB per call and creates its schema
    on first use, so an empty tempfile is sufficient.
    """
    monkeypatch.setenv("ELIXIR_V5_MEMORY_DB", str(tmp_path / "memory-test.db"))
    yield


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
