"""Shared read-only connection for external reporting scripts.

Operator and quality reports must not migrate or otherwise mutate the live
database merely by inspecting it. Keep that guarantee at the SQLite connection
boundary rather than relying on each query to behave.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def connect_read_only() -> sqlite3.Connection:
    """Open the configured Elixir database with SQLite's ``mode=ro``."""
    load_dotenv(ROOT / ".env")
    path = Path(os.getenv("ELIXIR_DB_PATH", ROOT / "elixir-v51.db")).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
