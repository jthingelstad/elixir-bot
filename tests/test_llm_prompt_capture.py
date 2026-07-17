"""Prompt capture lives at the LLM-call layer: EVERY call records its full
assembled prompt + response onto its llm_calls row (not just awareness), so
anything Elixir sends the model is drillable in the Observatory. Blobs are
pruned after 14 days; the metadata row survives 90 days for cost analysis.
The #thinking end-of-loop links to the LLM view."""

from __future__ import annotations

import asyncio
import json

import agent.core as core
import db
from db import managed_connection
from runtime import app
from runtime.awareness import diagnostic as diag_mod
from runtime.webapp import queries
from storage import messages as messages_store
from storage import metadata as metadata_store


class _Block:
    def __init__(self, **k):
        self.__dict__.update(k)


class _Usage:
    input_tokens = 120
    output_tokens = 30
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 90


class _Resp:
    stop_reason = "end_turn"
    usage = _Usage()

    def __init__(self, text, tool_uses=None):
        self.content = [_Block(type="text", text=text)] + list(tool_uses or [])


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    @property
    def messages(self):
        resp = self._resp

        class _Messages:
            def create(self, **kw):
                return resp

        return _Messages()


# --------------------------------------------------------------- capture is always-on


def test_every_call_captures_prompt_and_response(monkeypatch):
    monkeypatch.setattr(core, "_get_client", lambda: _FakeClient(_Resp('{"ok": true}')))
    # conftest no-ops db.record_llm_call (no conn) to avoid DB pollution; the DB
    # is already isolated here, so restore the real writer to exercise capture.
    monkeypatch.setattr(db, "record_llm_call", messages_store.record_llm_call)
    # A NON-awareness workflow — capture must happen for everything, not just the brain.
    core._create_chat_completion(
        workflow="channel_update",
        system="CHANNEL UPDATE SYSTEM",
        messages=[{"role": "user", "content": "grade this post"}],
        max_tokens=1024,
    )
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT call_id, workflow, prompt_json, response_json FROM llm_calls "
            "WHERE workflow = 'channel_update' ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    prompt = json.loads(row["prompt_json"])
    response = json.loads(row["response_json"])
    assert prompt["system"] == "CHANNEL UPDATE SYSTEM"
    assert prompt["messages"] == [{"role": "user", "content": "grade this post"}]
    assert response["text"] == '{"ok": true}'
    assert response["stop_reason"] == "end_turn"


