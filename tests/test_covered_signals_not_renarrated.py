"""A signal another author already covered is not the brain's to narrate again.

Before the scoped responder existed this could not happen: the awareness brain
was the only composer, so anything past its cursor was by definition unsaid. A
responder that welcomes a join within minutes changes that — the join sits past
the brain's cursor until the next deliberation, hours later.

Identical re-posts already dedup on the delivery intent key, which is
hash(lane, sorted covers). The gap this closes is the post that BATCHES: a brain
tick covering the join AND a departure in one "roster update" hashes to a
different key, passes INSERT OR IGNORE, and welcomes the member a second time.
"""

from __future__ import annotations

import json

from runtime.awareness import read as read_mod

JOIN_KEY = "member_joined:#AAA:2026-08-04T12:00:00Z"


def _fulfilled_intent(conn, covers, *, lane="announcements", created_at="2026-08-04T12:05:00Z"):
    conn.execute(
        "INSERT INTO awareness_delivery_intents (intent_key, lane, content, covers_json, "
        "post_json, status, created_at, updated_at, fulfilled_at) "
        "VALUES (?, ?, 'Welcome.', ?, '{}', 'fulfilled', ?, ?, ?)",
        (
            f"awareness:{'-'.join(covers)}",
            lane,
            json.dumps(covers),
            created_at,
            created_at,
            created_at,
        ),
    )
    conn.commit()


def test_a_covered_signal_is_excluded(engine_conn, monkeypatch):
    monkeypatch.setattr(
        read_mod,
        "_now",
        lambda: __import__("datetime").datetime.fromisoformat("2026-08-04T12:30:00+00:00"),
    )
    _fulfilled_intent(engine_conn, [JOIN_KEY])
    assert JOIN_KEY in read_mod._covered_signal_keys(engine_conn)


def test_an_uncovered_signal_is_not_excluded(engine_conn, monkeypatch):
    monkeypatch.setattr(
        read_mod,
        "_now",
        lambda: __import__("datetime").datetime.fromisoformat("2026-08-04T12:30:00+00:00"),
    )
    _fulfilled_intent(engine_conn, ["some_other_key"])
    assert JOIN_KEY not in read_mod._covered_signal_keys(engine_conn)


def test_coverage_is_bounded_so_old_posts_do_not_suppress_forever(engine_conn, monkeypatch):
    """A signal older than the window is past the brain's cursor anyway, and an
    unbounded covers_json scan grows without limit."""
    monkeypatch.setattr(
        read_mod,
        "_now",
        lambda: __import__("datetime").datetime.fromisoformat("2026-08-06T12:30:00+00:00"),
    )
    _fulfilled_intent(engine_conn, [JOIN_KEY], created_at="2026-08-04T12:05:00Z")
    assert JOIN_KEY not in read_mod._covered_signal_keys(engine_conn)


def test_an_unreadable_outbox_does_not_suppress_anything(engine_conn, monkeypatch):
    """Fail OPEN. A lookup failure that silently emptied the read would drop
    hard-post signals — the floor would never even know they existed."""

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("outbox unreadable")

    assert read_mod._covered_signal_keys(_Boom()) == set()
