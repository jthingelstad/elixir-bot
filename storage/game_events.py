"""storage.game_events — the game-level stream.

A durable record of changes to Clash Royale itself (new cards, new live events,
new event badges), curated and announced clan-wide to #announcements. It mirrors
the player/clan/war event tables but is keyed on `change_key` — the identity of
the real-world change — so one change that surfaces as several detections
becomes one post. `backfilled=1` marks reconstructed history that is recorded
for the record and Elixir's voice but is never announced.

Schema lives in db.schema; this module validates the game-stream contract.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

__all__ = [
    "ensure_schema", "insert_game_event", "new_events_since", "recent_game_events",
    "EVENT_TYPES",
]

_ISO = "%Y-%m-%dT%H:%M:%SZ"

# The stream's vocabulary (schema-free facts live in payload_json):
#   card_added         — a card new to the /cards catalog (image: icon_url)
#   event_started      — a new live event/game-mode from /events
#   event_badge_earned — a genuinely-new non-mastery badge, attributed to the
#                        first member seen wearing it (image: iconUrls.large)
EVENT_TYPES = ("card_added", "event_started", "event_badge_earned")

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime(_ISO)


def ensure_schema(conn) -> None:
    """Compatibility assertion; db.schema owns creation and backfill."""
    from db.schema import require_columns

    require_columns(conn, "game_events", {"event_id", "dedup_key", "change_key"})
    require_columns(conn, "card_catalog", {"first_seen_at"})


def insert_game_event(conn, *, dedup_key: str, event_type: str, change_key: str,
                      observed_at: str, payload: dict, subject_tag: str | None = None,
                      scope: str = "public", backfilled: bool = False) -> int:
    """INSERT OR IGNORE one game_events row. Returns rows inserted (0 on dedup).
    The caller owns the transaction (mirrors insert_stream_event)."""
    ensure_schema(conn)
    cur = conn.execute(
        """INSERT OR IGNORE INTO game_events
               (dedup_key, event_type, change_key, subject_tag, observed_at,
                payload_json, scope, backfilled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dedup_key, event_type, change_key, subject_tag, observed_at,
         json.dumps(payload, separators=(",", ":"), sort_keys=True),
         scope, 1 if backfilled else 0, _utcnow()),
    )
    return cur.rowcount


def new_events_since(conn, after_event_id: int) -> list:
    """Announceable game_events past the recognizer's cursor, oldest first.
    Backfilled rows are the record only — never returned for announcement."""
    ensure_schema(conn)
    return conn.execute(
        """SELECT * FROM game_events
           WHERE event_id > ? AND backfilled = 0
           ORDER BY event_id""",
        (after_event_id,),
    ).fetchall()


def recent_game_events(conn, *, days: int = 7, now: str | None = None) -> list[dict]:
    """Game-stream changes (new cards/events/badges) observed in the last `days`,
    newest first, payload parsed — the clan-wide 'what changed in CR' context for a
    member report (e.g. Ronin's arrival). Live rows only; backfilled history excluded."""
    ensure_schema(conn)
    anchor = (datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)
              if now else datetime.now(timezone.utc))
    cutoff = (anchor - timedelta(days=days)).strftime(_ISO)
    out: list[dict] = []
    for r in conn.execute(
        "SELECT event_type, change_key, subject_tag, observed_at, payload_json "
        "FROM game_events WHERE backfilled = 0 AND observed_at >= ? "
        "ORDER BY observed_at DESC",
        (cutoff,),
    ).fetchall():
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out
