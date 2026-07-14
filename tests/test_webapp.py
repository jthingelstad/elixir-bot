"""Observatory webapp tests (runtime/webapp/): identity middleware, page
renders against the fixture DB, the ring buffer, and the safe ops."""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

import db
from runtime.webapp import chat as webapp_chat
from runtime.webapp import ops as webapp_ops
from runtime.webapp import ticks as webapp_ticks
from runtime.webapp.server import build_app

LOGIN = {"Tailscale-User-Login": "jthingelstad@github"}


def _client_run(coro_fn):
    """Run an async test body with a TestClient around the app."""

    async def _main():
        app = build_app(deps=None)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await coro_fn(client)
        finally:
            await client.close()

    return asyncio.run(_main())


# ------------------------------------------------------------ middleware

def test_identity_required():
    async def body(client):
        r = await client.get("/")
        assert r.status == 403
        r = await client.get("/", headers={"Tailscale-User-Login": "someone@else"})
        assert r.status == 403
        r = await client.get("/", headers=LOGIN)
        assert r.status == 200
        r = await client.get("/healthz")  # liveness is identity-exempt
        assert r.status == 200

    _client_run(body)


def test_any_login_allowed_when_env_empty(monkeypatch):
    monkeypatch.setenv("TAILSCALE_ALLOWED_LOGIN", "")

    async def body(client):
        r = await client.get("/", headers={"Tailscale-User-Login": "someone@else"})
        assert r.status == 200
        r = await client.get("/")
        assert r.status == 403  # header still required

    _client_run(body)


# ----------------------------------------------------------- page renders

def test_pages_render_on_empty_db():
    async def body(client):
        for path in ("/", "/members", "/awards", "/ticks", "/streams",
                     "/recognition", "/polling", "/management", "/war",
                     "/llm", "/chat"):
            r = await client.get(path, headers=LOGIN)
            assert r.status == 200, path
            text = await r.text()
            assert "Elixir" in text, path

    _client_run(body)


def test_members_awards_baseline_render_seeded():
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
               VALUES ('#ROSTER1', 'Rosterling', '2026-06-01T00:00:00Z', '2026-07-04T00:00:00Z')"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)
               VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)"""
        )
        conn.execute(
            """INSERT INTO clan_memberships (player_tag, joined_at, join_source)
               VALUES ('#ROSTER1', '2026-06-01', 'test')"""
        )
        conn.execute(
            """INSERT INTO awards (award_type, season_id, player_tag, rank,
                                   metric_value, metric_unit, awarded_at)
               VALUES ('war_champ', 133, '#ROSTER1', 1, 12345, 'fame',
                       '2026-07-04T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO state_baselines
               (entity_kind, entity_tag, aspect, payload_json, payload_hash,
                observed_at)
               VALUES ('player', '#ROSTER1', 'profile',
                       '{"level": 45, "name": "Rosterling"}', 'h1',
                       '2026-07-04T00:00:00Z')"""
        )
        # Regression (live incident 2026-07-04): feedback_value is TEXT
        # 'up'/'down'; a numeric comparison in llm.html 500'd on real rows.
        conn.execute(
            """INSERT INTO prompt_feedback (assistant_discord_message_id,
                   discord_user_id, workflow, channel_name, feedback_value,
                   question, response_preview, recorded_at, updated_at)
               VALUES ('m1', 'u1', 'ask_elixir', '#ask-elixir', 'up', 'test q',
                       'test resp', '2026-07-04T00:00:00Z', '2026-07-04T00:00:00Z'),
                      ('m2', 'u1', 'ask_elixir', '#ask-elixir', 'down', 'test q2',
                       'test resp2', '2026-07-04T00:00:00Z', '2026-07-04T00:00:00Z')"""
        )
        conn.commit()
    finally:
        conn.close()

    async def body(client):
        r = await client.get("/members", headers=LOGIN)
        assert r.status == 200
        assert "Rosterling" in await r.text()
        r = await client.get("/awards", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "war_champ" in text and "Season 133" in text
        r = await client.get(
            "/baseline?kind=player&tag=%23ROSTER1&aspect=profile", headers=LOGIN
        )
        assert r.status == 200
        text = await r.text()
        assert "Rosterling" in text and "first sight" in text
        r = await client.get("/baseline?kind=player&tag=%23NOPE&aspect=profile",
                             headers=LOGIN)
        assert r.status == 404
        r = await client.get("/llm", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "up" in text and "down" in text  # text feedback values render

    _client_run(body)


def _seed_claim_and_intent():
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
               VALUES ('#SEED1', 'Seedling', '2026-07-01T00:00:00Z', '2026-07-04T00:00:00Z')"""
        )
        # ledger first: communication_intents.recognition_key FKs the ledger
        conn.execute(
            """INSERT INTO recognition_ledger
               (recognition_key, stream, event_refs_json, score, claimed_at, intent_id)
               VALUES ('collection_level_milestone:#SEED1:1700', 'player',
                       '{"refs": [], "suppressed": null}', 85, '2026-07-04T00:00:00Z', 7)"""
        )
        conn.execute(
            """INSERT INTO communication_intents
               (intent_id, recognition_key, intent_type, lane, scope, payload_json,
                status, attempts, created_at, expires_at, last_error)
               VALUES (7, 'collection_level_milestone:#SEED1:1700', 'celebrate:collection_level_milestone', 'member-highlights',
                       'public', '{"subject_tag": "#SEED1"}', 'failed', 2,
                       '2026-07-04T00:00:00Z', '2026-07-04T06:00:00Z', 'boom')"""
        )
        conn.execute(
            """INSERT INTO recognition_ledger
               (recognition_key, stream, event_refs_json, score, claimed_at)
               VALUES ('best_trophies_peak:#SEED1:5100', 'player',
                       '{"suppressed": {"reason": "player_highlight_accruing", "score": 40}}',
                       40, '2026-07-04T00:01:00Z')"""
        )
        conn.execute(
            """INSERT INTO poll_state (player_tag, temperature, heat, updated_at)
               VALUES ('#SEED1', 'warm', 2, '2026-07-04T00:00:00Z')"""
        )
        conn.commit()
    finally:
        conn.close()


