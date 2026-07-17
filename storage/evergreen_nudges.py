"""Evergreen housekeeping nudges — Discord, POAP KINGS FAQ, website.

Elixir surfaces ONE as an in-game-relay leader-action card during a quiet
period, infrequently and rotated, so these fill lulls instead of adding noise.
Inventory content and per-item send-state live together in `evergreen_nudges`.
See runtime/jobs/_core.py::_evergreen_nudge for the emitter.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

__all__ = [
    "ensure_schema",
    "is_quiet_period",
    "due_nudge",
    "mark_nudge_sent",
    "SEED_NUDGES",
]

_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Starter inventory (Jamie: Discord, FAQ, website). Clash chat strips links, so
# the copy names POAPKINGS.COM as plain text and points to the Members page;
# "Discord" is kept out of the invite copy (Clash filters the word).
SEED_NUDGES = [
    {
        "nudge_key": "discord_invite",
        "topic": "Discord invite",
        "cooldown_days": 30,
        "forbidden_terms": ["Discord", "http://", "https://", "www."],
        "context": (
            "Nudge clanmates to join our community hub for chat, deck tips, and clan "
            "news. Clash chat strips links, so tell them to visit POAPKINGS.COM and "
            "open the Members page to get connected. One short, warm line."
        ),
    },
    {
        "nudge_key": "poap_faq",
        "topic": "POAP KINGS FAQ",
        "cooldown_days": 30,
        "forbidden_terms": ["http://", "https://", "www."],
        "context": (
            "Point clanmates to the POAP KINGS FAQ for common questions about war, "
            "rewards, and how the clan runs. No links (Clash strips them) — just say "
            "it lives on POAPKINGS.COM. One friendly line."
        ),
    },
    {
        "nudge_key": "website_home",
        "topic": "POAPKINGS.COM",
        "cooldown_days": 45,
        "forbidden_terms": ["http://", "https://", "www."],
        "context": (
            "Remind clanmates that POAPKINGS.COM is the home base for clan info, "
            "stats, and everything about the clan. No raw links — name the site as "
            "POAPKINGS.COM. One casual line."
        ),
    },
]


def _now(now):
    return now or datetime.now(timezone.utc)


def _parse(ts):
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except TypeError, ValueError:
        return None


def ensure_schema(conn) -> None:
    """Validate the central schema, then seed the domain inventory."""
    from db.schema import require_columns

    require_columns(conn, "evergreen_nudges", {"nudge_key", "last_sent_at"})
    if not conn.execute("SELECT 1 FROM evergreen_nudges LIMIT 1").fetchone():
        now = _now(None).strftime(_ISO)
        for item in SEED_NUDGES:
            conn.execute(
                "INSERT OR IGNORE INTO evergreen_nudges (nudge_key, topic, context, "
                "forbidden_terms_json, cooldown_days, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (
                    item["nudge_key"],
                    item["topic"],
                    item["context"],
                    json.dumps(item.get("forbidden_terms") or []),
                    item.get("cooldown_days", 30),
                    now,
                ),
            )


def is_quiet_period(conn, now=None, *, quiet_days: int = 3) -> bool:
    """True when Elixir hasn't made a public post in `quiet_days`. The reliable
    signal is the latest awareness post (channel_state is not consistently
    updated; legacy communication intents are offline-only)."""
    row = conn.execute("SELECT MAX(posted_at) FROM awareness_posts").fetchone()
    last = row[0] if row else None
    if not last:
        return True
    last_dt = _parse(last)
    if last_dt is None:
        return False
    return (_now(now) - last_dt) >= timedelta(days=quiet_days)


def _has_pending_nudge_card(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM leader_action_recommendations "
            "WHERE source_signal_type = 'evergreen_nudge' AND status = 'proposed' LIMIT 1"
        ).fetchone()
        is not None
    )


def due_nudge(conn, now=None, *, global_cap_days: int = 7) -> dict | None:
    """The next nudge to emit, or None. Enforces the global rate cap (no nudge
    within `global_cap_days`), no stacking (skip if a proposed nudge card is
    already pending), and per-item cooldown; rotates oldest-sent-first
    (never-sent items first)."""
    now = _now(now)
    ensure_schema(conn)

    row = conn.execute("SELECT MAX(last_sent_at) FROM evergreen_nudges").fetchone()
    last_any = _parse(row[0]) if row and row[0] else None
    if last_any is not None and (now - last_any) < timedelta(days=global_cap_days):
        return None
    if _has_pending_nudge_card(conn):
        return None

    eligible = []
    for r in conn.execute("SELECT * FROM evergreen_nudges WHERE enabled = 1").fetchall():
        d = dict(r)
        ls = _parse(d.get("last_sent_at")) if d.get("last_sent_at") else None
        if ls is not None and (now - ls) < timedelta(days=d.get("cooldown_days") or 30):
            continue
        eligible.append(d)
    if not eligible:
        return None
    eligible.sort(key=lambda d: d.get("last_sent_at") or "")
    item = eligible[0]
    item["forbidden_terms"] = json.loads(item.get("forbidden_terms_json") or "[]")
    return item


def mark_nudge_sent(conn, nudge_key: str, now=None) -> None:
    conn.execute(
        "UPDATE evergreen_nudges SET last_sent_at = ?, send_count = send_count + 1 "
        "WHERE nudge_key = ?",
        (_now(now).strftime(_ISO), nudge_key),
    )
