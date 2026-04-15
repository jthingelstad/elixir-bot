"""Test-suite-wide fixtures.

The Elixir runtime calls ``load_dotenv()`` on import (see ``runtime/app.py``)
so any flags set in the developer's ``.env`` would otherwise leak into tests.
This module forces the test environment to a deterministic baseline.
"""

import os

import pytest


# Env vars that gate runtime behavior and should default OFF in tests so
# tests reflect the legacy/default code path. Individual tests can opt in
# by patching the env back on.
_LEAKED_ENV_FLAGS = (
    "ELIXIR_AWARENESS_LOOP",
)


@pytest.fixture(autouse=True)
def _clear_leaked_env_flags(monkeypatch):
    for name in _LEAKED_ENV_FLAGS:
        monkeypatch.delenv(name, raising=False)
