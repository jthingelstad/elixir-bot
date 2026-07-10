"""Bounded-event threads (channels.md §2): lifecycle idempotence, delivery
routing split, fallback behavior, and the game-event watcher."""

import asyncio
import json
import db
from engine import delivery
from engine.recognition.compose import render_intent
from runtime import threads as rt_threads


class _FakeThread:
    def __init__(self, tid):
        self.id = tid
        self.locked = False
        self.sent = []

    async def send(self, text):
        self.sent.append(text)

    async def edit(self, locked=False, archived=False):
        self.locked = locked


class _FakeChannel:
    def __init__(self, created):
        self._created = created

    async def create_thread(self, *, name, auto_archive_duration, type):
        t = _FakeThread(90000 + len(self._created))
        t.name = name
        self._created.append(t)
        return t


class _FakeBot:
    def __init__(self):
        self.created = []
        self.channel = _FakeChannel(self.created)
        self.threads = {}

    def get_channel(self, cid):
        return self.threads.get(cid, self.channel)


def _seed_week(conn, season=133, section=4, period="colosseum", finished=False):
    conn.execute("INSERT OR IGNORE INTO war_seasons (season_id, started_at) VALUES (?, ?)",
                 (season, "2026-07-01T00:00:00Z"))
    conn.execute(
        """INSERT OR REPLACE INTO war_weeks
           (season_id, section_index, period_type, created_date, finish_time)
           VALUES (?, ?, ?, '2026-07-01', ?)""",
        (season, section, period, "2026-07-05T09:00:00Z" if finished else None))
    conn.commit()


def test_war_week_thread_born_exactly_once():
    conn = db.get_connection()
    try:
        _seed_week(conn, finished=False)
    finally:
        conn.close()
    bot = _FakeBot()
    tid = asyncio.run(rt_threads.ensure_war_week_thread(bot, db.get_connection, 111))
    assert tid is not None and len(bot.created) == 1
    assert bot.created[0].name == "Colosseum — Season 133"
    assert bot.created[0].sent, "opener must post"
    # second pass: thread_id set -> nothing to do (restart-proof idempotence)
    tid2 = asyncio.run(rt_threads.ensure_war_week_thread(bot, db.get_connection, 111))
    assert tid2 is None and len(bot.created) == 1


def test_war_day_intent_carries_thread_and_week_finished_does_not():
    conn = db.get_connection()
    try:
        _seed_week(conn, finished=False)
        conn.execute("UPDATE war_weeks SET thread_id = 777 WHERE season_id = 133")
        conn.commit()
        wk = conn.execute("SELECT thread_id FROM war_weeks WHERE season_id=133").fetchone()
        iid = delivery.raise_intent(
            conn, None, "war:war_day_opened", "river-race", "public",
            {"event_type": "war_day_opened"}, "2026-07-05T10:00:00Z",
            thread_id=wk["thread_id"])
        row = conn.execute("SELECT * FROM communication_intents WHERE intent_id=?", (iid,)).fetchone()
        assert row["thread_id"] == 777
        # week_finished: link in payload, channel delivery (no thread_id)
        iid2 = delivery.raise_intent(
            conn, None, "war:week_finished", "river-race", "public",
            {"event_type": "week_finished", "our_rank": 1, "our_fame": 30000,
             "week_thread_id": 777}, "2026-07-05T10:00:00Z")
        row2 = conn.execute("SELECT * FROM communication_intents WHERE intent_id=?", (iid2,)).fetchone()
        assert row2["thread_id"] is None
        assert "<#777>" in render_intent(row2)
    finally:
        conn.close()


def test_delivery_routes_thread_to_capable_send_fn_only():
    conn = db.get_connection()
    try:
        delivery.raise_intent(conn, None, "war:war_day_opened", "river-race", "public",
                              {"event_type": "war_day_opened"}, "2026-07-05T10:00:00Z",
                              thread_id=555)
        conn.commit()
        seen = []

        def send3(lane, copy, thread_id=None):
            seen.append(("3", lane, thread_id))
            return "m1"

        delivery.consume(conn, send3, lambda i: "copy", "2026-07-05T10:01:00Z")
        assert seen == [("3", "river-race", 555)]
        # two-arg legacy stub still works (thread silently dropped)
        delivery.raise_intent(conn, None, "war:war_day_opened", "river-race", "public",
                              {"event_type": "war_day_opened"}, "2026-07-05T10:02:00Z",
                              thread_id=556)
        conn.commit()
        got = []

        def send2(lane, copy):
            got.append(lane)
            return "m2"

        delivery.consume(conn, send2, lambda i: "copy", "2026-07-05T10:03:00Z")
        assert got == ["river-race"]
    finally:
        conn.close()


def test_close_war_week_threads_locks_once():
    bot = _FakeBot()
    th = _FakeThread(777)
    bot.threads[777] = th
    intents = [
        {"intent_type": "war:week_finished",
         "payload_json": json.dumps({"week_thread_id": 777, "our_rank": 1, "our_fame": 30000})},
    ]
    closed = asyncio.run(rt_threads.close_war_week_threads(bot, db.get_connection, intents))
    assert closed == 1 and th.locked and any("record stands" in s for s in th.sent)
    # rescan within the 15-min window: already locked -> no double post
    closed2 = asyncio.run(rt_threads.close_war_week_threads(bot, db.get_connection, intents))
    assert closed2 == 0 and len(th.sent) == 1


def test_create_failure_means_no_row_and_channel_fallback():
    class _Broken:
        def get_channel(self, cid):
            raise RuntimeError("api down")

    conn = db.get_connection()
    try:
        _seed_week(conn, season=140, section=0, period="training", finished=False)
    finally:
        conn.close()
    tid = asyncio.run(rt_threads.ensure_war_week_thread(_Broken(), db.get_connection, 111))
    assert tid is None
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT thread_id FROM war_weeks WHERE season_id=140").fetchone()
        assert row["thread_id"] is None  # need stays pending; posts fall back to channel
    finally:
        conn.close()
