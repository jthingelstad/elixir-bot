"""v5.1 memory system tests (docs/reference/v5.1/memory.md, D1–D5 ratified).

Covers: ranked selection golden cases, FTS round-trip, retention (real
expiry), migration parity on a seeded old-schema source, and the Observatory
/memories page.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

import db
import memory_store
from runtime.webapp.server import build_app

LOGIN = {"Tailscale-User-Login": "jthingelstad@github"}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _seed(
    conn,
    *,
    title,
    body,
    member_tag=None,
    kind="system",
    confidence=0.9,
    age_days=0.0,
    scope="public",
    expires_at=None,
):
    now = datetime.now(timezone.utc)
    ts = _iso(now - timedelta(days=age_days))
    memory = memory_store.create_memory(
        body=body,
        source_type=kind,
        is_inference=(kind == "inference"),
        confidence=confidence,
        created_by="test",
        scope=scope,
        title=title,
        member_tag=member_tag,
        expires_at=expires_at,
        conn=conn,
    )
    # backdate for recency tests
    conn.execute(
        "UPDATE memories SET created_at = ?, updated_at = ? WHERE memory_id = ?",
        (ts, ts, memory["memory_id"]),
    )
    conn.commit()
    return memory["memory_id"]


def test_ranked_selection_member_match_beats_recency():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        old_but_mine = _seed(
            conn,
            title="TDuck prefers short recaps",
            body="TDuck said keep war recaps short.",
            member_tag="#TDUCK1",
            age_days=40,
        )
        _seed(
            conn,
            title="Fresh unrelated note",
            body="The clan hit 40 members.",
            age_days=0,
        )
        picked = memory_store.select_memories(
            member_tag="#TDUCK1", viewer_scope="leadership", limit=2, conn=conn
        )
        assert picked and picked[0]["memory_id"] == old_but_mine
    finally:
        conn.close()


def test_ranked_selection_confidence_tiebreak():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        low = _seed(
            conn,
            title="A low",
            body="alpha fact",
            member_tag="#SAME1",
            kind="inference",
            confidence=0.5,
            age_days=1,
        )
        high = _seed(
            conn,
            title="A high",
            body="beta fact",
            member_tag="#SAME1",
            kind="inference",
            confidence=0.95,
            age_days=1,
        )
        picked = memory_store.select_memories(
            member_tag="#SAME1", viewer_scope="leadership", limit=2, conn=conn
        )
        assert [m["memory_id"] for m in picked[:2]] == [high, low]
    finally:
        conn.close()


def test_fts_round_trip_and_query_selection():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        target = _seed(
            conn,
            title="Season 133 finale",
            body="POAP KINGS won colosseum with a xylophone strategy.",
        )
        _seed(conn, title="Noise", body="Unrelated donation chatter.")
        hits = memory_store.search_memories("xylophone", viewer_scope="leadership", conn=conn)
        assert hits and hits[0].memory["memory_id"] == target
        picked = memory_store.select_memories(
            query="xylophone strategy", viewer_scope="public", limit=3, conn=conn
        )
        assert picked and picked[0]["memory_id"] == target
    finally:
        conn.close()


def test_scope_gating_public_viewer():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        _seed(
            conn,
            title="Leadership only",
            body="watch entry secret",
            member_tag="#SCOPED1",
            scope="leadership",
        )
        assert memory_store.select_memories(
            member_tag="#SCOPED1", viewer_scope="public", limit=5, conn=conn
        ) == [] or all(
            m["scope"] == "public"
            for m in memory_store.select_memories(
                member_tag="#SCOPED1", viewer_scope="public", limit=5, conn=conn
            )
        )
        leadership = memory_store.select_memories(
            member_tag="#SCOPED1", viewer_scope="leadership", limit=5, conn=conn
        )
        assert any(m["member_tag"] == "#SCOPED1" for m in leadership)
    finally:
        conn.close()


def test_real_expiry_hard_deletes():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        gone = _seed(conn, title="Expired", body="old news", expires_at="2020-01-01")
        keep = _seed(conn, title="Durable", body="forever news")
        removed = memory_store.purge_expired_memories(conn=conn)
        assert removed >= 1
        ids = {r[0] for r in conn.execute("SELECT memory_id FROM memories").fetchall()}
        assert gone not in ids and keep in ids
    finally:
        conn.close()


def test_migration_parity_seeded_source(tmp_path):
    # Old-schema source with two memories + tags; migrate into the fixture DB.
    src_path = tmp_path / "old-memory.db"
    src = sqlite3.connect(src_path)
    src.executescript("""
        CREATE TABLE clan_memories (
            memory_id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT,
            created_by TEXT, source_type TEXT, confidence REAL, scope TEXT,
            status TEXT, title TEXT, body TEXT, summary TEXT, member_tag TEXT,
            channel_id TEXT, event_type TEXT, event_id TEXT, expires_at TEXT);
        CREATE TABLE clan_memory_tags (tag_id INTEGER PRIMARY KEY, tag TEXT);
        CREATE TABLE clan_memory_tag_links (memory_id INTEGER, tag_id INTEGER);
        INSERT INTO clan_memories VALUES
            (1,'2026-06-01','2026-06-01','t','elixir_inference',0.8,'public',
             'active','Inferred','body one',NULL,'#AAA111',NULL,NULL,NULL,NULL),
            (2,'2026-06-02','2026-06-02','t','elixir_synthesis',1.0,
             'system_internal','archived','Arc','body two',NULL,NULL,NULL,
             'war','w133',NULL);
        INSERT INTO clan_memory_tags VALUES (1,'war'),(2,'arc');
        INSERT INTO clan_memory_tag_links VALUES (1,1),(2,2);
    """)
    src.commit()
    src.close()

    from scripts.migrate_v51.memory_migrate import run

    conn = db.get_connection()
    try:  # fixture DB path — run() opens by path, so resolve it
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        # The parity gate requires episodes present (M2 verify); seed one.
        conn.execute(
            "INSERT INTO memory_episodes (subject_type, subject_key, episode_type, "
            "summary, importance, created_at) VALUES ('channel', 'c1', 'test', "
            "'an episode', 1, '2026-07-04')"
        )
        conn.commit()
    finally:
        conn.close()
    assert run(db_path, str(src_path)) == 0

    conn = db.get_connection()
    try:
        rows = {
            r["memory_id"]: dict(r)
            for r in conn.execute("SELECT * FROM memories WHERE created_by = 't'").fetchall()
        }
        assert rows[1]["kind"] == "inference" and rows[1]["member_tag"] == "#AAA111"
        assert rows[2]["kind"] == "synthesis"
        assert rows[2]["scope"] == "leadership"  # system_internal mapped
        assert rows[2]["retired_at"] is not None  # archived → retired
        assert rows[2]["source_event_key"] == "war:w133"
        tags = {
            (r[0], r[1]) for r in conn.execute("SELECT memory_id, tag FROM memory_tags").fetchall()
        }
        assert (1, "war") in tags and (2, "arc") in tags
    finally:
        conn.close()


def test_memories_page_renders():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        _seed(
            conn,
            title="Render me",
            body="a very findable zanzibar fact",
            member_tag="#PAGE1",
        )
    finally:
        conn.close()

    async def body():
        app = build_app(deps=None)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/memories", headers=LOGIN)
            assert r.status == 200
            text = await r.text()
            assert "Render me" in text
            r = await client.get("/memories?q=zanzibar", headers=LOGIN)
            assert r.status == 200
            assert "Render me" in await r.text()
        finally:
            await client.close()

    asyncio.run(body())


def test_build_memory_context_ranked_and_query():
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#CTX1', 'Ctx', '2026-06-01', '2026-07-04')"
        )
        conn.commit()
        mine = _seed(
            conn,
            title="Ctx loves 2v2",
            body="Ctx said 2v2 is best",
            member_tag="#CTX1",
            age_days=30,
        )
        _seed(conn, title="Recent noise", body="fresh unrelated", age_days=0)
        ctx = db.build_memory_context(
            member_tag="#CTX1",
            viewer_scope="leadership",
            durable_memory_limit=3,
            conn=conn,
        )
        durables = ctx.get("durable_memories") or []
        assert durables and durables[0]["memory_id"] == mine
    finally:
        conn.close()


def test_filtered_selection_excludes_wrong_subject_recents():
    # Cold review 2026-07-04 #5: with a member filter, the recency backstop
    # must NOT inject fresh memories about OTHER members into the candidates.
    conn = db.get_connection()
    try:
        _seed(
            conn,
            title="A old fact",
            body="member A keeps donating",
            member_tag="#AAA",
            confidence=0.7,
            age_days=40,
        )
        for i in range(6):
            _seed(
                conn,
                title=f"B hot fact {i}",
                body=f"member B thing {i}",
                member_tag="#BBB",
                confidence=0.95,
                age_days=0,
            )
        got = memory_store.select_memories(
            member_tag="#AAA", viewer_scope="leadership", limit=5, conn=conn
        )
        tags = {m.get("member_tag") for m in got}
        assert tags == {"#AAA"}, f"wrong-subject memories leaked: {tags}"
    finally:
        conn.close()


def test_unfiltered_selection_still_has_recency_backstop():
    conn = db.get_connection()
    try:
        _seed(
            conn,
            title="fresh clanwide",
            body="clan won the race",
            confidence=0.9,
            age_days=0,
        )
        got = memory_store.select_memories(viewer_scope="leadership", limit=5, conn=conn)
        assert any(m["title"] == "fresh clanwide" for m in got)
    finally:
        conn.close()


def test_fts_selection_excludes_expired_and_retired():
    """Soft expiry must hold on EVERY candidate source, including FTS.

    The FTS branch of select_memories selects straight out of memories_fts and
    carries no scope_sql() predicate, so an archived or expired memory matching
    the query used to be scored and injected into the prompt — while the
    member/channel/tag branches correctly excluded it.

    That silently defeated the retention design: runtime/jobs/_memory.py sets
    expires_at on stale or contradicted synthesis rows precisely so they vanish
    from readers, and 4 of the 6 conversational lanes retrieve via FTS. A fact
    Elixir had decided was wrong could still be recalled at it.
    """
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        _run_fts_expiry_case(conn)
    finally:
        conn.close()


def _run_fts_expiry_case(memory_conn):
    live = _seed(memory_conn, title="zorblatt live", body="The zorblatt protocol is current.")
    _seed(
        memory_conn,
        title="zorblatt expired",
        body="The zorblatt protocol lapsed.",
        expires_at="2026-01-01T00:00:00Z",
    )
    archived = _seed(
        memory_conn, title="zorblatt archived", body="The zorblatt protocol was wrong."
    )
    memory_store.archive_memory(archived, actor="test", conn=memory_conn)

    got = memory_store.select_memories(
        query="zorblatt", viewer_scope="public", limit=10, conn=memory_conn
    )
    assert [m["memory_id"] for m in got] == [live]


def test_non_fts_candidate_sources_still_exclude_expired():
    """Contrast case: the member/channel branches were always correct, and must
    stay correct after the post-filter guard was added."""
    conn = db.get_connection()
    try:
        memory_store.ensure_memory_schema(conn)
        _run_non_fts_expiry_case(conn)
    finally:
        conn.close()


def _run_non_fts_expiry_case(memory_conn):
    _seed(
        memory_conn,
        title="lapsed member fact",
        body="stale",
        member_tag="#EXP1",
        expires_at="2026-01-01T00:00:00Z",
    )
    got = memory_store.select_memories(
        member_tag="#EXP1", viewer_scope="public", limit=10, conn=memory_conn
    )
    assert got == []