def test_get_llm_call_roundtrips_blobs_decoded(monkeypatch):
    tool_use = _Block(
        type="tool_use", id="tu_1", name="get_elixir_state", input={"section": "battle"}
    )
    monkeypatch.setattr(
        core,
        "_get_client",
        lambda: _FakeClient(_Resp('{"posts": []}', tool_uses=[tool_use])),
    )
    monkeypatch.setattr(db, "record_llm_call", messages_store.record_llm_call)
    core._create_chat_completion(
        workflow="awareness",
        system="SYS",
        messages=[{"role": "user", "content": "the read"}],
        max_tokens=8192,
    )
    conn = db.get_connection()
    try:
        call_id = conn.execute(
            "SELECT call_id FROM llm_calls WHERE workflow='awareness' ORDER BY call_id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    detail = db.get_llm_call(call_id)
    assert detail["prompt"]["system"] == "SYS"
    assert detail["response"]["tool_uses"] == [
        {"name": "get_elixir_state", "input": {"section": "battle"}}
    ]


def test_llm_call_detail_query_and_missing():
    messages_store.record_llm_call(
        "clanops",
        "claude-sonnet-4-6",
        ok=True,
        prompt_json=json.dumps({"system": "s", "messages": []}),
        response_json=json.dumps({"text": "hi", "tool_uses": []}),
    )
    conn = db.get_connection()
    try:
        call_id = conn.execute("SELECT MAX(call_id) FROM llm_calls").fetchone()[0]
    finally:
        conn.close()
    data = queries.llm_call_detail(call_id)
    assert data["prompt"]["system"] == "s"
    assert data["response"]["text"] == "hi"
    assert queries.llm_call_detail(9_999_999) is None


# --------------------------------------------------------------- retention


@managed_connection
def _insert_call_at(recorded_at, *, with_blobs=True, conn=None):
    messages_store._ensure_llm_blob_columns(conn)
    conn.execute(
        "INSERT INTO llm_calls (recorded_at, workflow, model, ok, prompt_json, response_json) "
        "VALUES (?, 'awareness', 'm', 1, ?, ?)",
        (
            recorded_at,
            '{"system":"s"}' if with_blobs else None,
            '{"text":"t"}' if with_blobs else None,
        ),
    )
    conn.commit()


def test_blobs_pruned_after_14d_row_survives():
    # 30 days old: past the 14d BLOB window but within the 90d ROW window, so the
    # row must survive with its blobs NULLed (not deleted).
    from datetime import datetime, timedelta, timezone

    mid_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_call_at(mid_ts)
    _insert_call_at("2999-01-01T00:00:00Z")  # future → blobs kept
    stats = metadata_store.purge_old_data()
    assert stats.get("llm_calls_blobs_pruned", 0) >= 1

    conn = db.get_connection()
    try:
        old = conn.execute(
            "SELECT prompt_json, response_json FROM llm_calls WHERE recorded_at=?",
            (mid_ts,),
        ).fetchone()
        fresh = conn.execute(
            "SELECT prompt_json FROM llm_calls WHERE recorded_at='2999-01-01T00:00:00Z'"
        ).fetchone()
    finally:
        conn.close()
    # 30-day row still EXISTS (metadata kept < 90d) but its blobs are gone.
    assert old is not None and old["prompt_json"] is None and old["response_json"] is None
    # Fresh row keeps its blobs.
    assert fresh["prompt_json"] is not None


def test_metadata_row_deleted_after_90d():
    # 100 days old → past the 90d metadata window → whole row gone.
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_call_at(old_ts, with_blobs=False)
    metadata_store.purge_old_data()
    conn = db.get_connection()
    try:
        gone = conn.execute("SELECT 1 FROM llm_calls WHERE recorded_at = ?", (old_ts,)).fetchone()
    finally:
        conn.close()
    assert gone is None


# --------------------------------------------------------------- discord link → /llm


def test_observatory_url_points_at_llm_workflow_view():
    url = diag_mod.observatory_url()
    assert url.endswith("/llm?workflow=awareness")


class _FakeThread:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, body, allowed_mentions=None, suppress_embeds=False):
        self.sent.append(body)

    async def edit(self, name=None):
        if name is not None:
            self.name = name


class _FakeMessage:
    def __init__(self):
        self.embed = None
        self.thread = None

    async def create_thread(self, *, name, auto_archive_duration):
        self.thread = _FakeThread(name)
        return self.thread

    async def edit(self, embed=None):
        self.embed = embed


class _FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, embed=None, allowed_mentions=None):
        m = _FakeMessage()
        m.embed = embed
        self.messages.append(m)
        return m


def test_end_event_links_to_llm_view(monkeypatch):
    channel = _FakeChannel()

    class _Bot:
        def get_channel(self, cid):
            return channel if cid == app.THINKING_CHANNEL_ID else None

    monkeypatch.setattr(app, "bot", _Bot())
    app._thinking_session.clear()
    render = {
        "header": "🧠 · Loop #42",
        "outcome": "posted",
        "color": 0x2ECC71,
        "fields": {"Decision": "1 post"},
        "thread_name": "Loop #42 · posted",
        "thread_chunks": ["the decision"],
        "observatory_url": diag_mod.observatory_url(),
    }
    asyncio.run(app._awareness_event({"type": "start", "read_summary": "x"}))
    asyncio.run(app._awareness_event({"type": "end", "render": render, "loop_number": 42}))

    thread = channel.messages[0].thread
    assert any("/llm?workflow=awareness" in s for s in thread.sent)
    assert channel.messages[0].embed.url.endswith("/llm?workflow=awareness")
