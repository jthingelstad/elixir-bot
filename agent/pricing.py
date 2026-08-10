"""Canonical Anthropic token pricing for runtime charging and admin reports.

Prices are USD per million tokens. A call is quoted at its start timestamp so a
rate boundary cannot charge the same API request two different ways. Persisted
``cost_usd`` values remain the historical receipt; reporting only falls back to
these rates for rows written before per-call cost capture existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10
SONNET_5_STANDARD_START = datetime(2026, 9, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    source: str
    exact_model: bool = True

    @property
    def cache_read_per_million(self) -> float:
        return self.input_per_million * CACHE_READ_MULTIPLIER

    @property
    def cache_write_per_million(self) -> float:
        return self.input_per_million * CACHE_WRITE_MULTIPLIER


@dataclass(frozen=True)
class PricedCall:
    cost_usd: float
    source: str
    exact_model: bool


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_model(model: str | None) -> str:
    name = (model or "").strip().lower()
    if name.startswith("codex-"):
        return f"claude-{name.removeprefix('codex-')}"
    return name


def price_for_model(model: str | None, *, effective_at: datetime | str | None = None) -> ModelPrice:
    """Return the effective price, including dated promotional boundaries.

    Unknown production models retain the old conservative Sonnet-standard
    fallback, but the quote is marked inexact so admin reports can surface it.
    Test models are deliberately free.
    """
    name = _canonical_model(model)
    at = _as_utc(effective_at)

    if name.startswith("claude-test-"):
        return ModelPrice(0.0, 0.0, "test_model")
    if name.startswith("claude-sonnet-5"):
        if at < SONNET_5_STANDARD_START:
            return ModelPrice(2.0, 10.0, "sonnet_5_intro_through_2026_08_31")
        return ModelPrice(3.0, 15.0, "sonnet_5_standard_from_2026_09_01")
    if name.startswith(("claude-opus-5", "claude-opus-4-8")):
        return ModelPrice(5.0, 25.0, "opus_standard")
    if name.startswith("claude-sonnet-"):
        return ModelPrice(3.0, 15.0, "sonnet_standard")
    if name.startswith("claude-haiku-"):
        return ModelPrice(1.0, 5.0, "haiku_standard")
    return ModelPrice(3.0, 15.0, "unknown_model_sonnet_standard_fallback", exact_model=False)


def call_cost_usd(
    model: str,
    prompt_tokens,
    completion_tokens,
    cache_creation_tokens,
    cache_read_tokens,
    *,
    effective_at: datetime | str | None = None,
) -> float:
    price = price_for_model(model, effective_at=effective_at)
    return (
        (prompt_tokens or 0) * price.input_per_million
        + (cache_creation_tokens or 0) * price.cache_write_per_million
        + (cache_read_tokens or 0) * price.cache_read_per_million
        + (completion_tokens or 0) * price.output_per_million
    ) / 1_000_000


def price_call_row(row: Mapping) -> PricedCall:
    """Prefer a row's immutable receipt, otherwise price it at ``recorded_at``."""
    stored = row.get("cost_usd")
    if stored is not None:
        return PricedCall(float(stored), "stored", True)
    price = price_for_model(row.get("model"), effective_at=row.get("recorded_at"))
    return PricedCall(
        call_cost_usd(
            row.get("model") or "",
            row.get("prompt_tokens"),
            row.get("completion_tokens"),
            row.get("cache_creation_tokens"),
            row.get("cache_read_tokens"),
            effective_at=row.get("recorded_at"),
        ),
        "fallback",
        price.exact_model,
    )


def summarize_call_rows(rows) -> dict:
    calls = failures = stored_rows = fallback_rows = inexact_rows = 0
    cost_usd = 0.0
    for raw_row in rows:
        row = dict(raw_row)
        priced = price_call_row(row)
        calls += 1
        failures += int(not bool(row.get("ok")))
        stored_rows += int(priced.source == "stored")
        fallback_rows += int(priced.source == "fallback")
        inexact_rows += int(not priced.exact_model)
        cost_usd += priced.cost_usd
    return {
        "calls": calls,
        "failures": failures,
        "cost_usd": round(cost_usd, 4),
        "stored_cost_rows": stored_rows,
        "fallback_cost_rows": fallback_rows,
        "inexact_model_rows": inexact_rows,
    }


__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "ModelPrice",
    "PricedCall",
    "SONNET_5_STANDARD_START",
    "call_cost_usd",
    "price_call_row",
    "price_for_model",
    "summarize_call_rows",
]
