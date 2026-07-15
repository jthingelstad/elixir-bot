"""recent_member_spotlights: the per-member cooldown data the awareness brain
uses to avoid re-soloing the same members (24h-review milestone-firehose fix)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from runtime.awareness.read import _recent_member_spotlights


def _thought(conn, loop, at, posts):
    conn.execute(
        "INSERT INTO awareness_thoughts (thought_id, at, plan_json, chose_silence, "
        "post_count, loop_number) VALUES (?, ?, ?, 0, ?, ?)",
        (f"t{loop}", at, json.dumps({"posts": posts}), len(posts), loop),
    )
    conn.commit()


def test_spotlights_report_solo_and_newest_per_member(engine_conn, _isolate_default_sqlite_db):
    from runtime.awareness.store import ensure_awareness_schema
    ensure_awareness_schema(engine_conn)
    now = datetime.now(timezone.utc)
    older_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newer_at = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # older roundup (two members, solo=False)
    _thought(engine_conn, 1, older_at, [{
        "channel": "elixir", "leads_with": "milestone", "summary": "roundup",
        "member_tags": ["#A", "#B"], "member_names": ["Andy", "Bob"]}])
    # newer solo for Andy
    _thought(engine_conn, 2, newer_at, [{
        "channel": "elixir", "leads_with": "milestone", "summary": "Andy trophy peak",
        "member_tags": ["#A"], "member_names": ["Andy"]}])
    out = {s["member_tag"]: s for s in _recent_member_spotlights(engine_conn)}
    assert set(out) == {"#A", "#B"}
    # Andy's newest entry is the solo one
    assert out["#A"]["solo"] is True and out["#A"]["at"] == newer_at
    assert out["#B"]["solo"] is False


def test_spotlights_ignore_silence_and_non_milestone(engine_conn, _isolate_default_sqlite_db):
    from runtime.awareness.store import ensure_awareness_schema
    ensure_awareness_schema(engine_conn)
    # a war post is not a member spotlight
    _thought(engine_conn, 1, "2026-07-12T10:00:00Z", [{
        "channel": "elixir", "leads_with": "war", "summary": "race",
        "member_tags": [], "member_names": []}])
    # a silent tick contributes nothing
    engine_conn.execute(
        "INSERT INTO awareness_thoughts (thought_id, at, plan_json, chose_silence, "
        "post_count, loop_number) VALUES ('t2', '2026-07-12T10:30:00Z', '{}', 1, 0, 2)")
    engine_conn.commit()
    assert _recent_member_spotlights(engine_conn) == []
