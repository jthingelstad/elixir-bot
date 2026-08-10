"""Backup safety: complete artifacts and one shared runtime backup set."""

import asyncio
import gzip
import json
import os
import sqlite3
import stat

import pytest

from scripts import backup_db
from scripts.backup_db import _databases, create_backup


def test_cli_help_exits_without_reaching_backup_or_pruning(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backup_db, "backup_all", lambda: calls.append("backup"))
    monkeypatch.setattr(backup_db, "prune_backups", lambda: calls.append("prune"))

    with pytest.raises(SystemExit) as stopped:
        backup_db.main(["--help"])

    assert stopped.value.code == 0
    assert "usage:" in capsys.readouterr().out
    assert calls == []


def test_cli_rejects_unknown_arguments_before_backup_or_pruning(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backup_db, "backup_all", lambda: calls.append("backup"))
    monkeypatch.setattr(backup_db, "prune_backups", lambda: calls.append("prune"))

    with pytest.raises(SystemExit) as stopped:
        backup_db.main(["--definitely-not-an-option"])

    assert stopped.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert calls == []


def test_cli_without_arguments_runs_the_full_backup_set(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backup_db,
        "backup_all",
        lambda: calls.append("backup") or {"ok": True, "results": []},
    )

    assert backup_db.main([]) == 0
    assert calls == ["backup"]


def test_backup_leaves_no_temp_files_in_dest(tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1),(2),(3)")
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    prior_umask = os.umask(0)
    try:
        r = create_backup(db_path=src, backup_dir=dest, prefix="test")
    finally:
        os.umask(prior_umask)
    assert r["ok"] and r["error"] is None

    files = sorted(p.name for p in dest.iterdir())
    assert len(files) == 1 and files[0].endswith(".db.gz"), files  # ONLY the .gz
    assert not any(f.startswith("tmp") or f.endswith(".db-journal") for f in files)
    assert stat.S_IMODE((dest / files[0]).stat().st_mode) == 0o600
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


def test_daily_activity_uses_shared_backup_set(monkeypatch):
    """The scheduler was the untested third caller and silently kept using the
    one-database primitive after restart and weekly maintenance were unified."""
    from runtime import app
    from scripts import backup_db

    backup_calls = []
    pruned = []
    successes = []
    failures = []
    outcome = {
        "ok": True,
        "results": [
            {"prefix": "elixir-v51", "ok": True, "path": "/backup/clan.db.gz"},
            {
                "prefix": "elixir-telemetry",
                "ok": True,
                "path": "/backup/telemetry.db.gz",
            },
        ],
    }

    monkeypatch.setattr(
        backup_db,
        "backup_all",
        lambda *, log_progress: backup_calls.append(log_progress) or outcome,
    )
    monkeypatch.setattr(
        backup_db, "create_backup", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError)
    )
    monkeypatch.setattr(backup_db, "prune_backups", lambda *, prefix: pruned.append(prefix) or [])
    monkeypatch.setattr(app.runtime_status, "mark_job_start", lambda name: None)
    monkeypatch.setattr(
        app.runtime_status,
        "mark_job_success",
        lambda name, summary: successes.append((name, json.loads(summary))),
    )
    monkeypatch.setattr(
        app.runtime_status,
        "mark_job_failure",
        lambda name, error: failures.append((name, error)),
    )

    result = asyncio.run(app._db_backup())

    assert backup_calls == [False]
    assert pruned == ["elixir-v5-memory"]
    assert failures == []
    assert successes[0][0] == "db_backup"
    assert set(successes[0][1]["databases"]) == {"elixir-v51", "elixir-telemetry"}
    assert result == successes[0][1]


def test_daily_activity_reports_partial_backup_as_failure(monkeypatch):
    from runtime import app
    from scripts import backup_db

    successes = []
    failures = []
    outcome = {
        "ok": False,
        "results": [
            {"prefix": "elixir-v51", "ok": True, "path": "/backup/clan.db.gz"},
            {"prefix": "elixir-telemetry", "ok": False, "error": "disk full"},
        ],
    }

    monkeypatch.setattr(backup_db, "backup_all", lambda *, log_progress: outcome)
    monkeypatch.setattr(
        backup_db,
        "prune_backups",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not prune after failure")),
    )
    monkeypatch.setattr(app.runtime_status, "mark_job_start", lambda name: None)
    monkeypatch.setattr(
        app.runtime_status,
        "mark_job_success",
        lambda name, summary: successes.append((name, summary)),
    )
    monkeypatch.setattr(
        app.runtime_status,
        "mark_job_failure",
        lambda name, error: failures.append((name, json.loads(error))),
    )

    result = asyncio.run(app._db_backup())

    assert successes == []
    assert failures[0][0] == "db_backup"
    assert failures[0][1]["databases"]["elixir-telemetry"]["error"] == "disk full"
    assert result == failures[0][1]
