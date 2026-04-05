#!/usr/bin/env python3
"""Backup elixir.db with compression and tiered retention pruning.

Can be called standalone (manual / cron) or imported by the bot's
db-maintenance job.  Uses sqlite3.Connection.backup() for a safe
online snapshot — no need to stop the bot.

Retention tiers (weekly backup cadence assumed):
  0-28 days   keep all snapshots
  29-90 days  keep one per month (first backup of each month)
  91-365 days keep one per quarter (first backup of each quarter)
  >365 days   delete

Environment variables
  ELIXIR_DB_PATH    source database  (default: <project>/elixir.db)
  ELIXIR_BACKUP_DIR destination dir  (default: ~/elixir-backups)
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("elixir_backup")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "elixir.db"
_DEFAULT_BACKUP_DIR = Path.home() / "elixir-backups"

_FILENAME_RE = re.compile(r"^elixir-(\d{4}-\d{2}-\d{2}-\d{6})\.db\.gz$")
_TIMESTAMP_FMT = "%Y-%m-%d-%H%M%S"

# Retention thresholds in days.
_KEEP_ALL_DAYS = 28
_KEEP_MONTHLY_DAYS = 90
_KEEP_QUARTERLY_DAYS = 365


def _backup_dir() -> Path:
    return Path(os.getenv("ELIXIR_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR)))


def _db_path() -> Path:
    return Path(os.getenv("ELIXIR_DB_PATH", str(_DEFAULT_DB)))


def _timestamp_from_name(name: str) -> datetime | None:
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Core backup ──────────────────────────────────────────────────────────────


def create_backup(db_path: Path | None = None, backup_dir: Path | None = None) -> dict:
    """Create a compressed backup of the database.

    Returns a dict with keys: path, size_original, size_compressed, ok, error.
    """
    src = db_path or _db_path()
    dest_dir = backup_dir or _backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    filename = f"elixir-{now.strftime(_TIMESTAMP_FMT)}.db.gz"
    dest = dest_dir / filename

    result: dict = {"path": str(dest), "size_original": 0, "size_compressed": 0, "ok": False, "error": None}

    try:
        # Online backup into a temp file, then compress.
        src_conn = sqlite3.connect(str(src))
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=str(dest_dir))
            os.close(tmp_fd)
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

                # Compress.
                with open(tmp_path, "rb") as f_in, gzip.open(dest, "wb", compresslevel=6) as f_out:
                    while True:
                        chunk = f_in.read(1_048_576)  # 1 MB
                        if not chunk:
                            break
                        f_out.write(chunk)

                result["size_compressed"] = os.path.getsize(dest)
                result["ok"] = True
            finally:
                # Always clean up the uncompressed temp file.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        finally:
            src_conn.close()
    except Exception as exc:
        result["error"] = str(exc)
        # Clean up partial output on failure.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

    return result


# ── Retention pruning ────────────────────────────────────────────────────────


def _quarter(dt: datetime) -> tuple[int, int]:
    return dt.year, (dt.month - 1) // 3


def prune_backups(backup_dir: Path | None = None) -> list[str]:
    """Delete backups that exceed the retention policy.

    Returns list of filenames that were removed.
    """
    dest_dir = backup_dir or _backup_dir()
    if not dest_dir.is_dir():
        return []

    now = datetime.now(timezone.utc)

    # Collect all backup files with their parsed timestamps.
    backups: list[tuple[Path, datetime]] = []
    for entry in dest_dir.iterdir():
        ts = _timestamp_from_name(entry.name)
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

    db_path = _db_path()
    if not db_path.exists():
        log.error("Database not found: %s", db_path)
        return 1

    log.info("Backing up %s ...", db_path)
    result = create_backup(db_path)

    if not result["ok"]:
        log.error("Backup failed: %s", result["error"])
        return 1

    ratio = result["size_compressed"] / result["size_original"] * 100 if result["size_original"] else 0
    log.info(
        "Backup complete: %s (%.1f MB -> %.1f MB, %.0f%%)",
        result["path"],
        result["size_original"] / 1_048_576,
        result["size_compressed"] / 1_048_576,
        ratio,
    )

    removed = prune_backups()
    if removed:
        log.info("Pruned %d old backup(s): %s", len(removed), ", ".join(removed))
    else:
        log.info("No old backups to prune.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
