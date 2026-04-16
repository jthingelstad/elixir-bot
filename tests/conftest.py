"""Shared test fixtures.

The autouse fixtures here protect the production database copy and the
Anthropic API from accidental test pollution:

- `_block_real_llm_calls` patches `agent.core._get_client` so any test that
  reaches the bottom-level API call raises a loud RuntimeError. Tests that
  correctly mock at the workflow layer (e.g.
  `elixir.elixir_agent.generate_channel_update`) never hit this — but a test
  that forgot to mock will fail with a clear pointer instead of silently
  burning tokens and inserting rows into `llm_calls`.

The fixture runs at session scope (applied once, cheaply) but uses
`autouse=True` so every test inherits it without explicit opt-in.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _block_real_llm_calls():
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
