#!/usr/bin/env python3
"""Awareness-loop replay harness.

Pulls real signal payloads from the local elixir.db, constructs a series of
realistic "tick" scenarios, and runs each one through ``run_awareness_tick``
WITHOUT touching Discord. Dumps a structured report of every situation +
post plan to ``scripts/replay_awareness_report.md`` for human review.

This is a one-off integration test for Phase 4 of the awareness loop. It
exercises the real LLM (uses the ``ANTHROPIC_API_KEY`` from .env) so post
plans reflect what production would actually emit.

Usage:
    python scripts/replay_awareness.py [scenario_name ...]

If no scenario names are given, every scenario runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import elixir  # noqa: E402,F401  Seeds the runtime.app + jobs module graph.
import heartbeat  # noqa: E402
from agent.workflows import run_awareness_tick  # noqa: E402
from runtime.situation import (  # noqa: E402
    CHANNEL_LANES,
    HARD_POST_SIGNAL_TYPES,
    build_situation,
    classify_signal_lane,
    situation_is_quiet,
)


REPORT_PATH = REPO_ROOT / "scripts" / "replay_awareness_report.md"
DB_PATH = REPO_ROOT / "elixir.db"


# ---------------------------------------------------------------------------
# Signal extraction from the live db
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _payload_signals(row) -> list[dict]:
    payload = json.loads(row["payload_json"] or "{}")
    return payload.get("signals") or []


def latest_signals_of_type(conn, signal_type: str, limit: int = 1) -> list[dict]:
    """Return the most recent N raw signals of one type from signal_outcomes."""
    out: list[dict] = []
    seen_keys: set[str] = set()
    for row in conn.execute(
        """
        SELECT source_signal_key, payload_json
        FROM signal_outcomes
        WHERE source_signal_type = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (signal_type, limit * 4),
    ):
        if row["source_signal_key"] in seen_keys:
            continue
        seen_keys.add(row["source_signal_key"])
        for sig in _payload_signals(row):
            if sig.get("type") == signal_type:
                out.append(sig)
                break
        if len(out) >= limit:
            break
    return out


def latest_war_signal(conn, signal_type: str) -> dict | None:
    sigs = latest_signals_of_type(conn, signal_type, limit=1)
    return sigs[0] if sigs else None


def latest_member_signal(conn, signal_type: str, limit: int = 2) -> list[dict]:
    return latest_signals_of_type(conn, signal_type, limit=limit)


# ---------------------------------------------------------------------------
# Live clan / war state pulls (read-only, no LLM)
# ---------------------------------------------------------------------------

