#!/usr/bin/env python3
"""Review Elixir's own data for improvement opportunities.

Default mode is shadow-safe: collect opportunities, store them idempotently in
SQLite, and print a maintainer report. GitHub promotion is dry-run unless
--write-github is explicitly provided.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db

DEFAULT_REPO = "jthingelstad/elixir-bot"
DEFAULT_PROMOTION_CONFIDENCE = 0.72


def _cutoff(days: int) -> str:
    dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, int(days or 1)))
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _truncate(value, limit: int = 240) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _loads_dict(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _loads_list(value) -> list:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return loaded if isinstance(loaded, list) else []


def _has_table(conn, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _leader_action_feedback_spec(conn, *, days: int) -> dict | None:
    if not _has_table(conn, "leader_action_recommendations"):
        return None
    cutoff = _cutoff(days)
    rows = conn.execute(
        """
        SELECT *
        FROM leader_action_recommendations
        WHERE COALESCE(is_test, 0) = 0
          AND COALESCE(decision_note_at, decided_at, updated_at, proposed_at) >= ?
          AND (
            decision_note IS NOT NULL
            OR status IN ('rejected', 'deferred')
            OR copy_edit_diff_json IS NOT NULL
          )
        ORDER BY COALESCE(decision_note_at, decided_at, updated_at, proposed_at) DESC, action_id DESC
        LIMIT 75
        """,
        (cutoff,),
    ).fetchall()
    channel_comments = []
    if _has_table(conn, "messages") and _has_table(conn, "discord_channels"):
        channel_comments = conn.execute(
            """
            SELECT m.message_id, m.discord_message_id, m.content, m.summary, m.created_at,
                   c.channel_name, m.event_type, m.workflow
            FROM messages m
            LEFT JOIN discord_channels c ON c.channel_id = m.channel_id
            WHERE m.author_type = 'user'
              AND m.created_at >= ?
              AND TRIM(COALESCE(m.content, '')) <> ''
              AND (
                LOWER(COALESCE(c.channel_name, '')) LIKE '%leader-action%'
                OR LOWER(COALESCE(c.channel_name, '')) LIKE '%arena-relay%'
                OR m.event_type = 'leader_action_note'
                OR m.workflow = 'arena-relay'
              )
            ORDER BY m.created_at DESC, m.message_id DESC
            LIMIT 25
            """,
            (cutoff,),
        ).fetchall()
    if not rows and not channel_comments:
        return None

    by_type = Counter()
    by_status = Counter()
    samples = []
    notes = 0
    copy_edits = 0
    for row in rows:
        action_type = row["action_type"] or "unknown"
        status = row["status"] or "unknown"
        by_type[action_type] += 1
        by_status[status] += 1
        note = _clean(row["decision_note"])
        if note:
            notes += 1
        diff = _loads_dict(row["copy_edit_diff_json"])
        if diff.get("changed"):
            copy_edits += 1
        detail_parts = []
        if row["target_player_name"]:
            detail_parts.append(str(row["target_player_name"]))
        if note:
            detail_parts.append(f"note: {note}")
        if diff.get("changed"):
            similarity = diff.get("similarity")
            if similarity is not None:
                detail_parts.append(f"copy edited, similarity {similarity}")
            else:
                detail_parts.append("copy edited")
        if row["rationale"] and not note:
            detail_parts.append(f"rationale: {row['rationale']}")
        samples.append({
            "label": f"{action_type}/{status}",
            "detail": _truncate(" | ".join(detail_parts) or row["prompt_text"]),
            "action_id": row["action_id"],
            "source": "leader_action_recommendations",
        })

    comment_samples = []
    for comment in channel_comments:
        comment_samples.append({
            "label": f"#{comment['channel_name'] or 'leader-actions'} comment",
            "detail": _truncate(comment["content"] or comment["summary"]),
            "message_id": comment["message_id"],
            "discord_message_id": comment["discord_message_id"],
            "source": "messages",
        })

    total_evidence = len(rows) + len(channel_comments)
    confidence = min(0.95, 0.58 + (0.05 * min(total_evidence, 6)) + (0.05 if notes else 0) + (0.04 if copy_edits else 0))
    severity = 4 if total_evidence >= 3 or notes or by_status.get("rejected") else 3
    metrics = {
        "leader_action_feedback_rows": len(rows),
        "leader_action_channel_comments": len(channel_comments),
        "decision_notes": notes,
        "copy_edits": copy_edits,
        "rejected": by_status.get("rejected", 0),
        "deferred": by_status.get("deferred", 0),
    }
    metrics.update({f"action_type_{key}": value for key, value in by_type.most_common(5)})
    return {
        "category": "routing_quality",
        "title": "Fold leader-action feedback into Elixir recommendation policy",
        "rationale": (
            "Leaders are adding notes, decisions, direct #leader-actions comments, or copy edits "
            "to Elixir action cards. Those comments are high-signal evidence that the current "
            "recommendation/routing policy needs to learn from leadership corrections instead of "
            "treating them as one-off Discord reactions."
        ),
        "proposed_change": (
            "Review the sampled leader feedback, update the relevant signal detectors or leader-action "
            "policy, and add regression tests so similar future recommendations are either improved, "
            "suppressed, deferred, or routed differently."
        ),
        "evidence": {
            "basis": "leader-action-feedback",
            "metrics": metrics,
            "samples": (samples + comment_samples)[:12],
            "action_type_counts": dict(by_type),
            "status_counts": dict(by_status),
        },
        "severity": severity,
        "confidence": confidence,
        "suggestion_key": db.suggestion_key_for(
            "routing_quality",
            "Fold leader-action feedback into Elixir recommendation policy",
            basis="leader-action-feedback",
        ),
    }


def _prompt_failure_spec(conn, *, days: int) -> dict | None:
    if not _has_table(conn, "prompt_failures"):
        return None
    cutoff = _cutoff(days)
    rows = conn.execute(
        """
        SELECT workflow, failure_type, failure_stage, COUNT(*) AS count,
               MAX(recorded_at) AS last_seen,
               MAX(detail) AS detail,
               MAX(llm_last_error) AS llm_last_error
        FROM prompt_failures
        WHERE recorded_at >= ?
        GROUP BY workflow, failure_type, failure_stage
        HAVING COUNT(*) >= 2
        ORDER BY count DESC, last_seen DESC
        LIMIT 8
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        return None
    total = sum(int(row["count"] or 0) for row in rows)
    samples = [
        {
            "label": f"{row['workflow'] or 'unknown'} {row['failure_type']}/{row['failure_stage']}",
            "detail": _truncate(row["detail"] or row["llm_last_error"] or f"{row['count']} failures"),
            "count": int(row["count"] or 0),
            "last_seen": row["last_seen"],
        }
        for row in rows
    ]
    return {
        "category": "cost_reliability",
        "title": "Reduce recurring prompt failure patterns",
        "rationale": (
            f"Elixir recorded {total} recurring prompt failures in the last {days} days. "
            "Repeated failures usually mean a brittle prompt contract, empty-content path, or "
            "tool-routing edge case that wastes LLM calls and can produce silent user-facing gaps."
        ),
        "proposed_change": (
            "Inspect the grouped failures, add the smallest code or prompt guard that prevents the "
            "repeat failure, and cover the failure shape with a regression test."
        ),
        "evidence": {
            "basis": "prompt-failures",
            "metrics": {"recurring_prompt_failures": total, "groups": len(rows), "window_days": days},
            "samples": samples,
        },
        "severity": 4 if total >= 5 else 3,
        "confidence": min(0.92, 0.62 + 0.04 * min(total, 7)),
        "suggestion_key": db.suggestion_key_for(
            "cost_reliability",
            "Reduce recurring prompt failure patterns",
            basis="prompt-failures",
        ),
    }


