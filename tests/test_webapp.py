"""Observatory webapp tests (runtime/webapp/): identity middleware, page
renders against the fixture DB, the ring buffer, and the safe ops."""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

import db
from runtime.webapp import chat as webapp_chat
from runtime.webapp import ops as webapp_ops
from runtime.webapp import queries
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
        for path in (
            "/",
            "/members",
            "/awards",
            "/ticks",
            "/streams",
            "/api-sentinel",
            "/awareness",
            "/activities",
            "/polling",
            "/management",
            "/war",
            "/llm",
            "/cost",
            "/chat",
        ):
            r = await client.get(path, headers=LOGIN)
            assert r.status == 200, path
            text = await r.text()
            assert "Elixir" in text, path

    _client_run(body)


def test_llm_cost_page_aggregates_by_workflow_and_prices_models():
    """The cost panel sums spend per workflow and prices Sonnet/Haiku/Opus (the
    Opus branch fixes a $0 undercount in the canonical formula)."""
    conn = db.get_connection()
    try:
        # 1M completion tokens: Sonnet 5 introductory pricing → $10, Opus → $75.
        conn.execute(
            "INSERT INTO llm_calls (recorded_at, workflow, model, ok, "
            "completion_tokens, total_tokens) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%S','now'), 'awareness', 'claude-sonnet-5', 1, "
            "1000000, 1000000)",
        )
        conn.execute(
            "INSERT INTO llm_calls (recorded_at, workflow, model, ok, "
            "completion_tokens, total_tokens) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%S','now'), 'memory_synthesis', 'claude-opus-4-8', "
            "1, 1000000, 1000000)",
        )
        conn.commit()
    finally:
        conn.close()

    data = queries.llm_cost_page()
    by_wf = {w["workflow"]: w for w in data["workflows_7d"]}
    assert abs(by_wf["awareness"]["cost_usd"] - 10.0) < 0.01
    assert abs(by_wf["memory_synthesis"]["cost_usd"] - 75.0) < 0.01  # opus priced, not $0

    async def body(client):
        r = await client.get("/cost", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "LLM cost" in text and "awareness" in text and "memory_synthesis" in text

    _client_run(body)


def test_api_sentinel_page_shows_admission_and_drift():
    """The API-sentinel view surfaces admission verdicts (rejections prominent)
    and first-seen schema paths — neither had a UI before."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO api_observation_receipts (endpoint, entity_key, fetched_at, "
            "payload_hash, admission_status, admission_errors_json) VALUES "
            "('clan', '#J2RGCRVG', '2026-07-16T18:00:00Z', 'h1', 'rejected', "
            "'[\"memberList:missing\"]')",
        )
        conn.execute(
            "INSERT INTO api_sentinel_observations (sentinel_type, scope, name, "
            "endpoint, entity_key, first_seen_at, last_seen_at, created_at, "
            "updated_at) VALUES ('schema_path', 'field', 'newMysteryField', "
            "'player', '#X', '2026-07-16T18:00:00Z', '2026-07-16T18:00:00Z', "
            "'2026-07-16T18:00:00Z', '2026-07-16T18:00:00Z')",
        )
        conn.commit()
    finally:
        conn.close()

    async def body(client):
        r = await client.get("/api-sentinel", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "API sentinel" in text
        assert "rejected" in text and "memberList:missing" in text
        assert "newMysteryField" in text

    _client_run(body)


def test_activities_page_lists_the_registry_with_status():
    """The /activities registry lists every scheduled job (static registry) and
    joins its last-run outcome from runtime_job_status."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO runtime_job_status (job_name, status_json, updated_at) VALUES "
            "('engine_tick', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (
                json.dumps(
                    {
                        "run_count": 42,
                        "failure_count": 0,
                        "last_finished_at": "2026-07-16T17:00:00Z",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    async def body(client):
        r = await client.get("/activities", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "Scheduled activities" in text
        # static registry always present
        assert "engine-tick" in text and "awareness-loop" in text
        # schedule descriptions render
        assert "Every 10 minutes" in text
        # joined status (run count) shows
        assert "42" in text

    _client_run(body)


def test_awareness_page_renders_loops_and_posts():
    """The dedicated awareness view surfaces recent loops (tier + decision) and
    the posts they produced — the hourly brain, previously only a card on
    Overview and two dead /awareness links."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO awareness_thoughts (thought_id, loop_number, at, read_json, "
            "plan_json, chose_silence, post_count, skipped_reason, model) VALUES "
            "('t-post', 900, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, '{}', 0, 1, '', NULL), "
            "('t-silence', 899, strftime('%Y-%m-%dT%H:%M:%SZ','now'), '{}', '{}', 1, 0, "
            "'quiet', 'gate:triage')",
            (json.dumps({"signals_by_category": {"milestone": [{"k": "a"}, {"k": "b"}]}}),),
        )
        conn.execute(
            "INSERT INTO awareness_posts (lane, content_preview, covers_json, "
            "loop_number, posted_at, discord_message_id) VALUES "
            "('elixir', 'A grounded war post', '[\"war_champ:1\"]', 900, "
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'), '123')",
        )
        conn.commit()
    finally:
        conn.close()

    async def body(client):
        r = await client.get("/awareness", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "Awareness loop" in text
        assert "#900" in text and "#899" in text
        assert "A grounded war post" in text
        assert "deliberate" in text and "triage" in text

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
        r = await client.get("/baseline?kind=player&tag=%23ROSTER1&aspect=profile", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "Rosterling" in text and "first sight" in text
        r = await client.get("/baseline?kind=player&tag=%23NOPE&aspect=profile", headers=LOGIN)
        assert r.status == 404
        r = await client.get("/llm", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "up" in text and "down" in text  # text feedback values render

    _client_run(body)


def test_member_page_shows_leader_action_trail():
    """A member's page threads their leadership trail — every #actions card that
    named them, nomination → decision (traceability for the leader flow)."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, "
            "last_seen_at) VALUES ('#LATRAIL', 'Trailmark', '2026-06-01T00:00:00Z', "
            "'2026-07-16T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO leader_action_recommendations (action_key, action_type, "
            "objective, status, prompt_text, target_player_tag, target_player_name, "
            "decision_note, proposed_at, decided_at, created_at, updated_at, is_test) "
            "VALUES ('k1', 'kick_recommendation', 'Idle 9 days', 'done', 'card copy', "
            "'#LATRAIL', 'Trailmark', 'agreed, kicked', '2026-07-15T00:00:00Z', "
            "'2026-07-15T02:00:00Z', '2026-07-15T00:00:00Z', '2026-07-15T02:00:00Z', 0)"
        )
        conn.commit()
    finally:
        conn.close()

    async def body(client):
        r = await client.get("/member/LATRAIL", headers=LOGIN)
        assert r.status == 200
        text = await r.text()
        assert "Leader actions" in text
        assert "kick_recommendation" in text
        assert "Idle 9 days" in text
        assert "agreed, kicked" in text

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
        before = dict(
            conn.execute("SELECT * FROM member_management WHERE player_tag = '#DRY1'").fetchone()
        )
    finally:
        conn.close()

    result = webapp_ops.weekly_review_dryrun()
    assert result.get("dry_run") is True

    conn = db.get_connection()
    try:
        after = dict(
            conn.execute("SELECT * FROM member_management WHERE player_tag = '#DRY1'").fetchone()
        )
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
        r = await client.post("/ops/tick", headers={**LOGIN, "Origin": "https://evil.example"})
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

    import db
    import runtime.app as app
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
        action = dict(
            conn.execute(
                "SELECT * FROM leader_action_recommendations WHERE action_id=?", (aid,)
            ).fetchone()
        )
        conn.commit()  # the test owns this conn, so it commits it (writers no
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
            "SELECT copy_current_text FROM leader_action_recommendations WHERE action_id=?",
            (aid,),
        ).fetchone()[0]
        assert persisted and "Xavier" in persisted  # persisted, not just in-memory
    finally:
        conn.close()