def load_live_war_payload(conn) -> dict:
    """Return the most recent war_current_state raw_json so the situation
    assembler has real standings/clan data to work with."""
    row = conn.execute(
        "SELECT raw_json FROM war_current_state ORDER BY war_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["raw_json"]) or {}
    except (TypeError, json.JSONDecodeError):
        return {}


def load_clan_metadata(conn) -> dict:
    war = load_live_war_payload(conn)
    return war.get("clan") or {}


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    description: str
    signal_builder: Any  # callable(conn) -> list[dict]


def scenario_quiet_tick(conn) -> list[dict]:
    return []


def scenario_single_hot_streak(conn) -> list[dict]:
    return latest_signals_of_type(conn, "battle_hot_streak", limit=1)


def scenario_battle_day_complete(conn) -> list[dict]:
    sig = latest_war_signal(conn, "war_battle_day_complete")
    return [sig] if sig else []


def scenario_member_join_pair(conn) -> list[dict]:
    return latest_member_signal(conn, "member_join", limit=2)


def scenario_card_unlock(conn) -> list[dict]:
    return latest_signals_of_type(conn, "new_card_unlocked", limit=1)


def scenario_arena_change(conn) -> list[dict]:
    return latest_signals_of_type(conn, "arena_change", limit=1)


def scenario_path_of_legend(conn) -> list[dict]:
    return latest_signals_of_type(conn, "path_of_legend_promotion", limit=1)


def scenario_busy_mixed_tick(conn) -> list[dict]:
    """The realistic case the awareness loop is designed for: war + battle
    mode + milestone + roster all hit in the same tick."""
    out: list[dict] = []
    for sig_type in (
        "war_battle_day_complete",
        "battle_hot_streak",
        "new_card_unlocked",
        "member_join",
    ):
        sigs = latest_signals_of_type(conn, sig_type, limit=1)
        out.extend(sigs)
    return out


def scenario_war_completion_cascade(conn) -> list[dict]:
    """The actual 2026-04-13T10 cascade: practice phase + battle days complete
    + week rollover + war completed all in one cycle."""
    out: list[dict] = []
    for sig_type in (
        "war_battle_day_complete",
        "war_battle_days_complete",
        "war_week_rollover",
        "war_completed",
        "war_practice_phase_active",
    ):
        sig = latest_war_signal(conn, sig_type)
        if sig:
            out.append(sig)
    return out


def scenario_capability_unlock(conn) -> list[dict]:
    return latest_signals_of_type(conn, "capability_unlock", limit=1)


SCENARIOS: list[Scenario] = [
    Scenario(
        "quiet_tick",
        "No signals, mid-practice-day. Should produce zero posts (fast-path skip).",
        scenario_quiet_tick,
    ),
    Scenario(
        "single_hot_streak",
        "One battle_hot_streak signal. Should produce one #trophy-road post that "
        "ideally cites opponent evidence via cr_api lookup.",
        scenario_single_hot_streak,
    ),
    Scenario(
        "battle_day_complete",
        "One war_battle_day_complete signal. Hard-floor; must produce a #river-race post.",
        scenario_battle_day_complete,
    ),
    Scenario(
        "member_join_pair",
        "Two new members joined. Hard-floor for both. Should land on #clan-events "
        "(public welcome) and #leader-lounge (ops note).",
        scenario_member_join_pair,
    ),
    Scenario(
        "card_unlock",
        "Durable milestone. Should land on #player-progress (NOT #trophy-road).",
        scenario_card_unlock,
    ),
    Scenario(
        "arena_change",
        "Durable milestone. Should land on #player-progress.",
        scenario_arena_change,
    ),
    Scenario(
        "path_of_legend",
        "Volatile battle-mode signal (Phase 3 reroute). Should land on #trophy-road, "
        "NOT #player-progress.",
        scenario_path_of_legend,
    ),
    Scenario(
        "busy_mixed_tick",
        "War + battle-mode + milestone + roster all in one tick. Tests lane "
        "discipline and coherent timing across multiple channels.",
        scenario_busy_mixed_tick,
    ),
    Scenario(
        "war_completion_cascade",
        "End-of-week war cascade: battle day complete + week rollover + war complete + "
        "next practice phase active. Should narratively sequence, not fire 4 separate posts.",
        scenario_war_completion_cascade,
    ),
    Scenario(
        "capability_unlock",
        "System signal. Hard-floor; should land on #announcements.",
        scenario_capability_unlock,
    ),
]


# ---------------------------------------------------------------------------
# Plan validation (mirrors the real delivery layer's checks)
# ---------------------------------------------------------------------------

def validate_plan(plan: dict, situation: dict) -> dict:
    """Return a validation report: lane mismatches, hard-floor coverage, etc."""
    posts = (plan or {}).get("posts") or []
    issues: list[str] = []
    covered: set[str] = set()
    for i, post in enumerate(posts):
        channel = (post.get("channel") or "").strip()
        leads_with = (post.get("leads_with") or "").strip()
        if channel not in CHANNEL_LANES:
            issues.append(f"post[{i}]: unknown channel {channel!r}")
            continue
        if leads_with and leads_with not in CHANNEL_LANES[channel]:
            issues.append(
                f"post[{i}]: leads_with={leads_with!r} not allowed on #{channel} "
                f"(allowed: {sorted(CHANNEL_LANES[channel])})"
            )
        if not post.get("content"):
            issues.append(f"post[{i}]: empty content")
        for key in post.get("covers_signal_keys") or []:
            if key:
                covered.add(str(key))

    hard_required = situation.get("hard_post_signals") or []
    uncovered = [hp for hp in hard_required if hp.get("signal_key") not in covered]
    return {
        "post_count": len(posts),
        "issues": issues,
        "hard_floor_total": len(hard_required),
        "hard_floor_covered": len(hard_required) - len(uncovered),
        "hard_floor_uncovered": uncovered,
    }


# ---------------------------------------------------------------------------
# Patching: keep DB reads real but block any accidental Discord/network calls
# ---------------------------------------------------------------------------

class _NoDiscord:
    """Block any unexpected Discord posts. The harness must not send."""

    def __getattr__(self, name):
        def _noop(*a, **kw):
            raise RuntimeError(f"Discord call blocked in replay harness: {name}")
        return _noop


# ---------------------------------------------------------------------------
# Run one scenario end-to-end
# ---------------------------------------------------------------------------

def run_scenario(scenario: Scenario, conn) -> dict:
    signals = scenario.signal_builder(conn) or []
    war = load_live_war_payload(conn)
    clan = load_clan_metadata(conn)

    bundle = heartbeat.HeartbeatTickResult(signals=signals, clan=clan, war=war)
    situation = build_situation(bundle)
    is_quiet = situation_is_quiet(situation)

    if is_quiet:
        return {
            "scenario": scenario,
            "signals": signals,
            "situation": situation,
            "is_quiet": True,
            "plan": {"posts": [], "skipped_reason": "fast-path: situation is quiet"},
            "validation": {
                "post_count": 0,
                "issues": [],
                "hard_floor_total": 0,
                "hard_floor_covered": 0,
                "hard_floor_uncovered": [],
            },
            "error": None,
        }

    # Real LLM call. No Discord patches needed because run_awareness_tick
    # itself never touches Discord — it only returns a post plan.
    error = None
    plan: dict | None = None
    try:
        plan = run_awareness_tick(situation)
    except Exception as exc:
        error = repr(exc)

    if plan is None:
        plan = {"posts": [], "skipped_reason": "agent returned None"}

    return {
        "scenario": scenario,
        "signals": signals,
        "situation": situation,
        "is_quiet": False,
        "plan": plan,
        "validation": validate_plan(plan, situation),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _short(value: Any, width: int = 80) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def render_report(results: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"# Awareness loop replay report — {datetime.now().isoformat(timespec='seconds')}\n")
    total = len(results)
    quiet = sum(1 for r in results if r["is_quiet"])
    errors = sum(1 for r in results if r["error"])
    issues = sum(len(r["validation"]["issues"]) for r in results)
    floor_gap = sum(len(r["validation"]["hard_floor_uncovered"]) for r in results)
    lines.append(
        f"**Summary:** {total} scenarios · {quiet} quiet (skipped) · {errors} errors · "
        f"{issues} validation issues · {floor_gap} uncovered hard-post floors\n"
    )

    for r in results:
        s = r["scenario"]
        lines.append(f"\n---\n\n## `{s.name}`\n")
        lines.append(f"_{s.description}_\n")
        lines.append("### Input signals\n")
        if not r["signals"]:
            lines.append("(none)\n")
        else:
            for sig in r["signals"]:
                lines.append(
                    f"- **{sig.get('type')}** "
                    f"tag={sig.get('tag', '—')} "
                    f"name={sig.get('name', '—')} "
                    f"lane=`{classify_signal_lane(sig)}`"
                )
            lines.append("")

        sit = r["situation"]
        time_block = sit.get("time") or {}
        standing = sit.get("standing") or {}
        lines.append("### Situation snapshot\n")
        lines.append(f"- time: phase={time_block.get('phase')} day={time_block.get('day_number')}/{time_block.get('day_total')} hrs_left={time_block.get('hours_remaining_in_day')} colosseum={time_block.get('is_colosseum_week')}")
        lines.append(f"- standing: rank={standing.get('rank')} fame={standing.get('fame')} deficit={standing.get('deficit_to_leader')}")
        lines.append(f"- hard_post_signals: {len(sit.get('hard_post_signals') or [])}")
        lines.append(f"- roster_vitals: {len(sit.get('roster_vitals') or [])} entries\n")

        if r["is_quiet"]:
            lines.append("**Result:** quiet tick → fast-path skip (no LLM call). ✅\n")
            continue

        if r["error"]:
            lines.append(f"**ERROR:** `{r['error']}`\n")
            continue

        v = r["validation"]
        lines.append("### Validation\n")
        lines.append(f"- posts: {v['post_count']}")
        lines.append(f"- hard-floor coverage: {v['hard_floor_covered']}/{v['hard_floor_total']}")
        if v["hard_floor_uncovered"]:
            lines.append(f"- ⚠️  uncovered: {[hp.get('type') for hp in v['hard_floor_uncovered']]}")
        if v["issues"]:
            for issue in v["issues"]:
                lines.append(f"- ⚠️  {issue}")
        if not v["issues"] and not v["hard_floor_uncovered"]:
            lines.append("- ✅ all checks pass")
        lines.append("")

        plan = r["plan"]
        posts = plan.get("posts") or []
        lines.append("### Post plan\n")
        if plan.get("skipped_reason"):
            lines.append(f"_skipped_reason:_ {plan['skipped_reason']}\n")
        if not posts:
            lines.append("_(no posts emitted)_\n")
        for i, post in enumerate(posts):
            lines.append(f"#### Post {i+1} → `#{post.get('channel')}`")
            lines.append(f"- leads_with: `{post.get('leads_with')}` · tone: `{post.get('tone')}`")
            lines.append(f"- summary: {post.get('summary') or '—'}")
            lines.append(f"- covers_signal_keys: {post.get('covers_signal_keys') or []}")
            content = post.get("content")
            if isinstance(content, list):
                for j, part in enumerate(content):
                    lines.append(f"\n**Body part {j+1}:**\n\n{textwrap.indent(str(part), '> ')}\n")
            else:
                lines.append(f"\n**Body:**\n\n{textwrap.indent(str(content or ''), '> ')}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", help="optional list of scenario names to run")
    parser.add_argument("--report", default=str(REPORT_PATH), help="path to write markdown report")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("CLAUDE_API_KEY"):
        # Try loading from .env
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() and val.strip():
                    os.environ.setdefault(key.strip(), val.strip())
    # Anthropic SDK looks for ANTHROPIC_API_KEY; copy from CLAUDE_API_KEY if needed.
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]

    conn = _connect()
    selected = SCENARIOS
    if args.scenarios:
        selected = [s for s in SCENARIOS if s.name in args.scenarios]
        unknown = set(args.scenarios) - {s.name for s in SCENARIOS}
        if unknown:
            print(f"Unknown scenarios: {unknown}", file=sys.stderr)
            return 2

    print(f"Running {len(selected)} scenario(s)…")
    results: list[dict] = []
    for scenario in selected:
        print(f"  · {scenario.name} … ", end="", flush=True)
        result = run_scenario(scenario, conn)
        results.append(result)
        if result["is_quiet"]:
            print("quiet (skipped)")
        elif result["error"]:
            print(f"ERROR: {result['error']}")
        else:
            v = result["validation"]
            tag = "ok" if not v["issues"] and not v["hard_floor_uncovered"] else "issues"
            print(f"posts={v['post_count']} {tag}")

    report = render_report(results)
    Path(args.report).write_text(report)
    print(f"\nReport written → {args.report}")

    # Exit non-zero if any uncovered hard floors or validation issues, so this
    # can gate a CI loop or release decision.
    bad = any(r["error"] or r["validation"]["issues"] or r["validation"]["hard_floor_uncovered"]
              for r in results)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