def _awareness_gap_spec(conn, *, days: int) -> dict | None:
    if not _has_table(conn, "awareness_ticks"):
        return None
    cutoff = _cutoff(days)
    rows = conn.execute(
        """
        SELECT tick_id, ticked_at, workflow, signals_in, covered_keys,
               considered_skipped, posts_rejected, skipped_reason,
               write_calls_issued, write_calls_succeeded, write_calls_denied,
               signal_outcomes_json,
               MAX(signals_in - covered_keys - considered_skipped, 0) AS unaccounted_signals
        FROM awareness_ticks
        WHERE ticked_at >= ?
          AND signals_in > 0
          AND (
            signals_in > covered_keys + considered_skipped
            OR write_calls_denied > 0
          )
        ORDER BY ticked_at DESC, tick_id DESC
        LIMIT 25
        """,
        (cutoff,),
    ).fetchall()
    gap_rows = []
    for row in rows:
        outcomes = _loads_list(row["signal_outcomes_json"])
        if outcomes:
            unaccounted_count = sum(1 for item in outcomes if (item or {}).get("status") == "coverage_gap")
        else:
            unaccounted_count = int(row["unaccounted_signals"] or 0)
        denied_count = int(row["write_calls_denied"] or 0)
        if unaccounted_count <= 0 and denied_count <= 0:
            continue
        row_dict = dict(row)
        row_dict["unaccounted_signals"] = unaccounted_count
        gap_rows.append(row_dict)

    if len(gap_rows) < 2:
        return None
    signals = sum(int(row["signals_in"] or 0) for row in gap_rows)
    covered = sum(int(row["covered_keys"] or 0) for row in gap_rows)
    skipped = sum(int(row["considered_skipped"] or 0) for row in gap_rows)
    denied = sum(int(row["write_calls_denied"] or 0) for row in gap_rows)
    unaccounted = sum(int(row["unaccounted_signals"] or 0) for row in gap_rows)
    samples = [
        {
            "label": f"{row['workflow'] or 'awareness'} tick",
            "detail": _truncate(
                f"signals={row['signals_in']} covered={row['covered_keys']} "
                f"skipped={row['considered_skipped']} unaccounted={row['unaccounted_signals']} "
                f"denied={row['write_calls_denied']} "
                f"reason={row['skipped_reason'] or '-'}"
            ),
            "tick_id": row["tick_id"],
            "ticked_at": row["ticked_at"],
        }
        for row in gap_rows[:8]
    ]
    return {
        "category": "signal_gap",
        "title": "Review awareness ticks that did not cover all observed signals",
        "rationale": (
            "Recent awareness ticks left observed signals unaccounted for, or attempted write tools "
            "that policy denied. That is exactly where observational mode can drift away from "
            "recommendation mode."
        ),
        "proposed_change": (
            "Inspect the unaccounted ticks, classify whether each gap is missing routing, missing "
            "case/project modeling, or a tool-policy constraint, then encode the rule. Intentional "
            "skips belong in routing-quality review, not this signal-gap bucket."
        ),
        "evidence": {
            "basis": "awareness-coverage-gaps",
            "metrics": {
                "gap_ticks": len(gap_rows),
                "signals_in": signals,
                "covered_keys": covered,
                "considered_skipped": skipped,
                "unaccounted_signals": unaccounted,
                "write_calls_denied": denied,
                "window_days": days,
            },
            "samples": samples,
        },
        "severity": 4 if skipped or denied else 3,
        "confidence": min(0.9, 0.64 + 0.03 * min(len(gap_rows), 8)),
        "suggestion_key": db.suggestion_key_for(
            "signal_gap",
            "Review awareness ticks that did not cover all observed signals",
            basis="awareness-coverage-gaps",
        ),
    }


