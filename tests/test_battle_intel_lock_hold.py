"""rebuild_interpreted must not hold the single SQLite writer for its whole run.

2026-08-03, 12:47 CT: this call held the write lock 46.1s across 117,434
statements. Every API persist failed with `database is locked` for 17 minutes
(28 payloads dropped), the engine tick was skipped as "maximum number of running
instances reached", and the job never finished — run_count 1, success_count 0.

The work is idempotent and documented safe to re-run, so it does not need to be
atomic. It needs to give the writer back.
"""

from __future__ import annotations

import storage.battle_intel as bi


class _CommitSpy:
    """Wraps a connection and counts commits without changing behaviour."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def commit(self):
        self.commits += 1
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_borrowed_connection_is_never_committed(engine_conn, monkeypatch):
    """The contract that makes chunking safe.

    An inner commit on a borrowed connection persists the caller's partial work
    and defeats the engine tick's per-step rollback guard. Chunked commits are
    allowed ONLY when this call owns the connection.
    """
    spy = _CommitSpy(engine_conn)
    monkeypatch.setattr(bi, "WRITE_CHUNK", 1)
    bi.rebuild_interpreted(conn=spy)
    assert spy.commits == 0, "borrowed connection must not be committed"


def test_an_owned_connection_commits_between_chunks(engine_conn, monkeypatch):
    """Owning the connection means releasing the writer as it goes."""
    spy = _CommitSpy(engine_conn)
    monkeypatch.setattr(bi, "WRITE_CHUNK", 1)
    monkeypatch.setattr("db.get_connection", lambda *a, **k: spy)
    monkeypatch.setattr(spy, "close", lambda: None, raising=False)
    bi.rebuild_interpreted()
    assert spy.commits > 0, "an owned run must commit as it goes, not once at the end"


def test_the_batch_loop_stops_on_a_short_batch(engine_conn, monkeypatch):
    """A short batch proves every remaining untagged battle was processed."""
    calls = {"n": 0}

    def _stuck(conn, limit=bi._BATTLE_TAG_BATCH, force=False, checkpoint=None):
        calls["n"] += 1
        if calls["n"] > 50:
            raise AssertionError("rebuild_interpreted did not terminate")
        return 7  # always short, never zero — the livelock shape

    monkeypatch.setattr(bi, "_fill_battle_tags", _stuck)
    monkeypatch.setattr(bi, "_fill_deck_facts", lambda *a, **k: 0)
    result = bi.rebuild_interpreted(conn=engine_conn)
    assert calls["n"] == 1
    assert result["battle_tags"] == 7


def test_incremental_tags_ignore_already_tagged_incomplete_decks(engine_conn, monkeypatch):
    """Incomplete card facts must not keep an already-tagged batch runnable forever."""
    engine_conn.execute(
        "INSERT INTO deck_profile "
        "(deck_hash, family, archetype, cards_json, scored_at, facts_complete) "
        "VALUES ('incomplete', 'x', 'y', '[]', '2026-08-03T00:00:00Z', 0)"
    )
    engine_conn.execute(
        "INSERT INTO battle_events "
        "(dedup_key, player_tag, battle_time, observed_at, game_mode_name, is_ranked) "
        "VALUES ('already-tagged', '#T', '2026-08-03T00:00:00Z', "
        "'2026-08-03T00:00:00Z', 'Ladder', 0)"
    )
    engine_conn.execute(
        "INSERT INTO battle_enrichment "
        "(battle_dedup_key, player_tag, battle_time, our_deck_hash, "
        "level_validity, decisive_factor) "
        "VALUES ('already-tagged', '#T', '2026-08-03T00:00:00Z', 'incomplete', "
        "'real', 'level_gap')"
    )
    engine_conn.commit()
    monkeypatch.setattr(bi, "_card_facts_map", lambda conn: {(1, 0): {}})

    assert bi._fill_battle_tags(engine_conn) == 0


class _WriteSpy(_CommitSpy):
    """Counts row-at-a-time UPDATEs versus batched ones."""

    def __init__(self, conn):
        super().__init__(conn)
        self.single_updates = 0
        self.batched_rows = 0

    def execute(self, sql, parameters=()):
        if sql.lstrip()[:6].upper() == "UPDATE":
            self.single_updates += 1
        return self._conn.execute(sql, parameters)

    def executemany(self, sql, parameters):
        rows = list(parameters)
        self.batched_rows += len(rows)
        return self._conn.executemany(sql, rows)


def test_deck_updates_go_through_executemany(engine_conn):
    """117,434 single-row statements is the thing being fixed."""
    engine_conn.executemany(
        "INSERT OR REPLACE INTO deck_profile "
        "(deck_hash, cards_json, family, archetype, scored_at, facts_complete) "
        "VALUES (?, ?, 'x', 'y', '2026-08-03T00:00:00Z', 0)",
        [(f"probe{i}", "[[26000000, 0]]") for i in range(5)],
    )
    engine_conn.commit()
    spy = _WriteSpy(engine_conn)
    bi._fill_deck_facts(spy, restate=True)
    assert spy.single_updates == 0, "deck facts must not be written one row per statement"
