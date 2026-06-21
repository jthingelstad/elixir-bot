"""Tests for the multi-database backup script.

Covers the v5 extension: every database is snapshotted under its own filename
prefix, and retention pruning is isolated per prefix so one family's pruning can
never delete another's snapshots.
"""
from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

from scripts import backup_db


def _make_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t(v) VALUES ('hello')")
    conn.commit()
    conn.close()


def test_create_backup_uses_prefix_and_is_restorable(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    dest = tmp_path / "backups"

    result = backup_db.create_backup(src, backup_dir=dest, prefix="elixir-v5-events")

    assert result["ok"], result["error"]
    out = list(dest.glob("elixir-v5-events-*.db.gz"))
    assert len(out) == 1
    # the snapshot decompresses to a valid sqlite db with our row
    restored = tmp_path / "restored.db"
    with gzip.open(out[0], "rb") as f_in, open(restored, "wb") as f_out:
        f_out.write(f_in.read())
    conn = sqlite3.connect(str(restored))
    assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello"
    conn.close()


def test_prefix_matching_does_not_collide():
    # "elixir" must not match the v5 families, and "elixir-v5" must not match
    # the more-specific events/memory families.
    ts = "elixir-2026-06-21-120000.db.gz"
    v5 = "elixir-v5-2026-06-21-120000.db.gz"
    v5_events = "elixir-v5-events-2026-06-21-120000.db.gz"

    assert backup_db._timestamp_from_name(ts, "elixir") is not None
    assert backup_db._timestamp_from_name(v5, "elixir") is None
    assert backup_db._timestamp_from_name(v5_events, "elixir") is None
    assert backup_db._timestamp_from_name(v5, "elixir-v5") is not None
    assert backup_db._timestamp_from_name(v5_events, "elixir-v5") is None
    assert backup_db._timestamp_from_name(v5_events, "elixir-v5-events") is not None


def test_prune_is_isolated_per_prefix(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    # An ancient (>365d) snapshot for each family — all prune-eligible if matched.
    old = datetime.now(timezone.utc) - timedelta(days=400)
    stamp = old.strftime(backup_db._TIMESTAMP_FMT)
    for prefix in ("elixir", "elixir-v5", "elixir-v5-events", "elixir-v5-memory"):
        (dest / f"{prefix}-{stamp}.db.gz").write_bytes(b"x")

    removed = backup_db.prune_backups(backup_dir=dest, prefix="elixir")

    # Only the legacy family's ancient snapshot is pruned; v5 families untouched.
    assert removed == [f"elixir-{stamp}.db.gz"]
    survivors = {p.name for p in dest.iterdir()}
    assert f"elixir-v5-{stamp}.db.gz" in survivors
    assert f"elixir-v5-events-{stamp}.db.gz" in survivors
    assert f"elixir-v5-memory-{stamp}.db.gz" in survivors


def test_databases_registry_includes_v5_stores():
    from event_core import config

    # conftest already points config at temp paths; assert the registry wires
    # all three v5 stores plus the required operational DB.
    prefixes = {prefix for prefix, _path, _required in backup_db._databases()}
    assert prefixes == {"elixir", "elixir-v5-events", "elixir-v5", "elixir-v5-memory"}

    required = {prefix for prefix, _p, req in backup_db._databases() if req}
    assert required == {"elixir"}  # v5 stores are optional (skip if absent)

    paths = {prefix: path for prefix, path, _ in backup_db._databases()}
    assert str(paths["elixir-v5-events"]) == config.EVENTS_DB
    assert str(paths["elixir-v5"]) == config.PROJECTIONS_DB
    assert str(paths["elixir-v5-memory"]) == config.MEMORY_DB