def test_recognition_shows_suppression_and_status():
    _seed_claim_and_intent()

    async def body(client):
        r = await client.get("/recognition", headers=LOGIN)
        text = await r.text()
        assert "player_highlight_accruing" in text
        assert "failed" in text
        r = await client.get(
            "/recognition/best_trophies_peak:%23SEED1:5100", headers=LOGIN
        )
        assert r.status == 200
        assert "player_highlight_accruing" in await r.text()
        r = await client.get("/member/SEED1", headers=LOGIN)
        assert r.status == 200
        assert "Seedling" in await r.text()

    _client_run(body)


# ------------------------------------------------------------- ring buffer

def test_tick_history_persists_and_orders():
    # Since 2026-07-04 record_tick dual-writes: in-memory ring (fast path,
    # maxlen 288) + the tick_history table (30d, survives restarts).
    webapp_ticks._TICKS.clear()
    for i in range(300):
        webapp_ticks.record_tick({"n": i})
    recent = webapp_ticks.recent_ticks(5)
    assert [t["n"] for t in recent] == [299, 298, 297, 296, 295]
    assert all("recorded_at" in t for t in recent)
    # Ring stays bounded; the table holds everything within retention.
    assert len(webapp_ticks._TICKS) == 288
    assert len(webapp_ticks.recent_ticks(1000)) == 300
    # Persistence survives a "restart" (ring cleared).
    webapp_ticks._TICKS.clear()
    assert [t["n"] for t in webapp_ticks.recent_ticks(3)] == [299, 298, 297]


# ---------------------------------------------------------------- safe ops

def test_weekly_review_dryrun_rolls_back():
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
               VALUES ('#DRY1', 'DryRun', '2026-05-01T00:00:00Z', '2026-07-04T00:00:00Z')"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)
               VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-04', 1)"""
        )
        conn.execute(
            """INSERT INTO clan_memberships (player_tag, joined_at, join_source)
               VALUES ('#DRY1', '2026-05-01', 'test')"""
        )
        conn.execute(
            """INSERT INTO member_management
               (player_tag, computed_at, week_anchor, promote_qualifying_weeks, state_json)
               VALUES ('#DRY1', '2026-07-01T00:00:00Z', '2026-06-23', 2, '{"x": 1}')"""
        )
        conn.commit()
        before = dict(conn.execute(
            "SELECT * FROM member_management WHERE player_tag = '#DRY1'"
        ).fetchone())
    finally:
        conn.close()

    result = webapp_ops.weekly_review_dryrun()
    assert result.get("dry_run") is True

    conn = db.get_connection()
    try:
        after = dict(conn.execute(
            "SELECT * FROM member_management WHERE player_tag = '#DRY1'"
        ).fetchone())
        assert after == before  # rolled back — nothing changed
    finally:
        conn.close()


