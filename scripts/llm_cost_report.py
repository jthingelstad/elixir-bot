#!/usr/bin/env python3
"""Read-only LLM cost report using Elixir's canonical date-aware pricing."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "elixir-telemetry.db"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(path: Path, cutoff: str) -> tuple[list[dict], dict]:
    conn = _read_only_connection(path)
    try:
        extent = dict(
            conn.execute(
                "SELECT MIN(recorded_at) AS first_call, MAX(recorded_at) AS last_call, "
                "COUNT(*) AS lifetime_calls FROM llm_calls"
            ).fetchone()
        )
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT call_id, recorded_at, workflow, model, ok, prompt_tokens, "
                "completion_tokens, cache_creation_tokens, cache_read_tokens, cost_usd "
                "FROM llm_calls WHERE recorded_at >= ? ORDER BY recorded_at, call_id",
                (cutoff,),
            )
        ]
        return rows, extent
    finally:
        conn.close()


def build_report(rows: list[dict], *, days: int, cutoff: str, extent: dict) -> dict:
    from agent.pricing import price_call_row, summarize_call_rows

    summary = summarize_call_rows(rows)
    by_day = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    by_workflow_model = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    cache_by_workflow = defaultdict(lambda: {"cache_write_tokens": 0, "cache_read_tokens": 0})
    unknown_models: set[str] = set()

    for row in rows:
        priced = price_call_row(row)
        day = (row.get("recorded_at") or "unknown")[:10]
        workflow = row.get("workflow") or "unknown"
        model = row.get("model") or "unknown"
        day_item = by_day[day]
        day_item["calls"] += 1
        day_item["cost_usd"] += priced.cost_usd
        workflow_item = by_workflow_model[(workflow, model)]
        workflow_item["calls"] += 1
        workflow_item["cost_usd"] += priced.cost_usd
        cache_item = cache_by_workflow[workflow]
        cache_item["cache_write_tokens"] += int(row.get("cache_creation_tokens") or 0)
        cache_item["cache_read_tokens"] += int(row.get("cache_read_tokens") or 0)
        if not priced.exact_model:
            unknown_models.add(model)

    daily = [
        {"day": day, "calls": item["calls"], "cost_usd": round(item["cost_usd"], 4)}
        for day, item in sorted(by_day.items())
    ]
    workflows = [
        {
            "workflow": workflow,
            "model": model,
            "calls": item["calls"],
            "cost_usd": round(item["cost_usd"], 4),
        }
        for (workflow, model), item in by_workflow_model.items()
    ]
    workflows.sort(key=lambda item: (-item["cost_usd"], item["workflow"], item["model"]))
    cache = []
    for workflow, item in cache_by_workflow.items():
        writes = item["cache_write_tokens"]
        reads = item["cache_read_tokens"]
        cache.append(
            {
                "workflow": workflow,
                **item,
                "read_per_write": round(reads / writes, 4) if writes else None,
            }
        )
    cache.sort(key=lambda item: (-item["cache_write_tokens"], item["workflow"]))

    return {
        "window_days": days,
        "cutoff": cutoff,
        **extent,
        **summary,
        "projected_monthly_usd": round(summary["cost_usd"] / days * 30, 2),
        "daily": daily,
        "workflow_models": workflows,
        "cache_efficiency": cache,
        "unknown_fallback_models": sorted(unknown_models),
    }


def _print_report(report: dict) -> None:
    print(
        f"{report['window_days']}d: ${report['cost_usd']:.4f} across "
        f"{report['calls']} calls ({report['failures']} failures), "
        f"projected ${report['projected_monthly_usd']:.2f}/month"
    )
    print(
        f"cost receipts: {report['stored_cost_rows']} stored, "
        f"{report['fallback_cost_rows']} timestamp-priced fallback"
    )
    if report["unknown_fallback_models"]:
        print("WARNING unknown model fallback: " + ", ".join(report["unknown_fallback_models"]))
    print("\nTop workflow/model costs")
    for item in report["workflow_models"][:10]:
        print(f"{item['cost_usd']:>9.4f}  {item['calls']:>4}  {item['workflow']} / {item['model']}")
    print("\nDaily")
    for item in report["daily"]:
        print(f"{item['day']}  ${item['cost_usd']:.4f}  {item['calls']} calls")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error("--days must be positive")
    now = datetime.now(timezone.utc)
    cutoff = _iso_z(now - timedelta(days=args.days))
    rows, extent = load_rows(args.db, cutoff)
    report = build_report(rows, days=args.days, cutoff=cutoff, extent=extent)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
