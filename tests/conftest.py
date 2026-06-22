"""Shared test fixtures.

The autouse fixtures here protect the production database copy and the
Anthropic API from accidental test pollution:

- `_isolate_default_sqlite_db` routes implicit `db.get_connection()` calls to a
  per-test tempfile-backed SQLite database. Tests that pass an explicit
  connection or database path keep full control of their storage.
- `_block_real_llm_calls` patches `agent.core._get_client` so any test that
  reaches the bottom-level API call raises a loud RuntimeError. Tests that
  correctly mock at the workflow layer (e.g.
  `elixir.elixir_agent.generate_channel_update`) never hit this — but a test
  that forgot to mock will fail with a clear pointer instead of silently
  burning tokens and inserting rows into `llm_calls`.

Both fixtures use `autouse=True` so every test inherits the guardrails without
explicit opt-in.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_sqlite_db(tmp_path, monkeypatch):
    """Route implicit DB access away from the production SQLite file."""
    db_path = str(tmp_path / "elixir-test.db")
    monkeypatch.setenv("ELIXIR_DB_PATH", db_path)

    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", db_path)
    yield


@pytest.fixture(autouse=True)
def _neutralize_runtime_feature_flags(monkeypatch):
    """Production runtime toggles set in .env (loaded via runtime.app's load_dotenv)
    leak into the test process and would silently flip behavioral defaults. Scrub
    them so tests exercise the code default; a test that wants a specific value
    patches the flag explicitly."""
    monkeypatch.delenv("PLAYER_INTEL_DELIVERY", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_v5_event_stores(tmp_path, monkeypatch):
    """Route the v5 event-sourcing stores to per-test tempfiles.

    Without this, any test that rebuilds the foundation (e.g.
    build_foundation.build(), which defaults to clean=True and *deletes*
    config.EVENTS_DB/PROJECTIONS_DB) runs against the LIVE production v5 stores
    — wiping the running bot's event store and corrupting its projections. The
    frozen LEGACY_DB is a read-only parity oracle and is intentionally NOT
    redirected. Tests that pass explicit DB paths are unaffected.
    """
    from event_core import config

    events = str(tmp_path / "v5-events.db")
    projections = str(tmp_path / "v5.db")
    memory = str(tmp_path / "v5-memory.db")
    monkeypatch.setenv("ELIXIR_V5_EVENTS_DB", events)
    monkeypatch.setenv("ELIXIR_V5_DB", projections)
    monkeypatch.setenv("ELIXIR_V5_MEMORY_DB", memory)
    monkeypatch.setattr(config, "EVENTS_DB", events)
    monkeypatch.setattr(config, "PROJECTIONS_DB", projections)
    monkeypatch.setattr(config, "MEMORY_DB", memory)
    yield


@pytest.fixture(autouse=True)
def _block_real_llm_calls(_isolate_default_sqlite_db):
    """Fail loudly if any test reaches the Anthropic API."""
    def _boom(*args, **kwargs):
        raise RuntimeError(
            "Unmocked LLM call in test. Mock at the workflow layer "
            "(e.g. patch('elixir.elixir_agent.generate_channel_update')) "
            "or patch agent.memory_tasks.extract_inference_facts / "
            "runtime.jobs._signals._post_signal_memory before the code path "
            "reaches agent.core._create_chat_completion."
        )

    fake_client = MagicMock()
    fake_client.messages.create = _boom
    # db.record_llm_call / db.record_prompt_failure are re-exported from
    # storage.messages at module-load time, so patching the storage module
    # alone doesn't reach the agent.core / runtime.app sites that actually
    # call them. Patch the db module attributes directly. We use a pass-
    # through: if the test provides an explicit conn (to verify behavior in
    # isolation), the real function runs; otherwise the call is a no-op so
    # we don't pollute the default (production) DB.
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
