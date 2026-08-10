from __future__ import annotations

from datetime import datetime, timezone

from agent.pricing import call_cost_usd, price_call_row, price_for_model, summarize_call_rows
from scripts.llm_cost_report import build_report
from storage import telemetry
from storage.identity import _llm_cost_7d


def test_sonnet_5_introductory_rate_ends_at_september_utc_boundary():
    before = price_for_model("claude-sonnet-5", effective_at="2026-08-31T23:59:59Z")
    after = price_for_model("claude-sonnet-5", effective_at="2026-09-01T00:00:00Z")
    assert (before.input_per_million, before.output_per_million) == (2.0, 10.0)
    assert (after.input_per_million, after.output_per_million) == (3.0, 15.0)


def test_opus_5_and_cache_classes_use_published_multipliers():
    at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    price = price_for_model("claude-opus-5", effective_at=at)
    assert (price.input_per_million, price.output_per_million) == (5.0, 25.0)
    assert price.cache_read_per_million == 0.5
    assert price.cache_write_per_million == 6.25
    assert call_cost_usd("claude-opus-5", 0, 1_000_000, 0, 0, effective_at=at) == 25.0


def test_historical_rows_prefer_stored_cost_over_repricing():
    row = {
        "recorded_at": "2026-08-09T12:00:00Z",
        "model": "claude-opus-5",
        "ok": 1,
        "prompt_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 1.2345,
    }
    priced = price_call_row(row)
    assert priced.cost_usd == 1.2345
    assert priced.source == "stored"


def test_fallback_rows_use_the_rate_at_the_row_timestamp():
    rows = [
        {
            "recorded_at": "2026-08-31T23:59:59Z",
            "model": "claude-sonnet-5",
            "ok": 1,
            "prompt_tokens": 0,
            "completion_tokens": 1_000_000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": None,
        },
        {
            "recorded_at": "2026-09-01T00:00:00Z",
            "model": "claude-sonnet-5",
            "ok": 0,
            "prompt_tokens": 0,
            "completion_tokens": 1_000_000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": None,
        },
    ]
    assert summarize_call_rows(rows) == {
        "calls": 2,
        "failures": 1,
        "cost_usd": 25.0,
        "stored_cost_rows": 0,
        "fallback_cost_rows": 2,
        "inexact_model_rows": 0,
    }


def test_operator_report_uses_the_same_row_pricing_authority():
    rows = [
        {
            "recorded_at": "2026-08-09T12:00:00Z",
            "workflow": "memory_synthesis",
            "model": "claude-opus-5",
            "ok": 1,
            "prompt_tokens": 0,
            "completion_tokens": 1_000_000,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": None,
        }
    ]
    report = build_report(
        rows,
        days=7,
        cutoff="2026-08-02T12:00:00Z",
        extent={"first_call": rows[0]["recorded_at"], "last_call": rows[0]["recorded_at"]},
    )
    assert report["cost_usd"] == 25.0
    assert report["workflow_models"][0]["cost_usd"] == 25.0
    assert report["fallback_cost_rows"] == 1


def test_status_window_uses_an_exact_iso_z_cutoff():
    conn = telemetry.connect()
    rows = [
        ("2026-08-02T11:59:59Z", "claude-opus-5", None),
        ("2026-08-02T12:00:00Z", "claude-opus-5", None),
        ("2026-08-09T11:59:59Z", "claude-sonnet-5", 1.25),
    ]
    conn.executemany(
        "INSERT INTO llm_calls "
        "(recorded_at, workflow, model, ok, prompt_tokens, completion_tokens, "
        "cache_creation_tokens, cache_read_tokens, cost_usd) "
        "VALUES (?, 'test', ?, 1, 0, 1000000, 0, 0, ?)",
        rows,
    )
    conn.commit()

    summary = _llm_cost_7d(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
    assert summary == {
        "calls": 2,
        "failures": 0,
        "cost_usd": 26.25,
        "stored_cost_rows": 1,
        "fallback_cost_rows": 1,
        "inexact_model_rows": 0,
    }
