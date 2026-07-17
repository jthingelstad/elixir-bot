"""The awareness loop STREAMS its train of thought to #thinking bot-native
(replaced the old #elixir-log webhook): a `start` opens a per-loop thread with
the read, `tool`/`truncation`/`retry` append live as they happen, and `end`
finalizes the header with the outcome. Nothing member-facing."""

import asyncio

from runtime import app


class _FakeThread:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, body, allowed_mentions=None):
        self.sent.append(body)

    async def edit(self, name=None):
        if name is not None:
            self.name = name


class _FakeMessage:
    def __init__(self):
        self.thread = None
        self.embed = None

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


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, cid):
        return self._channel if cid == app.THINKING_CHANNEL_ID else None


def _render():
    return {
        "header": "🧠 AWARENESS (SHADOW — nothing posted) · Loop #7",
        "outcome": "posted",
        "color": 0x2ECC71,
        "fields": {
            "Read": "lanes: war 1",
            "Thinking": "1 tool call(s)",
            "Decision": "1 intended post(s): #river-race",
        },
        "thread_name": "Loop #7 · posted",
        "thread_chunks": ["decision chunk one", "decision chunk two"],
    }


def _run_stream(channel, monkeypatch, events):
    monkeypatch.setattr(app, "bot", _FakeBot(channel))
    app._thinking_session.clear()
    for e in events:
        asyncio.run(app._awareness_event(e))


def test_stream_opens_thread_appends_live_then_finalizes(monkeypatch):
    channel = _FakeChannel()
    _run_stream(
        channel,
        monkeypatch,
        [
            {"type": "start", "read_summary": "lanes: war 1"},
            {
                "type": "tool",
                "tool": "get_member",
                "args": "#ABC",
                "result": "ok · 5 keys",
            },
            {"type": "truncation", "phase": "initial_response", "max_tokens": 8192},
            {"type": "retry", "reason": "truncation", "max_tokens": 16384},
            {"type": "end", "render": _render(), "loop_number": 7},
        ],
    )

    # One header message with a thread.
    assert len(channel.messages) == 1
    msg = channel.messages[0]
    thread = msg.thread
    assert thread is not None

    joined = "\n".join(thread.sent)
    # The live tool call, the truncation, and the retry all streamed into the thread.
    assert "get_member" in joined
    assert "truncated" in joined.lower()
    assert "retrying" in joined.lower()
    # The verbatim decision landed at the end.
    assert "decision chunk one" in joined and "decision chunk two" in joined

    # Finalized: header embed swapped to the outcome, thread renamed with the number.
    assert msg.embed is not None and "Loop #7" in msg.embed.title
    assert thread.name == "Loop #7 · posted"
    assert app._thinking_session == {}  # session cleared


def test_stream_missing_channel_is_safe_noop(monkeypatch):
    class _NoChannelBot:
        def get_channel(self, cid):
            return None

    monkeypatch.setattr(app, "bot", _NoChannelBot())
    app._thinking_session.clear()
    # Must not raise even with no channel.
    asyncio.run(app._awareness_event({"type": "start", "read_summary": "x"}))
    asyncio.run(app._awareness_event({"type": "end", "render": _render(), "loop_number": 7}))


def test_tool_event_before_start_is_ignored(monkeypatch):
    # A stray event with no open session must not raise.
    channel = _FakeChannel()
    monkeypatch.setattr(app, "bot", _FakeBot(channel))
    app._thinking_session.clear()
    asyncio.run(app._awareness_event({"type": "tool", "tool": "x", "result": "y"}))
    assert channel.messages == []
