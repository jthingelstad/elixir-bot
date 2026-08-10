"""What every model call records, and why each field is a column rather than a blob.

Every gap here cost a real investigation in the week to 2026-08-09, and each was
answered by re-running a workflow or wrapping the SDK client by hand, because
the data was not in the table:

- `effort` was introduced as the thinking lever with no visibility at all, so
  depth could not be related to cost.
- `attempts` was 2 on every claude-5 call for months — a guaranteed 400 on
  `temperature`, then the real request — and nothing recorded it.
- `timeout_s` had to be inferred from a cluster of failures at 181.7s (the 60s
  default times the SDK's two retries).
- `stop_reason` and `block_census` lived inside `response_json`, which is pruned
  at 14 days while the row lives 90. Truncation history was readable for 2,092
  of 9,297 rows: the outcome that matters most was the first thing discarded.
- `cost_usd` was computed on every call for the spend budget and thrown away,
  so reports maintained a second copy of the pricing table.
- `turn_id` did not exist, so per-workflow totals could answer "what did
  awareness cost this week" but never "what did one tick cost".
"""

from __future__ import annotations

import agent.core as core
from storage import telemetry as telemetry_store


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Block:
    def __init__(self, **k):
        self.__dict__.update(k)


class _Resp:
    usage = _Usage()

    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


def _record(monkeypatch, workflow, resp):
    """Run one call for real against the temp telemetry DB and return its row."""
    monkeypatch.setattr(core, "_get_client", lambda: _Client(resp))
    monkeypatch.setattr(core.db, "record_llm_call", telemetry_store.record_llm_call)
    core._create_chat_completion(workflow=workflow, messages=[{"role": "user", "content": "x"}])
    conn = telemetry_store.connect()
    return conn.execute(
        "SELECT * FROM llm_calls WHERE workflow = ? ORDER BY call_id DESC LIMIT 1",
        (workflow,),
    ).fetchone()


class _Client:
    def __init__(self, resp):
        self._resp = resp

    @property
    def messages(self):
        resp = self._resp

        class _M:
            def create(self, **kw):
                return resp

        return _M()


def test_call_configuration_is_recorded(monkeypatch):
    """What the call was ASKED to do — without this a stop_reason is unreadable."""
    row = _record(monkeypatch, "ask_elixir_daily", _Resp([_Block(type="text", text="hi")]))
    policy = core.policy_for("ask_elixir_daily")
    assert row["effort"] == policy.effort
    assert row["max_tokens"] == policy.max_tokens
    assert row["timeout_s"] == policy.timeout


def test_effort_is_null_where_the_model_rejects_it(monkeypatch):
    """Haiku 4.5 never receives effort, so recording one would be a lie."""
    row = _record(monkeypatch, "memory_distill", _Resp([_Block(type="text", text="hi")]))
    assert row["model"].startswith("claude-haiku-4-5")
    assert row["effort"] is None


def test_outcome_survives_blob_pruning(monkeypatch):
    """stop_reason and the block census are columns, not fields inside
    response_json — that blob is pruned at 14 days, the row lives 90."""
    row = _record(
        monkeypatch,
        "ask_elixir_daily",
        _Resp([_Block(type="text", text="hi")], stop_reason="max_tokens"),
    )
    assert row["stop_reason"] == "max_tokens"
    assert "text" in (row["block_census"] or "")


def test_a_thinking_only_response_is_distinguishable_from_an_empty_one(monkeypatch):
    """The 2026-08-08 failure: 4096 tokens spent, no text, no tool call. The
    census is what separates that from the model genuinely returning nothing.

    Note the thinking block's char count is 0 — `thinking.display` defaults to
    "omitted", so the text is always empty. The block being PRESENT is the
    signal; pair it with completion_tokens for the size."""
    row = _record(
        monkeypatch,
        "ask_elixir_daily",
        _Resp([_Block(type="thinking", thinking="")], stop_reason="max_tokens"),
    )
    census = row["block_census"] or ""
    assert "thinking" in census, "a thinking-only response must not look empty"
    assert row["stop_reason"] == "max_tokens"


def test_attempts_counts_api_round_trips(monkeypatch):
    row = _record(monkeypatch, "ask_elixir_daily", _Resp([_Block(type="text", text="hi")]))
    assert row["attempts"] == 1, "a clean call is one round trip"


def test_cost_is_priced_at_write_time(monkeypatch):
    """Stored rather than recomputed, so history stays correct when rates move."""
    row = _record(monkeypatch, "ask_elixir_daily", _Resp([_Block(type="text", text="hi")]))
    assert row["cost_usd"] is not None and row["cost_usd"] > 0


def test_cost_and_daily_counter_share_the_api_request_timestamp(monkeypatch):
    """A midnight boundary must not price one call on one day and charge another."""
    from agent import spend_budget

    seen = {}

    def _price(*args, effective_at):
        seen["priced_at"] = effective_at
        return 0.25

    def _record_cost(usd, *, now):
        seen["recorded_usd"] = usd
        seen["recorded_at"] = now

    monkeypatch.setattr(spend_budget, "call_cost_usd", _price)
    monkeypatch.setattr(spend_budget, "record_spend_usd", _record_cost)
    row = _record(monkeypatch, "ask_elixir_daily", _Resp([_Block(type="text", text="hi")]))

    assert row["cost_usd"] == 0.25
    assert seen["recorded_usd"] == 0.25
    assert seen["priced_at"] == seen["recorded_at"]
    assert seen["priced_at"].tzinfo is not None


def test_calls_in_one_turn_share_a_turn_id(monkeypatch):
    """A tool-using workflow makes several calls per turn. Without this, cost
    per turn is not computable — only cost per workflow per day."""
    monkeypatch.setattr(
        core, "_get_client", lambda: _Client(_Resp([_Block(type="text", text="hi")]))
    )
    monkeypatch.setattr(core.db, "record_llm_call", telemetry_store.record_llm_call)

    with core.turn():
        first = core.current_turn_id()
        core._create_chat_completion(
            workflow="interactive", messages=[{"role": "user", "content": "a"}]
        )
        core._create_chat_completion(
            workflow="interactive", messages=[{"role": "user", "content": "b"}]
        )

    conn = telemetry_store.connect()
    rows = conn.execute(
        "SELECT turn_id FROM llm_calls WHERE workflow='interactive' ORDER BY call_id DESC LIMIT 2"
    ).fetchall()
    assert first is not None
    assert {r["turn_id"] for r in rows} == {first}


def test_a_nested_turn_stays_part_of_the_outer_one():
    """The outer turn is what actually cost the money."""
    with core.turn():
        outer = core.current_turn_id()
        with core.turn():
            assert core.current_turn_id() == outer


def test_turn_id_is_cleared_after_the_block():
    with core.turn():
        assert core.current_turn_id() is not None
    assert core.current_turn_id() is None