def _delivery_outcome_spec(conn, *, days: int) -> dict | None:
    if not _has_table(conn, "signal_outcomes"):
        return None
    cutoff = _cutoff(days)
    rows = conn.execute(
        """
        SELECT target_channel_key, delivery_status, error_detail, COUNT(*) AS count,
               MAX(updated_at) AS last_seen
        FROM signal_outcomes
        WHERE COALESCE(updated_at, created_at) >= ?
          AND delivery_status IN ('failed', 'skipped')
        GROUP BY target_channel_key, delivery_status, error_detail
        HAVING COUNT(*) >= 2
        ORDER BY count DESC, last_seen DESC
        LIMIT 8
        """,
        (cutoff,),
    ).fetchall()
    if not rows:
        return None
    total = sum(int(row["count"] or 0) for row in rows)
    samples = [
        {
            "label": f"{row['target_channel_key'] or 'unknown'} {row['delivery_status']}",
            "detail": _truncate(row["error_detail"] or f"{row['count']} outcomes"),
            "count": int(row["count"] or 0),
            "last_seen": row["last_seen"],
        }
        for row in rows
    ]
    return {
        "category": "routing_quality",
        "title": "Investigate repeated skipped or failed signal outcomes",
        "rationale": (
            f"Elixir recorded {total} repeated skipped or failed signal outcomes in the last {days} days. "
            "Some skips are healthy policy, but repeated skips often reveal missing durable state, "
            "lane mismatch, or an over-tight delivery guard."
        ),
        "proposed_change": (
            "Review the grouped outcomes and decide whether each should become an explicit policy, "
            "a durable case/project state transition, or a routing/delivery fix."
        ),
        "evidence": {
            "basis": "signal-outcomes",
            "metrics": {"skipped_or_failed_outcomes": total, "groups": len(rows), "window_days": days},
            "samples": samples,
        },
        "severity": 4 if total >= 5 else 3,
        "confidence": min(0.9, 0.6 + 0.04 * min(total, 7)),
        "suggestion_key": db.suggestion_key_for(
            "routing_quality",
            "Investigate repeated skipped or failed signal outcomes",
            basis="signal-outcomes",
        ),
    }


