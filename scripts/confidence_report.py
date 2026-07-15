#!/usr/bin/env python3
"""The one-command confidence surface (confidence plan capstone).

Answers "is Elixir healthy?" in one shot by unifying the three pillars:
  1. open incidents          (runtime_incidents — the fail-soft ledger)
  2. confidence test status   (entrypoint smoke + lane + cold-start + pipeline)
  3. latest post-quality      (scripts/eval_post_quality — game-accuracy + depth)

Exit code is NON-ZERO when there are findings, so a cron/launchd wrapper or the
unattended `confidence-monitor` routine knows to act. `--json` emits a machine
object for an agent to triage.

    ./venv/bin/python scripts/confidence_report.py           # human summary
    ./venv/bin/python scripts/confidence_report.py --json    # agent-readable
    ./venv/bin/python scripts/confidence_report.py --quick   # skip the LLM eval
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

os.environ.setdefault(
    "ELIXIR_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "elixir-v51.db")
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIDENCE_TESTS = [
    "tests/test_entrypoints_smoke.py",
    "tests/test_lane_registration.py",
    "tests/test_cold_start_tick.py",
    "tests/test_pipeline_integration.py",
]


def _incidents() -> list[dict]:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(REPO, ".env"))
    import db
    from storage.incidents import open_incidents

    conn = db.get_connection()
    try:
        return [
            {
                "at": r["at"],
                "component": r["component"],
                "severity": r["severity"],
                "summary": r["summary"],
            }
            for r in open_incidents(conn, limit=100)
        ]
    finally:
        conn.close()


def _liveness() -> list[str]:
    """Silence signals — no output, or leader-actions recommended-but-never-posted.
    Silence is an alarm, not the absence of one (Jamie, 2026-07-05)."""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(REPO, ".env"))
    import db
    from runtime.health import check_output_silence

    conn = db.get_connection()
    try:
        return check_output_silence(conn)
    except Exception as exc:  # noqa: BLE001
        return [f"liveness check failed: {exc!r}"]
    finally:
        conn.close()


def _run_tests() -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *_CONFIDENCE_TESTS],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    failures = [ln for ln in proc.stdout.splitlines() if ln.startswith("FAILED")]
    return {"ok": proc.returncode == 0, "summary": tail, "failures": failures}


def _quality(days: int, quick: bool) -> dict:
    try:
        from scripts.eval_post_quality import run_eval
    except Exception as exc:  # eval not built / import error
        return {"available": False, "reason": str(exc)}
    try:
        return {"available": True, **run_eval(days=days, use_llm=not quick)}
    except Exception as exc:
        return {"available": True, "error": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quick", action="store_true", help="skip the LLM depth eval")
    ap.add_argument("--days", type=int, default=3, help="post-quality sample window")
    args = ap.parse_args()

    incidents = _incidents()
    liveness = _liveness()
    tests = _run_tests()
    quality = _quality(args.days, args.quick)

    findings = (
        len(incidents)
        + len(liveness)
        + (0 if tests["ok"] else 1)
        + len(quality.get("flagged", []) if quality.get("available") else [])
    )
    report = {
        "healthy": findings == 0,
        "incidents": incidents,
        "liveness": liveness,
        "tests": tests,
        "quality": quality,
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"{'✅ HEALTHY' if report['healthy'] else '⚠️  FINDINGS'}\n")
        if liveness:
            print("Liveness / silence:")
            for s in liveness:
                print(f"  ⚠️  {s}")
            print()
        print(f"Open incidents: {len(incidents)}")
        for i in incidents[:15]:
            print(
                f"  [{i['at'][5:16]}] {i['severity']:5} {i['component']}: {i['summary'][:80]}"
            )
        print(
            f"\nConfidence tests: {'PASS' if tests['ok'] else 'FAIL'} — {tests['summary']}"
        )
        for f in tests["failures"]:
            print(f"  {f}")
        if quality.get("available"):
            ga = quality.get("game_accuracy_rate")
            print(
                f"\nPost quality ({quality.get('sampled', 0)} posts, {args.days}d): "
                f"game-accuracy {ga if ga is not None else 'n/a'}, "
                f"{len(quality.get('flagged', []))} flagged"
            )
            for fl in quality.get("flagged", [])[:5]:
                print(f"  flag: {str(fl)[:100]}")
        else:
            print(f"\nPost quality: unavailable ({quality.get('reason', '?')})")

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
