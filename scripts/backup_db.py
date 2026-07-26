#!/usr/bin/env python3
"""Backup Elixir's operational database with compression and retention pruning.

The CLI entry point (used by the restart script) snapshots ``elixir-v51.db``.
Durable memory moved into that database in the v5.1 memory pass; the retired
``elixir-v5-memory.db`` archive is read-only and is not a runtime backup target.
Uses sqlite3.Connection.backup() for a safe online snapshot — no need to stop
the bot.

create_backup() / prune_backups() default to the operational DB; pass
`prefix=`/`db_path=` to target another store.

Retention tiers (weekly backup cadence assumed), applied per prefix:
  0-28 days   keep all snapshots
  29-90 days  keep one per month (first backup of each month)
  91-365 days keep one per quarter (first backup of each quarter)
  >365 days   delete

Environment variables
  ELIXIR_DB_PATH       operational database (default: <project>/elixir-v51.db)
  ELIXIR_BACKUP_DIR    destination dir      (default: ~/elixir-backups)
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("elixir_backup")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Run standalone, sys.path[0] is scripts/, so the project-root packages
# (event_core) aren't importable. Put the project root on the path so the v5
# store config resolves whether invoked as a script or imported as a module.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_DB = _PROJECT_ROOT / "elixir-v51.db"
_DEFAULT_BACKUP_DIR = Path.home() / "elixir-backups"

_TIMESTAMP_FMT = "%Y-%m-%d-%H%M%S"
_DEFAULT_PREFIX = "elixir"


def _filename_re(prefix: str) -> re.Pattern:
    """Match `<prefix>-<timestamp>.db.gz`. Anchored so prefixes don't collide:
    the literal `-` before the 4-digit year stops "elixir" matching
    "elixir-v5-…" files (and "elixir-v5" matching "elixir-v5-events-…")."""
    return re.compile(rf"^{re.escape(prefix)}-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}})\.db\.gz$")


def _databases() -> list[tuple[str, Path, bool]]:
    """(filename_prefix, source_path, required) for every DB the restart backup
    covers. v5.1 has one operational database; pre-cut stores are immutable
    archives and need no recurring backup."""
    return [("elixir-v51", _db_path(), True)]


# Retention thresholds in days.
_KEEP_ALL_DAYS = 28
_KEEP_MONTHLY_DAYS = 90
_KEEP_QUARTERLY_DAYS = 365


def _backup_dir() -> Path:
    return Path(os.getenv("ELIXIR_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR)))


def _db_path() -> Path:
    return Path(os.getenv("ELIXIR_DB_PATH", str(_DEFAULT_DB)))


def _timestamp_from_name(name: str, prefix: str = _DEFAULT_PREFIX) -> datetime | None:
    m = _filename_re(prefix).match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Core backup ──────────────────────────────────────────────────────────────


def create_backup(
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    prefix: str = _DEFAULT_PREFIX,
) -> dict:
    """Create a compressed backup of the database.

    `prefix` names the snapshot family (`<prefix>-<timestamp>.db.gz`) so each
    database is backed up and pruned independently in the shared backup dir.

    Returns a dict with keys: path, size_original, size_compressed, ok, error.
    """
    src = db_path or _db_path()
    dest_dir = backup_dir or _backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    filename = f"{prefix}-{now.strftime(_TIMESTAMP_FMT)}.db.gz"
    dest = dest_dir / filename

    result: dict = {
        "path": str(dest),
        "size_original": 0,
        "size_compressed": 0,
        "ok": False,
        "error": None,
    }

    try:
        # Stage EVERYTHING in a LOCAL temp dir, then atomically move the final
        # .gz into dest_dir. dest_dir is often iCloud/a network mount: writing
        # temps there (old bug) meant a hard restart mid-backup left 0-byte
        # tmp*.db turds in the backup folder AND a stale offsite copy (live
        # 2026-07-05). Local staging + os.replace means the destination only
        # ever sees a complete file, and any interruption strands temps locally
        # (auto-reaped), never in iCloud.
        stage = Path(tempfile.mkdtemp(prefix="elixir-backup-"))
        tmp_path = str(stage / "snapshot.db")
        stage_gz = stage / filename
        src_conn = sqlite3.connect(str(src))
        try:
            try:
                dst_conn = sqlite3.connect(tmp_path)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()

                result["size_original"] = os.path.getsize(tmp_path)

                # Integrity check on the backup copy.
                check_conn = sqlite3.connect(tmp_path)
                try:
                    check_result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if check_result != "ok":
                        result["error"] = f"integrity check failed: {check_result}"
                        return result
                finally:
                    check_conn.close()

                # Compress locally, then atomically publish to the backup dir.
                with (
                    open(tmp_path, "rb") as f_in,
                    gzip.open(stage_gz, "wb", compresslevel=6) as f_out,
                ):
                    while True:
                        chunk = f_in.read(1_048_576)  # 1 MB
                        if not chunk:
                            break
                        f_out.write(chunk)
                os.replace(stage_gz, dest)  # atomic move into (possibly iCloud) dest_dir

                result["size_compressed"] = os.path.getsize(dest)
                result["ok"] = True
            finally:
                # Reap the whole local staging dir (temps never touch dest_dir).
                shutil.rmtree(stage, ignore_errors=True)
        finally:
            src_conn.close()
    except Exception as exc:
        result["error"] = str(exc)
        # A failed backup that goes unnoticed is how the offsite copy went stale
        # for weeks — make it visible in the ledger, not just the return value.
        try:
            from storage.incidents import record_incident

            record_incident("backup.create", exc, context={"dest": str(dest)})
        except Exception:
            # Best-effort: the real failure is already in result["error"], but a
            # broken incident ledger shouldn't itself vanish without a trace.
            log.warning("backup incident recording failed", exc_info=True)
        # Clean up partial output on failure.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

    return result


# ── Retention pruning ────────────────────────────────────────────────────────


def _quarter(dt: datetime) -> tuple[int, int]:
    return dt.year, (dt.month - 1) // 3


def prune_backups(backup_dir: Path | None = None, prefix: str = _DEFAULT_PREFIX) -> list[str]:
    """Delete backups of one prefix family that exceed the retention policy.

    Only files matching `<prefix>-<timestamp>.db.gz` are considered, so each
    database's snapshots are pruned independently in the shared dir.

    Returns list of filenames that were removed.
    """
    dest_dir = backup_dir or _backup_dir()
    if not dest_dir.is_dir():
        return []

    now = datetime.now(timezone.utc)

    # Collect this prefix's backup files with their parsed timestamps.
    backups: list[tuple[Path, datetime]] = []
    for entry in dest_dir.iterdir():
        ts = _timestamp_from_name(entry.name, prefix)
        if ts is not None:
            backups.append((entry, ts))

    # Sort oldest first for stable keep-first-per-bucket logic.
    backups.sort(key=lambda pair: pair[1])

    removed: list[str] = []
    seen_months: set[tuple[int, int]] = set()
    seen_quarters: set[tuple[int, int]] = set()

    for path, ts in backups:
        age_days = (now - ts).days

        if age_days <= _KEEP_ALL_DAYS:
            # Keep everything in the recent window.
            continue

        if age_days <= _KEEP_MONTHLY_DAYS:
            # Keep one per month.
            bucket = (ts.year, ts.month)
            if bucket not in seen_months:
                seen_months.add(bucket)
                continue
            # Duplicate for this month — remove.
            path.unlink()
            removed.append(path.name)
            continue

        if age_days <= _KEEP_QUARTERLY_DAYS:
            # Keep one per quarter.
            bucket = _quarter(ts)
            if bucket not in seen_quarters:
                seen_quarters.add(bucket)
                continue
            path.unlink()
            removed.append(path.name)
            continue

        # Beyond max retention — remove.
        path.unlink()
        removed.append(path.name)

    return removed


# ── CLI entry point ──────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    failed = False
    for prefix, db_path, required in _databases():
        if not db_path.exists():
            if required:
                log.error("Database not found: %s", db_path)
                failed = True
            else:
                log.info("Skipping %s (not present): %s", prefix, db_path)
            continue

        log.info("Backing up %s ...", db_path)
        result = create_backup(db_path, prefix=prefix)

        if not result["ok"]:
            log.error("Backup failed for %s: %s", db_path, result["error"])
            failed = True
            continue

        ratio = (
            result["size_compressed"] / result["size_original"] * 100
            if result["size_original"]
            else 0
        )
        log.info(
            "Backup complete: %s (%.1f MB -> %.1f MB, %.0f%%)",
            result["path"],
            result["size_original"] / 1_048_576,
            result["size_compressed"] / 1_048_576,
            ratio,
        )

        removed = prune_backups(prefix=prefix)
        if removed:
            log.info(
                "Pruned %d old %s backup(s): %s",
                len(removed),
                prefix,
                ", ".join(removed),
            )

    if failed:
        log.error("One or more backups failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