def collect_improvement_specs(*, days: int = 30, conn=None) -> list[dict]:
    detectors = (
        _leader_action_feedback_spec,
        _prompt_failure_spec,
        _awareness_gap_spec,
        _delivery_outcome_spec,
    )
    specs = []
    for detector in detectors:
        spec = detector(conn, days=days)
        if spec:
            specs.append(spec)
    return specs


def store_improvement_specs(specs: Iterable[dict], *, conn=None) -> list[dict]:
    stored = []
    for spec in specs:
        stored.append(db.upsert_improvement_suggestion(conn=conn, **spec))
    return stored


def _format_suggestion(suggestion: dict) -> str:
    evidence = suggestion.get("evidence") or {}
    metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else {}
    sample = ""
    samples = evidence.get("samples") or []
    if samples:
        first = samples[0]
        sample = f"\n  sample: {first.get('label')}: {first.get('detail')}"
    metric_text = ", ".join(f"{key}={value}" for key, value in sorted(metrics.items())[:6])
    metric_line = f"\n  metrics: {metric_text}" if metric_text else ""
    return (
        f"[{suggestion.get('category')}] {suggestion.get('title')}\n"
        f"  severity={suggestion.get('severity')} confidence={suggestion.get('confidence')}"
        f" status={suggestion.get('status', 'new')}{metric_line}{sample}"
    )


def _format_promotion_result_status(item: dict) -> str:
    if item.get("dry_run"):
        return "dry-run"
    if item.get("action") == "skip":
        return "skipped"
    return "ok" if item.get("ok") else "failed"


def _gh_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _parse_issue_number(url_or_output: str) -> int | None:
    match = re.search(r"/issues/(\d+)", url_or_output or "")
    if not match:
        return None
    return int(match.group(1))


def _github_issue_metadata(
    issue_number: int,
    *,
    repo: str,
    runner: Callable[[list[str]], subprocess.CompletedProcess] = _gh_runner,
) -> tuple[dict | None, str | None]:
    completed = runner([
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "state,stateReason,url",
    ])
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return None, output or "failed to read issue state"
    try:
        loaded = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None, output or "invalid issue state response"
    return loaded if isinstance(loaded, dict) else {}, None


