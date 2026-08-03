"""Tests for the join trigger (runtime/awareness/trigger.py).

A newcomer is the one hard-post where minutes matter, so a `member_joined` past
the awareness cursor wakes the brain out of band instead of waiting up to 6h for
the next cron. These tests pin the four properties that make that safe to run:

1. it fires only when a join is genuinely unwelcomed (past the event cursor),
2. it fires ONCE per join even when the resulting awareness run fails — the
   cost guard, without which a policy-refused join re-runs Sonnet every 10 min,
3. a genuinely new join still fires after an earlier one was marked,
4. it stands down when the scheduled run is already imminent.
"""

from datetime import datetime, timedelta, timezone

import pytest

import elixir  # noqa: F401  (full runtime init before awareness internals)
from runtime.awareness import store as awareness_store
from runtime.awareness import trigger


def _seed_join(conn, *, tag="#NEW1", name="Newcomer", observed_at="2026-08-03T04:00:00Z"):
    """Insert a member_joined clan event and return its event_id."""
    cur = conn.execute(
        "INSERT INTO clan_events (event_type, clan_tag, subject_tag, payload_json, "
        "observed_at, timing, scope, created_at, dedup_key) "
        "VALUES ('member_joined', '#J2RGCRVG', ?, ?, ?, 'estimated', 'public', ?, ?)",
        (
            tag,
            f'{{"name": "{name}"}}',
            observed_at,
            observed_at,
            f"member_joined:{tag}:{observed_at}",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture()
def conn(engine_conn):
    awareness_store.ensure_awareness_schema(engine_conn)
    awareness_store.ensure_event_cursors(engine_conn)
    engine_conn.commit()
    return engine_conn


def test_no_pending_joins_does_not_fire(conn):
    decision = trigger.evaluate(conn=conn)
    assert decision["run"] is False
    assert decision["joins"] == []


def test_unwelcomed_join_fires(conn):
    event_id = _seed_join(conn)
    decision = trigger.evaluate(conn=conn)
    assert decision["run"] is True
    assert decision["high_water"] == event_id
    assert [j["subject_tag"] for j in decision["joins"]] == ["#NEW1"]


def test_a_join_the_brain_already_consumed_does_not_fire(conn):
    """The cursor, not the clock, decides. A join past the awareness clan cursor
    is unwelcomed; one behind it has already been narrated."""
    event_id = _seed_join(conn)
    awareness_store.advance_event_cursors({"clan": event_id}, conn=conn)
    conn.commit()

    decision = trigger.evaluate(conn=conn)
    assert decision["run"] is False
    assert decision["joins"] == []


def test_fires_once_per_join_even_when_the_awareness_run_fails(conn):
    """The cost guard. A failed awareness tick does NOT advance the event cursor,
    so the join stays pending forever — without the trigger's own high-water mark
    that would re-fire a full Sonnet run every engine tick, indefinitely."""
    _seed_join(conn)

    first = trigger.evaluate(conn=conn)
    assert first["run"] is True
    trigger.mark_fired(first["high_water"], conn=conn)
    conn.commit()

    # The run failed: the event cursor is untouched, the join is still pending.
    assert trigger.pending_joins(conn), "join should still be unconsumed"

    second = trigger.evaluate(conn=conn)
    assert second["run"] is False
    assert "already fired" in second["reason"]


def test_a_later_join_still_fires_after_an_earlier_one_was_marked(conn):
    """The high-water mark must bound retries, not deafen the trigger."""
    first_id = _seed_join(conn, tag="#NEW1", observed_at="2026-08-03T04:00:00Z")
    trigger.mark_fired(first_id, conn=conn)
    conn.commit()

    second_id = _seed_join(conn, tag="#NEW2", observed_at="2026-08-03T05:00:00Z")
    decision = trigger.evaluate(conn=conn)
    assert decision["run"] is True
    assert decision["high_water"] == second_id
    assert [j["subject_tag"] for j in decision["joins"]] == ["#NEW2"]


def test_stands_down_when_the_scheduled_run_is_imminent(conn):
    """No point spending an out-of-band Sonnet run to save four minutes."""
    _seed_join(conn)
    now = datetime(2026, 8, 3, 4, 1, tzinfo=timezone.utc)

    decision = trigger.evaluate(conn=conn, now=now, next_scheduled_at=now + timedelta(minutes=4))
    assert decision["run"] is False
    assert "scheduled run" in decision["reason"]

    # ...but a distant scheduled run must not suppress it.
    decision = trigger.evaluate(conn=conn, now=now, next_scheduled_at=now + timedelta(hours=5))
    assert decision["run"] is True


def test_standing_down_for_an_imminent_run_does_not_burn_the_join(conn):
    """Holding is not firing: if the scheduled run somehow misses the join, the
    next engine tick must still be able to fire for it."""
    now = datetime(2026, 8, 3, 4, 1, tzinfo=timezone.utc)
    _seed_join(conn)

    held = trigger.evaluate(conn=conn, now=now, next_scheduled_at=now + timedelta(minutes=4))
    assert held["run"] is False

    later = trigger.evaluate(conn=conn, now=now + timedelta(minutes=10))
    assert later["run"] is True


def test_env_flag_disables_the_trigger(conn, monkeypatch):
    _seed_join(conn)
    monkeypatch.setenv("ELIXIR_JOIN_TRIGGER", "0")
    decision = trigger.evaluate(conn=conn)
    assert decision["run"] is False
    assert decision["reason"] == "disabled"


def test_trigger_cursor_is_not_an_awareness_event_stream(conn):
    """The trigger's high-water mark shares stream_cursors with the awareness
    event cursors. advance_event_cursors must never move it — if it did, a normal
    awareness tick would silently mark joins as already-triggered."""
    trigger.mark_fired(5, conn=conn)
    awareness_store.advance_event_cursors({trigger.TRIGGER_CURSOR_KEY: 999}, conn=conn)
    conn.commit()

    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ?",
        (trigger.TRIGGER_CURSOR_KEY,),
    ).fetchone()
    assert row["cursor_int"] == 5


# ---------------------------------------------------------------------------
# App wiring: _maybe_trigger_awareness_for_joins
# ---------------------------------------------------------------------------


def test_trigger_marks_high_water_even_when_the_run_raises(conn, monkeypatch):
    """The cost guard again, this time through the app path. If the awareness run
    blows up, the finally must still mark — otherwise the next engine tick fires
    another Sonnet run for the same newcomer, and so does the one after that."""
    import asyncio

    from runtime import app

    _seed_join(conn)
    marked = []

    async def _boom(*, trigger=None):
        raise RuntimeError("brain exploded")

    monkeypatch.setattr(app, "_awareness_loop", _boom)
    monkeypatch.setattr(trigger, "mark_fired", lambda hw, **kw: marked.append(hw))
    assert not app.scheduler.running, "test scheduler must be idle (no next_run_time)"

    async def _go():
        result = await app._maybe_trigger_awareness_for_joins()
        # The run is a background task; let it finish.
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_go())
    assert result is not None and result["joins"] == ["#NEW1"]
    assert marked == [trigger.evaluate(conn=conn)["high_water"]]


def test_an_in_flight_run_is_not_doubled(conn):
    """Two concurrent awareness loops would build their reads from the same
    unadvanced cursor and both post the same signals. The second must stand down,
    not queue."""
    import asyncio

    from runtime import app

    async def _go():
        async with app._awareness_lock:
            return await app._awareness_loop(trigger="join")

    assert asyncio.run(_go()) is None
