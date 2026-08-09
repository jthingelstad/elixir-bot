#!/usr/bin/env python3
"""Exercise a real workflow against the live API and show what the call did.

The tool that should have existed. Every ad-hoc harness written to answer a
question about a model call — "is thinking eating the budget?", "how many round
trips is this really making?", "did the ceiling change help?" — has been a
throwaway script that imported ``agent.core`` and called it directly. That works,
and it writes its results into the PRODUCTION telemetry database, because
``telemetry_path()`` defaults there whenever ``ELIXIR_TELEMETRY_DB_PATH`` is
unset. It happened four times (2026-07-10, 07-11, 08-03, 08-09) and left 21
fabricated rows skewing the averages the table exists to produce.

This isolates telemetry *by construction* — the env var is set before anything
imports the agent package, so there is no ordering to get wrong and nothing to
remember. Use this instead of writing another one-off.

    # What does one composition actually cost, and where do the tokens go?
    uv run --locked python scripts/probe_llm.py ask_elixir_daily

    # Any workflow reachable through the generic entry point:
    uv run --locked python scripts/probe_llm.py interactive --message "how is dez42 doing?"

    # Compare a policy change without touching the policy:
    uv run --locked python scripts/probe_llm.py ask_elixir_daily --effort low

Nothing here posts to Discord: it calls the composer, not the job that delivers
its output.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Before ANY project import. agent.core reaches telemetry through storage, and a
# late override would leave the first connection pointed at production.
_PROBE_DB = Path(tempfile.gettempdir()) / "elixir-probe-telemetry.db"
os.environ["ELIXIR_TELEMETRY_DB_PATH"] = str(_PROBE_DB)

sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

import agent.core as core  # noqa: E402


def _assert_isolated() -> None:
    """Fail loudly rather than write a fabricated row into production."""
    from storage import telemetry

    actual = telemetry.telemetry_path()
    if Path(actual).resolve() != _PROBE_DB.resolve():
        raise SystemExit(
            f"refusing to run: telemetry would go to {actual!r}, not the probe database. "
            "Something imported the agent package before this module set the override."
        )


def _compose(workflow: str, message: str):
    """Call the composer for a workflow. Composers return their output; the jobs
    that wrap them are what post to Discord, and those are not called here."""
    import elixir_agent

    if workflow == "ask_elixir_daily":
        from runtime.awareness import read as awareness_read

        return elixir_agent.generate_ask_elixir_daily(awareness_read.build_read())
    if workflow == "recruiting_copy":
        return elixir_agent.generate_promote_content({"requiredTrophies": 2000})
    # Generic: any workflow with a policy row, exercised through the funnel.
    resp = core._create_chat_completion(
        workflow=workflow, messages=[{"role": "user", "content": message}]
    )
    return core.response_text(resp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workflow", help="workflow name, e.g. ask_elixir_daily")
    parser.add_argument("--message", default="Reply with one short sentence.")
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        help="override the policy's effort for this run only (does not edit the policy)",
    )
    args = parser.parse_args()

    _assert_isolated()

    policy = core.policy_for(args.workflow)
    if args.effort:
        core.MODEL_CALL_POLICY[args.workflow] = policy._replace(effort=args.effort)
        policy = core.policy_for(args.workflow)

    print(f"probe: {args.workflow}")
    print(f"  model   {core._model_for_workflow(args.workflow)}")
    print(
        f"  policy  max_tokens={policy.max_tokens} effort={policy.effort} timeout={policy.timeout}s"
    )
    print(f"  telemetry -> {_PROBE_DB}\n")

    with core.turn():
        result = _compose(args.workflow, args.message)

    from storage import telemetry

    rows = (
        telemetry.connect()
        .execute(
            "SELECT workflow, model, effort, max_tokens, timeout_s, stop_reason, attempts, "
            "duration_ms, completion_tokens, cost_usd, block_census, turn_id "
            "FROM llm_calls ORDER BY call_id"
        )
        .fetchall()
    )

    print(f"{'#':>2}  {'stop':<10}{'out':>6}{'ms':>8}{'try':>4}{'cost$':>10}  blocks")
    print("-" * 78)
    for i, r in enumerate(rows, 1):
        print(
            f"{i:>2}  {str(r['stop_reason']):<10}{str(r['completion_tokens']):>6}"
            f"{r['duration_ms']:>8.0f}{r['attempts']:>4}"
            f"{(r['cost_usd'] or 0):>10.6f}  {r['block_census'] or '-'}"
        )

    total_out = sum(r["completion_tokens"] or 0 for r in rows)
    total_cost = sum(r["cost_usd"] or 0 for r in rows)
    wasted = sum((r["attempts"] or 1) - 1 for r in rows)
    print(
        f"\n  {len(rows)} call(s), {total_out} output tokens, ${total_cost:.6f}"
        + (f", {wasted} WASTED round trip(s)" if wasted else "")
    )
    truncated = [r for r in rows if r["stop_reason"] == "max_tokens"]
    if truncated:
        print(f"  TRUNCATED: {len(truncated)} call(s) hit max_tokens={policy.max_tokens}")

    print("\n--- composer result ---")
    if isinstance(result, dict) and "_error" in result:
        print(
            f"  ERROR {result['_error'].get('kind')}: {str(result['_error'].get('detail'))[:160]}"
        )
        return 1
    if result is None:
        print("  None (no output)")
        return 1
    preview = result.get("post") if isinstance(result, dict) else result
    print(f"  {str(preview)[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
