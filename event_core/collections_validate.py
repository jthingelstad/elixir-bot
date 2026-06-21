"""Self-contained build + parity harness for the PlayerCollections slice.

Standalone (does NOT edit application.py / build_foundation.py): defines a local
ObservedWorld subclass with the collections command, backfills the raw 'player'
archive into throwaway event + projection DBs, runs the current-collections
projection, then checks content parity against the frozen legacy snapshots.

Env (defaults are throwaway scratch DBs — never the shared elixir-v5*.db):
  COLL_EVENTS_DB  default /tmp/coll_events.db
  COLL_PROJ_DB    default /tmp/coll_proj.db
  ELIXIR_LEGACY_DB / config.LEGACY_DB  -> elixir.db.legacy (READ-ONLY oracle)

Run:  ./venv/bin/python -m event_core.collections_validate
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from event_core import config
from event_core.ingest.collections import ingest_player_collections

EVENTS_DB = os.environ.get("COLL_EVENTS_DB", "/tmp/coll_events.db")
PROJ_DB = os.environ.get("COLL_PROJ_DB", "/tmp/coll_proj.db")


# ---------------------------------------------------------------------------
# Application (local; mirrors event_core.application.ObservedWorld shape)
# ---------------------------------------------------------------------------
def _build_app():
    from eventsourcing.application import AggregateNotFoundError, Application

    from event_core.domain.collections import (
        PlayerCollections,
        canon_tag,
        collections_id,
    )

    class CollectionsWorld(Application):
        snapshotting_intervals = {PlayerCollections: 100}

        def _get_or_create(self, tag: str) -> PlayerCollections:
            try:
                return self.repository.get(collections_id(tag))
            except AggregateNotFoundError:
                return PlayerCollections(player_tag=tag)

        def observe_player_collections(
            self,
            player_tag: str,
            *,
            cards_json: str,
            support_cards_json: str,
            cards_hash: str,
            badges_json: str,
            badges_hash: str,
            achievements_json: str,
            achievements_hash: str,
            observed_at: str,
        ) -> dict:
            tag = canon_tag(player_tag)
            agg = self._get_or_create(tag)
            changed = {
                "cards": agg.observe_cards(
                    cards_json, support_cards_json, observed_at, cards_hash
                ),
                "badges": agg.observe_badges(badges_json, observed_at, badges_hash),
                "achievements": agg.observe_achievements(
                    achievements_json, observed_at, achievements_hash
                ),
            }
            if any(changed.values()):
                self.save(agg)
            return changed

    return CollectionsWorld()


# ---------------------------------------------------------------------------
# Backfill (idempotent via ingest cursor, mirrors event_core.backfill)
# ---------------------------------------------------------------------------
def _proj_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(PROJ_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projection_tracking ("
        "projection_name TEXT PRIMARY KEY, last_global_position INTEGER NOT NULL DEFAULT 0, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ingest_cursor ("
        "source TEXT PRIMARY KEY, last_payload_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT)"
    )
    conn.commit()
    return conn


def backfill_collections(app, legacy_path: str | None = None) -> dict:
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    cursor_conn = _proj_conn()
    try:
        row = cursor_conn.execute(
            "SELECT last_payload_id FROM ingest_cursor WHERE source=?", ("player_collections",)
        ).fetchone()
        start_id = row["last_payload_id"] if row else 0
        rows = legacy.execute(
            "SELECT payload_id, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='player' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()

        emitted = {"cards": 0, "badges": 0, "achievements": 0}
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            changed = ingest_player_collections(app, payload, r["fetched_at"])
            for k in emitted:
                emitted[k] += 1 if changed.get(k) else 0
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            cursor_conn.execute(
                "INSERT INTO ingest_cursor(source,last_payload_id,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_payload_id=excluded.last_payload_id, "
                "updated_at=excluded.updated_at",
                ("player_collections", max_id, datetime.now(timezone.utc).isoformat()),
            )
            cursor_conn.commit()
        return {"payloads": len(rows), "events_emitted": emitted}
    finally:
        legacy.close()
        cursor_conn.close()


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------
def _tag_key(tag: str) -> str:
    t = (tag or "").strip().upper()
    return t.lstrip("#")


def _parse(j):
    if j is None:
        return None
    try:
        return json.loads(j)
    except (json.JSONDecodeError, TypeError):
        return None


def _card_sort_key(c: dict):
    # cards/support arrays aren't guaranteed ordered identically across writes;
    # compare order-insensitively keyed by (id, name, evolutionLevel).
    return (
        c.get("id"),
        c.get("name"),
        c.get("evolutionLevel"),
        c.get("starLevel"),
    )


def _norm_list(items, key):
    if not isinstance(items, list):
        return items
    try:
        return sorted(items, key=key)
    except TypeError:
        return items


def _cards_equal(a, b) -> bool:
    return _norm_list(a, _card_sort_key) == _norm_list(b, _card_sort_key)


def _badge_sort_key(c: dict):
    return (c.get("name"), c.get("level"), c.get("progress"), c.get("target"))


def _ach_sort_key(c: dict):
    return (c.get("name"), c.get("stars"), c.get("value"), c.get("target"))


def _list_equal(a, b, key) -> bool:
    return _norm_list(a, key) == _norm_list(b, key)


def check_collections_parity(
    legacy_path: str | None = None, projections_path: str | None = None
) -> dict:
    legacy = sqlite3.connect(legacy_path or config.LEGACY_DB)
    legacy.row_factory = sqlite3.Row
    proj = sqlite3.connect(projections_path or config.PROJECTIONS_DB)
    proj.row_factory = sqlite3.Row

    try:
        archive_tags = {
            _tag_key(r["entity_key"])
            for r in legacy.execute(
                "SELECT DISTINCT entity_key FROM raw_api_payloads WHERE endpoint='player'"
            )
        }

        # latest member_card_collection_snapshots row per member
        legacy_cards: dict[str, sqlite3.Row] = {}
        for r in legacy.execute(
            "SELECT m.player_tag AS player_tag, mc.* FROM member_card_collection_snapshots mc "
            "JOIN members m ON m.member_id = mc.member_id"
        ):
            tk = _tag_key(r["player_tag"])
            prev = legacy_cards.get(tk)
            if prev is None or r["snapshot_id"] > prev["snapshot_id"]:
                legacy_cards[tk] = r

        # latest player_profile_snapshots row per member (badges + achievements)
        legacy_profile: dict[str, sqlite3.Row] = {}
        for r in legacy.execute(
            "SELECT m.player_tag AS player_tag, ps.* FROM player_profile_snapshots ps "
            "JOIN members m ON m.member_id = ps.member_id"
        ):
            tk = _tag_key(r["player_tag"])
            prev = legacy_profile.get(tk)
            if prev is None or r["snapshot_id"] > prev["snapshot_id"]:
                legacy_profile[tk] = r

        proj_rows = {
            _tag_key(r["player_tag"]): r
            for r in proj.execute("SELECT * FROM player_current_collections")
        }
    finally:
        legacy.close()
        proj.close()

    def run_one(legacy_map, proj_col, comparator, legacy_extra=None):
        matched, mismatches, missing_proj, outside, missing_legacy = [], [], [], [], []
        for tk in archive_tags:
            leg = legacy_map.get(tk)
            pr = proj_rows.get(tk)
            if leg is None:
                missing_legacy.append(tk)
                continue
            if pr is None:
                missing_proj.append(tk)
                continue
            ok = comparator(leg, pr)
            (matched if ok else mismatches).append(tk)
        # legacy members outside the raw 'player' archive horizon
        for tk in legacy_map:
            if tk not in archive_tags:
                outside.append(tk)
        return {
            "scoped_members": len(matched) + len(mismatches) + len(missing_proj),
            "matched": len(matched),
            "mismatched": len(mismatches),
            "missing_projection": len(missing_proj),
            "missing_legacy_in_archive_scope": len(missing_legacy),
            "outside_archive_horizon": len(outside),
            "mismatch_detail": mismatches[:25],
        }

    def cards_cmp(leg, pr):
        return _cards_equal(_parse(leg["cards_json"]), _parse(pr["cards_json"])) and _cards_equal(
            _parse(leg["support_cards_json"]), _parse(pr["support_cards_json"])
        )

    def badges_cmp(leg, pr):
        return _list_equal(_parse(leg["badges_json"]), _parse(pr["badges_json"]), _badge_sort_key)

    def ach_cmp(leg, pr):
        return _list_equal(
            _parse(leg["achievements_json"]), _parse(pr["achievements_json"]), _ach_sort_key
        )

    return {
        "cards": run_one(legacy_cards, "cards_json", cards_cmp),
        "badges": run_one(legacy_profile, "badges_json", badges_cmp),
        "achievements": run_one(legacy_profile, "achievements_json", ach_cmp),
    }


def build(clean: bool = True) -> dict:
    if clean:
        for path in (EVENTS_DB, PROJ_DB):
            for suffix in ("", "-wal", "-shm"):
                p = path + suffix
                if os.path.exists(p):
                    os.remove(p)

    config.configure_eventstore_env(EVENTS_DB)
    app = _build_app()
    bf = backfill_collections(app)

    conn = _proj_conn()
    from event_core.projections.collections import PlayerCurrentCollections

    proj = PlayerCurrentCollections(app, conn)
    proj.reset()
    applied = proj.run()
    conn.close()

    return {
        "backfill": bf,
        "projection_events_applied": applied,
        "parity": check_collections_parity(),
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, default=str))
