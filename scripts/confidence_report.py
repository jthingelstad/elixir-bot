#!/usr/bin/env python3
"""The one-command confidence surface (confidence plan capstone).

Answers "is Elixir healthy?" in one shot by unifying the three pillars:
  1. recent errors            (logs/elixir-error.log — the ERROR-only log)
  2. confidence test status   (entrypoint smoke + lane + cold-start + pipeline)
  3. latest post-quality      (scripts/eval_post_quality — game-accuracy + depth)

Pillar 1 used to read `runtime_incidents`, a ledger that recorded 0 rows in 25
days while the log held 159 real errors — so this report said "healthy" through
every actual failure. The ledger was retired 2026-07-28 (schema v20); the log
was always the record, and this now reads it directly.

Exit code is NON-ZERO when there are findings, so the external Operations and
Quality Manager automations know to act. `--json` emits a machine object for an
agent to triage. The report is read-only: it evaluates production evidence but
does not turn its own findings into product memories or another work queue.

    uv run python scripts/confidence_report.py           # human summary
    uv run python scripts/confidence_report.py --json    # agent-readable
    uv run python scripts/confidence_report.py --quick   # skip the LLM eval
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
    # tests/test_pipeline_integration.py was deleted with the recognition stack
    # in #207 but left in this list, so pytest exited 4 ("file not found") and
    # this pillar reported FAIL on every run since. Same class of bug as the
    # incident ledger this report used to read: a signal nobody validated.
]


_LOG_LINE = re.compile(
    r"^(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[,.]\d+ "
    r"\[(?P<level>[A-Z]+)\] (?P<logger>[^:]+): (?P<message>.*)$"
)

# Call sites log `<component> failed: k=v k=v`, so the varying part is exactly
# the context that makes each occurrence unique. Grouping on the raw message
# would report one broken path firing 40 times as 40 findings — the opposite of
# the point. Collapse the values, keep the shape.
_SHAPE_SUBS = (
    (re.compile(r"(?<==)\S+"), "*"),  # k=v values (thread_id=123, lane=elixir)
    (re.compile(r"#[0-9A-Z]{3,}"), "#TAG"),  # CR player/clan tags
    (re.compile(r"\b\d[\d.,:+-]*\b"), "N"),  # bare ids, counts, timestamps
)


def _shape(message: str) -> str:
    """A stable grouping key: the message with its varying values collapsed."""
    for pattern, replacement in _SHAPE_SUBS:
        message = pattern.sub(replacement, message)
    return message[:160]


def _errors(hours: int = 24) -> list[dict]:
    """Distinct error kinds in the last `hours` of logs/elixir-error.log.

    Grouped by (logger, message *shape*) so 40 repeats of one broken path read
    as one finding with a count, not 40 findings. `message` is the collapsed
    shape, `sample` a real line from it, `last` what says still-firing.
    """
    import datetime as _dt

    from runtime.logging_setup import error_log_path

    path = error_log_path()
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not os.path.exists(path):
        # A missing operator interface is itself a finding. Same shape as a real
        # group so every consumer can read `last` without special-casing.
        return [
            {
                "logger": "confidence_report",
                "message": f"no error log at {path} — logging may not be configured",
                "sample": f"no error log at {path}",
                "count": 1,
                "first": now,
                "last": now,
            }
        ]

    cutoff = (_dt.datetime.now() - _dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    grouped: dict[tuple[str, str], dict] = {}
    with open(path, errors="replace") as handle:
        for line in handle:
            match = _LOG_LINE.match(line.rstrip("\n"))
            if not match or match["at"] < cutoff:
                continue  # continuation/traceback lines ride with their header
            shape = _shape(match["message"])
            entry = grouped.setdefault(
                (match["logger"], shape),
                {
                    "logger": match["logger"],
                    "message": shape,
                    "sample": match["message"][:200],
                    "count": 0,
                    "first": match["at"],
                    "last": match["at"],
                },
            )
            entry["count"] += 1
            # min/max rather than "last line wins": a real log is chronological,
            # but `last` is the field triage turns on, so don't make it depend on
            # that.
            entry["first"] = min(entry["first"], match["at"])
            if match["at"] >= entry["last"]:
                entry["last"] = match["at"]
                entry["sample"] = match["message"][:200]  # most recent occurrence
    return sorted(grouped.values(), key=lambda e: e["last"], reverse=True)


# The once-daily 09:05 CT awareness cron needs the same two-hour grace that
# the former twice-daily cadence had (12h + 2h = 14h).  Otherwise the report
# raises a false liveness alarm every night between the morning decisions.
AWARENESS_DECISION_STALE_HOURS = 26
LEADER_ACTION_STALE_HOURS = 2  # a proposed card unposted this long → posting broken


def _liveness() -> list[str]:
    """Liveness signals — no successful awareness decision, or stuck leader actions.
    Silence is an alarm, not the absence of one (Jamie, 2026-07-05).

    These two queries lived in `runtime/health.py` until the daily health check
    was retired. They stay here because they are the one thing that check saw
    that a log never will: an error log cannot report a failure that produced no
    error, and both of these signatures are *quiet*. This is an operator script,
    which is where the watching belongs.
    """
    from scripts.read_only_db import connect_read_only

    conn = connect_read_only()
    try:
        problems = []
        # (a) Awareness owns proactive output and may deliberately stay quiet.
        # A terminal decision (a real post or deliberate silence) proves the loop
        # is alive; failed plans must not mask a stalled loop.
        row = conn.execute(
            "SELECT MAX(at) FROM awareness_thoughts "
            "WHERE (chose_silence = 1 OR post_count > 0) "
            "AND json_extract(plan_json, '$._error') IS NULL"
        ).fetchone()
        last = row[0] if row else None
        if last is None:
            problems.append(
                "no successful awareness decision recorded — awareness may be silently stuck"
            )
        else:
            hrs = conn.execute(
                "SELECT ROUND((julianday('now') - julianday(?)) * 24, 1)", (last,)
            ).fetchone()[0]
            if hrs is not None and hrs > AWARENESS_DECISION_STALE_HOURS:
                problems.append(
                    f"no successful awareness decision in {hrs}h (last decision {last}) "
                    "— awareness may be silently stuck"
                )
        # (b) the can_post_leader_action signature: proposed but never posted.
        stuck = conn.execute(
            "SELECT COUNT(*) FROM leader_action_recommendations "
            "WHERE status = 'proposed' "
            "AND (source_message_id IS NULL OR source_message_id = 'posting') "
            "AND COALESCE(is_test, 0) = 0 AND proposed_at < "
            "strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
            (f"-{LEADER_ACTION_STALE_HOURS} hours",),
        ).fetchone()[0]
        if stuck:
            problems.append(
                f"{stuck} leader-action(s) proposed >{LEADER_ACTION_STALE_HOURS}h ago but never "
                f"posted — card posting may be broken"
            )
        return problems
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
        return {
            "available": True,
            **run_eval(days=days, use_llm=not quick, record_feedback=False),
        }
    except Exception as exc:
        return {"available": True, "error": str(exc)}


def _finding_count(errors: list[dict], liveness: list[str], tests: dict, quality: dict) -> int:
    """Count every failed pillar; unavailable/broken quality must not read healthy."""
    quality_failed = not quality.get("available") or bool(quality.get("error"))
    return (
        len(errors)
        + len(liveness)
        + (0 if tests["ok"] else 1)
        + (1 if quality_failed else 0)
        + len(quality.get("flagged", []) if quality.get("available") else [])
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quick", action="store_true", help="skip the LLM depth eval")
    ap.add_argument("--days", type=int, default=3, help="post-quality sample window")
    ap.add_argument("--error-hours", type=int, default=24, help="error-log lookback window")
    args = ap.parse_args()

    errors = _errors(args.error_hours)
    liveness = _liveness()
    tests = _run_tests()
    quality = _quality(args.days, args.quick)

    findings = _finding_count(errors, liveness, tests, quality)
    report = {
        "healthy": findings == 0,
        "finding_count": findings,
        "errors": errors,
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
        print(f"Error kinds in the last {args.error_hours}h: {len(errors)}")
        for e in errors[:15]:
            print(f"  [last {e['last'][5:16]}] x{e['count']:<4} {e['logger']}: {e['sample'][:80]}")
        print(f"\nConfidence tests: {'PASS' if tests['ok'] else 'FAIL'} — {tests['summary']}")
        for f in tests["failures"]:
            print(f"  {f}")
        if quality.get("error"):
            print(f"\nPost quality: ERROR ({quality['error']})")
        elif quality.get("available"):
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
