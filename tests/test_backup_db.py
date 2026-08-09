"""Backup safety: the destination (often iCloud) must only ever receive a
complete .gz — never temp turds, even if interrupted (live bug 2026-07-05)."""

import gzip
import sqlite3

from scripts.backup_db import _databases, create_backup


def test_backup_leaves_no_temp_files_in_dest(tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1),(2),(3)")
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    r = create_backup(db_path=src, backup_dir=dest, prefix="test")
    assert r["ok"] and r["error"] is None

    files = sorted(p.name for p in dest.iterdir())
    assert len(files) == 1 and files[0].endswith(".db.gz"), files  # ONLY the .gz
    assert not any(f.startswith("tmp") or f.endswith(".db-journal") for f in files)
    # and it's a real gzip of a real sqlite backup
    with gzip.open(dest / files[0], "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")


def test_backup_covers_the_operational_and_telemetry_databases(tmp_path, monkeypatch):
    """Telemetry was uncovered until 2026-08-09 — AGENTS.md called it "a known
    gap, not a design decision". It is optional rather than required: a fresh
    install legitimately has none, and Elixir's behaviour may never depend on
    it. But losing it costs the entire cost and model-call history."""
    operational = tmp_path / "elixir-v51.db"
    telemetry = tmp_path / "elixir-telemetry.db"
    monkeypatch.setenv("ELIXIR_DB_PATH", str(operational))
    monkeypatch.setenv("ELIXIR_TELEMETRY_DB_PATH", str(telemetry))
    # The retired setting may survive in an old shell; it must not resurrect a
    # second required runtime database or block restart.
    monkeypatch.setenv("ELIXIR_V5_MEMORY_DB", str(tmp_path / "missing-memory.db"))

    assert _databases() == [
        ("elixir-v51", operational, True),
        ("elixir-telemetry", telemetry, False),
    ]


def test_a_missing_telemetry_database_does_not_fail_the_backup(tmp_path, monkeypatch):
    """Optional means optional: a restart must not fail because an admin-only
    file is absent."""
    from scripts.backup_db import backup_all

    operational = tmp_path / "elixir-v51.db"
    sqlite3.connect(operational).close()
    monkeypatch.setenv("ELIXIR_DB_PATH", str(operational))
    monkeypatch.setenv("ELIXIR_TELEMETRY_DB_PATH", str(tmp_path / "absent.db"))
    monkeypatch.setenv("ELIXIR_BACKUP_DIR", str(tmp_path / "backups"))

    outcome = backup_all(log_progress=False)
    assert outcome["ok"]
    assert [r["prefix"] for r in outcome["results"]] == ["elixir-v51"]


def test_both_databases_are_snapshotted_when_present(tmp_path, monkeypatch):
    """The weekly job and the restart path go through the same function, so they
    cannot diverge on which databases they cover — they did before, and the job
    silently backed up only the clan DB."""
    from scripts.backup_db import backup_all

    operational = tmp_path / "elixir-v51.db"
    telemetry = tmp_path / "elixir-telemetry.db"
    for path in (operational, telemetry):
        sqlite3.connect(path).close()
    monkeypatch.setenv("ELIXIR_DB_PATH", str(operational))
    monkeypatch.setenv("ELIXIR_TELEMETRY_DB_PATH", str(telemetry))
    monkeypatch.setenv("ELIXIR_BACKUP_DIR", str(tmp_path / "backups"))

    outcome = backup_all(log_progress=False)
    assert outcome["ok"]
    assert sorted(r["prefix"] for r in outcome["results"]) == [
        "elixir-telemetry",
        "elixir-v51",
    ]
    written = sorted(p.name.split("-2")[0] for p in (tmp_path / "backups").glob("*.db.gz"))
    assert written == ["elixir", "elixir"] or len(written) == 2