def promote_suggestions_to_github(
    suggestions: list[dict],
    *,
    repo: str = DEFAULT_REPO,
    min_confidence: float = DEFAULT_PROMOTION_CONFIDENCE,
    write: bool = False,
    runner: Callable[[list[str]], subprocess.CompletedProcess] = _gh_runner,
    conn=None,
) -> list[dict]:
    results = []
    for suggestion in suggestions:
        if suggestion.get("confidence", 0) < min_confidence:
            results.append({
                "suggestion_key": suggestion.get("suggestion_key"),
                "action": "skip",
                "reason": "below_confidence_threshold",
            })
            continue
        if suggestion.get("status") in {db.SUGGESTION_DISMISSED, db.SUGGESTION_IMPLEMENTED}:
            results.append({
                "suggestion_key": suggestion.get("suggestion_key"),
                "action": "skip",
                "reason": f"status:{suggestion.get('status')}",
            })
            continue
        labels = db.github_labels_for_improvement(suggestion)
        body = db.build_improvement_github_issue_body(suggestion)
        existing_issue = suggestion.get("github_issue_number")
        action = "update" if existing_issue else "create"
        if not write:
            results.append({
                "suggestion_key": suggestion.get("suggestion_key"),
                "action": action,
                "dry_run": True,
                "title": suggestion.get("title"),
                "labels": labels,
                "github_issue_number": existing_issue,
            })
            continue
        github_issue_url = suggestion.get("github_issue_url")
        if existing_issue:
            metadata, metadata_error = _github_issue_metadata(
                int(existing_issue),
                repo=repo,
                runner=runner,
            )
            if metadata_error:
                results.append({
                    "suggestion_key": suggestion.get("suggestion_key"),
                    "action": action,
                    "ok": False,
                    "github_issue_number": existing_issue,
                    "error": metadata_error,
                })
                continue
            github_issue_url = metadata.get("url") or github_issue_url
            if metadata.get("state") == "CLOSED":
                state_reason = metadata.get("stateReason") or "closed"
                results.append({
                    "suggestion_key": suggestion.get("suggestion_key"),
                    "action": "skip",
                    "reason": f"github_issue_closed:{state_reason}",
                    "github_issue_number": existing_issue,
                    "github_issue_url": github_issue_url,
                })
                continue
        if existing_issue:
            args = [
                "gh",
                "issue",
                "edit",
                str(existing_issue),
                "--repo",
                repo,
                "--title",
                suggestion.get("title") or "Elixir improvement suggestion",
                "--body",
                body,
            ]
        else:
            args = [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                suggestion.get("title") or "Elixir improvement suggestion",
                "--body",
                body,
            ]
            for label in labels:
                args.extend(["--label", label])
        completed = runner(args)
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            results.append({
                "suggestion_key": suggestion.get("suggestion_key"),
                "action": action,
                "ok": False,
                "error": output,
            })
            continue
        issue_number = existing_issue or _parse_issue_number(output)
        if issue_number:
            updated = db.mark_improvement_suggestion_promoted(
                suggestion["suggestion_key"],
                github_issue_number=issue_number,
                github_issue_url=output if output.startswith("http") else github_issue_url,
                conn=conn,
            )
            suggestion.update(updated or {})
        results.append({
            "suggestion_key": suggestion.get("suggestion_key"),
            "action": action,
            "ok": True,
            "github_issue_number": issue_number,
            "output": output,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Review Elixir improvement opportunities.")
    parser.add_argument("--days", type=int, default=30, help="Lookback window for source evidence.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum stored suggestions to print/promote.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text report.")
    parser.add_argument("--no-store", action="store_true", help="Do not persist collected suggestions.")
    parser.add_argument("--promote-github", action="store_true", help="Dry-run promotion of stored suggestions to GitHub issues.")
    parser.add_argument("--write-github", action="store_true", help="Actually create/update GitHub issues. Implies --promote-github.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository owner/name.")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_PROMOTION_CONFIDENCE, help="Minimum confidence for promotion.")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        specs = collect_improvement_specs(days=args.days, conn=conn)
        if args.no_store:
            suggestions = [
                {
                    **spec,
                    "status": db.SUGGESTION_SHADOW,
                    "first_seen_at": None,
                    "last_seen_at": None,
                }
                for spec in specs
            ]
        else:
            suggestions = store_improvement_specs(specs, conn=conn)
            suggestions = suggestions[: max(1, min(int(args.limit or 50), 200))]
        promotion = []
        if args.promote_github or args.write_github:
            promotion = promote_suggestions_to_github(
                suggestions,
                repo=args.repo,
                min_confidence=args.min_confidence,
                write=args.write_github,
                conn=conn,
            )
        if args.json:
            print(json.dumps({"suggestions": suggestions, "promotion": promotion}, indent=2, ensure_ascii=False))
            return
        if not suggestions:
            print("No improvement opportunities found for the selected window.")
        else:
            print(f"Elixir Improvement Radar ({args.days}d)")
            print()
            for suggestion in suggestions:
                print(_format_suggestion(suggestion))
                print()
        if promotion:
            print("GitHub promotion:")
            for item in promotion:
                status = _format_promotion_result_status(item)
                issue = f" #{item['github_issue_number']}" if item.get("github_issue_number") else ""
                reason = f" ({item['reason']})" if item.get("reason") else ""
                print(f"- {status}: {item.get('action')} {item.get('suggestion_key')}{issue}{reason}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
