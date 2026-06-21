#!/usr/bin/env python3
"""Backup the Elixir databases with compression and tiered retention pruning.

The CLI entry point (used by the restart script) snapshots EVERY database: the
operational elixir.db plus the three v5 event-sourcing stores (events,
projections, memory). Each database gets its own filename prefix so retention is
tracked independently. Uses sqlite3.Connection.backup() for a safe online
snapshot — no need to stop the bot.

create_backup() / prune_backups() default to the legacy elixir.db (prefix
"elixir") so existing callers — e.g. the bot's db-maintenance job — are
unchanged; pass `prefix=`/`db_path=` to target another store.

Retention tiers (weekly backup cadence assumed), applied per prefix:
  0-28 days   keep all snapshots
  29-90 days  keep one per month (first backup of each month)
  91-365 days keep one per quarter (first backup of each quarter)
  >365 days   delete

Environment variables
  ELIXIR_DB_PATH    operational database (default: <project>/elixir.db)
  ELIXIR_BACKUP_DIR destination dir      (default: ~/elixir-backups)
  v5 store paths come from event_core.config (ELIXIR_V5_* env vars).
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
# Run standalone, sys.path[0] is scripts/, so the project-root packages
# (event_core) aren't importable. Put the project root on the path so the v5
# store config resolves whether invoked as a script or imported as a module.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_DB = _PROJECT_ROOT / "elixir.db"
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
    covers: the operational elixir.db (required) plus the three v5 stores
    (optional — absent on a fresh machine, which must not block a restart)."""
    dbs: list[tuple[str, Path, bool]] = [(_DEFAULT_PREFIX, _db_path(), True)]
    try:
        from event_core import config

        dbs += [
            ("elixir-v5-events", Path(config.EVENTS_DB), False),
            ("elixir-v5", Path(config.PROJECTIONS_DB), False),
            ("elixir-v5-memory", Path(config.MEMORY_DB), False),
        ]
    except Exception as exc:  # pragma: no cover - defensive: config import failure
        log.warning("v5 config unavailable; backing up operational DB only: %s", exc)
    return dbs

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

        ratio = result["size_compressed"] / result["size_original"] * 100 if result["size_original"] else 0
        log.info(
            "Backup complete: %s (%.1f MB -> %.1f MB, %.0f%%)",
            result["path"],
            result["size_original"] / 1_048_576,
            result["size_compressed"] / 1_048_576,
            ratio,
        )

        removed = prune_backups(prefix=prefix)
        if removed:
            log.info("Pruned %d old %s backup(s): %s", len(removed), prefix, ", ".join(removed))

    if failed:
        log.error("One or more backups failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
