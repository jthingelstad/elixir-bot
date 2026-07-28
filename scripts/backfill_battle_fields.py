#!/usr/bin/env python3
"""Rebuild every battle_events row from the raw payloads that produced it (#216).

Schema v17 widened `battle_events` from 17 of the ~30 facts a battle carries to
all of them. This replays the archive through the CURRENT extractor so history
gets the same record new battles do -- tower troop, elixir leaked, tower HP,
opponent identity, duel rounds, boat results.

Sources, in order of preference per battle:
  1. `raw_api_payloads` in the live DB  (~2 weeks, short retention by design)
  2. every `*.db.gz` in $ELIXIR_BACKUP_DIR, newest first -- each froze that
     same short window on its own date, so the union reaches months back

Rebuilding rather than patching field-by-field is the point: the extractor is
the single definition of what a battle row contains, so this cannot drift from
what ingest writes. Identity columns (dedup_key, war keys, observed_at) are
never touched -- only the battle facts.

Idempotent. Rows already complete are skipped unless --refresh.

Usage:
    uv run --locked python scripts/backfill_battle_fields.py            # dry run
    uv run --locked python scripts/backfill_battle_fields.py --apply
    ... [--refresh] [--backup-dir DIR] [--limit N]
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from engine.ingest import extract_battles  # noqa: E402
from engine.normalize import canon_tag  # noqa: E402

DEFAULT_BACKUP_DIR = Path.home() / "elixir-backups"

# Everything the extractor produces EXCEPT the identity/derived columns, which
# are owned by mirror_battles and must survive untouched.
_SKIP = {"player_tag", "battle_time", "opponent_tag", "battle_type"}

# A row missing this has not seen the v17 extractor. Chosen because the CR API
# populates it on 100% of battles, so NULL means "not backfilled", never "no
# such fact" -- unlike, say, tournament_tag.
_COMPLETENESS_PROBE = "elixir_leaked"


def _arg(flag: str, default: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _rows_from_payloads(conn, wanted: set[tuple[str, str]]) -> dict:
    """Replay stored battlelog payloads through the current extractor."""
    out: dict[tuple[str, str], dict] = {}
    try:
        cur = conn.execute(
            "SELECT payload_json FROM raw_api_payloads WHERE endpoint = 'player_battlelog'"
        )
    except sqlite3.DatabaseError:
        return out
    for (payload,) in cur:
        try:
            battles = json.loads(payload)
        except json.JSONDecodeError, TypeError, ValueError:
            continue
        if not isinstance(battles, list):
            continue
        # One payload is one player's log, but each battle names its whole team.
        # battle_events holds a row per polled player, so extract once per team
        # member -- that is what gives each row ITS OWN deck and tower HP.
        for battle in battles:
            if not isinstance(battle, dict):
                continue
            for member in battle.get("team") or []:
                if not isinstance(member, dict) or not member.get("tag"):
                    continue
                key = (canon_tag(member["tag"]), battle.get("battleTime"))
                if key not in wanted or key in out:
                    continue
                extracted = extract_battles(member["tag"], [battle])
                if extracted:
                    out[key] = extracted[0]
    return out


def _mine_backup(path: Path, wanted: set, stage: Path) -> dict:
    db_file = stage / "backup.db"
    try:
        with gzip.open(path, "rb") as src, open(db_file, "wb") as dst:
            shutil.copyfileobj(src, dst, length=8 << 20)
    except OSError, EOFError, gzip.BadGzipFile:
        return {}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        return _rows_from_payloads(conn, wanted)
    except sqlite3.DatabaseError:
        return {}
    finally:
        if conn is not None:
            conn.close()
        db_file.unlink(missing_ok=True)


def main() -> int:
    apply = "--apply" in sys.argv
    refresh = "--refresh" in sys.argv
    backup_dir = Path(_arg("--backup-dir", str(DEFAULT_BACKUP_DIR))).expanduser()
    limit = int(_arg("--limit", "0"))

    import db

    conn = db.get_connection()
    where = "" if refresh else f" WHERE {_COMPLETENESS_PROBE} IS NULL"
    wanted = {
        (r[0], r[1])
        for r in conn.execute(f"SELECT player_tag, battle_time FROM battle_events{where}")
    }
    total = conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0]
    print(f"battle_events rows: {total}   needing a rebuild: {len(wanted)}")
    if not wanted:
        print("nothing to do")
        return 0

    rebuilt = _rows_from_payloads(conn, wanted)
    print(f"  from live raw_api_payloads: {len(rebuilt)}")

    backups = sorted(backup_dir.glob("*.db.gz"), reverse=True)
    if limit:
        backups = backups[:limit]
    stage = Path(tempfile.mkdtemp(prefix="elixir-battle-rebuild-"))
    try:
        for i, path in enumerate(backups, 1):
            still = wanted - rebuilt.keys()
            if not still:
                print(f"  all rows rebuilt after {i - 1} backups — stopping early")
                break
            hits = _mine_backup(path, still, stage)
            rebuilt.update(hits)
            if hits:
                print(f"  [{i:3}/{len(backups)}] {path.name:42} +{len(hits):5} → {len(rebuilt)}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print(f"\nrebuilt {len(rebuilt)} of {len(wanted)} ({len(wanted) - len(rebuilt)} unrecoverable)")
    if not rebuilt:
        conn.rollback()
        conn.close()
        return 0

    columns = sorted(set(next(iter(rebuilt.values()))) - _SKIP)
    assignments = ", ".join(f"{c} = ?" for c in columns)
    print(f"columns rewritten per row: {len(columns)}")

    if not apply:
        conn.rollback()
        conn.close()
        print("dry run — pass --apply to write")
        return 0

    conn.executemany(
        f"UPDATE battle_events SET {assignments} WHERE player_tag = ? AND battle_time = ?",
        [[row.get(c) for c in columns] + [tag, bt] for (tag, bt), row in rebuilt.items()],
    )
    conn.commit()
    filled = conn.execute(f"SELECT COUNT({_COMPLETENESS_PROBE}) FROM battle_events").fetchone()[0]
    print(f"applied. rows carrying the full v17 record: {filled} of {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
