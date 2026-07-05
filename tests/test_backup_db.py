"""Backup safety: the destination (often iCloud) must only ever receive a
complete .gz — never temp turds, even if interrupted (live bug 2026-07-05)."""
import gzip
import sqlite3

from scripts.backup_db import create_backup


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