# --------------------------------------------------------------------- chat

def test_chat_post_and_poll(monkeypatch):
    monkeypatch.setattr(webapp_chat, "_run_agent", lambda q, login, prior: f"echo: {q}")

    async def body(client):
        r = await client.post("/chat", headers=LOGIN, data={"message": "why no post?"})
        assert r.status == 200
        assert (await r.json())["ok"] is True
        # let the background agent task finish
        for _ in range(50):
            await asyncio.sleep(0.02)
            r = await client.get("/chat/messages", headers=LOGIN)
            msgs = (await r.json())["messages"]
            if len(msgs) >= 2:
                break
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles
        assert any("echo: why no post?" in m["content"] for m in msgs)

    _client_run(body)


def test_ops_routes_same_origin_guard():
    async def body(client):
        r = await client.post(
            "/ops/tick", headers={**LOGIN, "Origin": "https://evil.example"}
        )
        assert r.status == 403
        # Cold review 2026-07-04 #6: prefix bypass — https://<host>.evil.com
        # must be rejected (exact host equality, not startswith).
        evil = f"https://{client.host}:{client.port}.evil.com"
        r = await client.post("/ops/tick", headers={**LOGIN, "Origin": evil})
        assert r.status == 403
        # and the legitimate same-origin form post still works
        ok = f"http://{client.host}:{client.port}"
        r = await client.post("/ops/tick", headers={**LOGIN, "Origin": ok})
        # passes the origin gate (redirect followed to the flash page)
        assert r.status == 200 and "tick scheduled" in await r.text()

    _client_run(body)


def test_raw_payload_page():
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO raw_api_payloads (endpoint, entity_key, fetched_at,
                                             payload_hash, payload_json)
               VALUES ('clan', 'global', '2026-07-04T00:00:00Z', 'h', ?)""",
            (json.dumps({"tag": "#J2RGCRVG", "name": "POAP KINGS"}),),
        )
        conn.commit()
        pid = conn.execute("SELECT MAX(payload_id) FROM raw_api_payloads").fetchone()[0]
    finally:
        conn.close()

    async def body(client):
        r = await client.get(f"/raw/{pid}", headers=LOGIN)
        assert r.status == 200
        assert "POAP KINGS" in await r.text()

    _client_run(body)


def test_role_action_card_gets_clan_chat_copy(monkeypatch):
    """Every promotion/demotion/kick card must carry a clan-chat 'why' message.
    Guards the lazy-import path in _ensure_role_action_clan_chat_copy (a bad
    import there silently killed card posting, live 2026-07-05)."""
    import asyncio
    import runtime.app as app
    import db

    from storage.leader_actions import create_leader_action_recommendation

    conn = db.get_connection()
    try:
        created = create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="Promote X",
            rationale="Ultimate Champion and strong war contributor.",
            target_player_tag="#X",
            target_player_name="Xavier",
            source_signal_key="test:role-copy",
            source_signal_type="leadership_manual",
            conn=conn,
        )
        aid = created["action_id"]
        action = dict(conn.execute(
            "SELECT * FROM leader_action_recommendations WHERE action_id=?", (aid,)).fetchone())
        conn.commit()   # the test owns this conn, so it commits it (writers no
                        # longer commit a borrowed connection — that was the bug)
    finally:
        conn.close()

    # Force the deterministic fallback (no live LLM in tests).
    async def _boom(*a, **k):
        raise RuntimeError("no LLM in tests")
    monkeypatch.setattr("runtime.clan_chat_copy.generate_clan_chat_copy", _boom)

    out = asyncio.run(app._ensure_role_action_clan_chat_copy(action))
    assert out.get("copy_current_text")  # copy attached via deterministic fallback
    assert "Xavier" in out["copy_current_text"]

    conn = db.get_connection()
    try:
        persisted = conn.execute(
            "SELECT copy_current_text FROM leader_action_recommendations WHERE action_id=?", (aid,)
        ).fetchone()[0]
        assert persisted and "Xavier" in persisted  # persisted, not just in-memory
    finally:
        conn.close()
