#!/usr/bin/env python3
"""Enforce the reviewed budget and minimum behavior for broad exceptions."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (
    "agent",
    "capabilities",
    "db",
    "engine",
    "memory_store",
    "runtime",
    "storage",
)
ROOT_SOURCES = ("cr_api.py", "elixir.py", "elixir_agent.py", "prompts.py")
EXCLUDED: set[str] = set()

# This is a reviewed migration baseline, not a target. Any broad catch added,
# removed, or moved requires an explicit baseline update in the same change.
# The policy below also rejects bare/BaseException catches and broad pass-only
# handlers. Long-term cleanup should make these numbers go down.
BROAD_EXCEPTION_BASELINE = {
    "agent/chat.py": 1,
    # 3 (2026-08-04, new): the chassis degrades rather than raises. One guards
    # the turn itself (a failed turn becomes an episode with an error, so the
    # caller can escalate instead of crashing), and two guard context assembly —
    # a turn without lessons or recent posts is worse than one with, and far
    # better than a hard-post floor left uncovered because a lookup failed.
    "agent/chassis.py": 3,
    # 3 -> 4 (2026-08-05): recording a call's cost against the daily spend
    # ceiling. A counter that can fail an already-successful model call would
    # be a cost control causing the outage it exists to prevent.
    # 4 -> 5 (2026-08-09): reading the block census off the serialized response
    # for telemetry. Same rule — a reporting detail must not fail a call that
    # already succeeded; the row still records without it.
    "agent/core.py": 5,
    # 2: the ceiling fails OPEN (an unreadable counter must never be what stops
    # Elixir welcoming a new member), and the #leaders notice is best-effort —
    # a cost control must not raise into the call it is declining.
    "agent/spend_budget.py": 2,
    "agent/cr_api_tool.py": 1,
    "agent/factual_admission.py": 1,
    "agent/intent_router.py": 1,
    "agent/release_notes.py": 2,
    "agent/tool_exec.py": 8,  # -1: #225 removed the obsolete watch-to-case fallback
    "agent/workflows.py": 10,
    "capabilities/battle_intel.py": 1,  # newcomer view: deck naming must never fail a welcome
    "capabilities/decks.py": 2,
    "capabilities/members.py": 1,
    "capabilities/war.py": 1,
    "cr_api.py": 4,
    "db/__init__.py": 2,
    # prompts.py: +1 for the fail-soft live-trophy-floor read. A prompt must build
    # even with no database; an unavailable floor renders as "read it live"
    # rather than a guessed number.
    "prompts.py": 1,
    "db/schema.py": 37,  # +1: v37 migration rollback/re-raise (same pattern as v2-v36)
    "engine/chronicles.py": 1,
    "engine/emitters/clan.py": 2,
    "engine/game_check.py": 1,
    "engine/leader_note_effects.py": 2,  # apply/revert leader-note effects fail-open (never break the interpreter)
    "engine/management.py": 6,  # +3: v7 leader-note gates (premise fingerprint, member shield) fail-open to False
    "engine/materialize.py": 2,
    "engine/nicknames.py": 1,
    "engine/pol_seasons.py": 2,
    "memory_store/__init__.py": 1,
    "runtime/activity_runner.py": 3,
    "runtime/admin.py": 2,  # -2: #212 retired the dead signal.publish-pending impl
    "runtime/alerts.py": 3,  # +1 each: job-failure and spend-ceiling loop-schedule guards
    # 38 -> 40 (2026-08-03): the join trigger's two guards. Both are the same
    # "a diagnostic must never fail its caller" shape as the rest of this file —
    # one keeps a trigger failure from failing the engine tick, the other keeps a
    # high-water write failure from escaping a background task.
    # 41 -> 40 (2026-08-04): the Observatory webapp startup guard went with the
    # webapp itself. The stall-watchdog boot guard is still in this count.
    # 40 -> 41 (2026-08-04): the Phase 0 wake-evaluation guard in the engine
    # tick. Shadow measurement must never be able to fail a tick.
    # 41 -> 42 (2026-08-04): the Phase 1 responder turn, same reason — a wake
    # that fails is a wake the daily deliberation inherits, not a dead tick.
    # 42 -> 43 (2026-08-05): the Phase 2 divergence report. It observes two
    # composing paths for overlap; a check that could fail the daily leader-action
    # job would be a monitor taking down the thing it monitors.
    "runtime/app.py": 43,
    "runtime/awareness/deliver.py": 10,
    # The floor-miss half reads the telemetry database, which is a separate file
    # and may be absent or locked. Missing telemetry must degrade the report to
    # "unavailable", never raise into the daily job.
    "runtime/awareness/divergence.py": 1,
    "runtime/awareness/gate.py": 2,
    "runtime/awareness/loop.py": 8,
    # 1 -> 2 (2026-08-04): the covered-signal lookup fails OPEN. A failure that
    # silently emptied the set would suppress nothing (correct); a failure that
    # raised would take down the whole read, and with it the hard-post floor.
    "runtime/awareness/read.py": 2,
    "runtime/awareness/store.py": 1,
    # 4 (2026-08-04, new): the wake evaluator runs inside the 10-minute engine
    # tick and is pure measurement in Phase 0 — every guard here degrades to
    # "no wake this tick" and logs, which is strictly safer than a raised
    # exception killing the heartbeat. Two guard evaluate/observe at the tick
    # boundary, one guards the telemetry budget read (an unreadable file must
    # not block a wake), one guards a high-water write.
    "runtime/awareness/wake.py": 4,
    # 2 (2026-08-04, new): the responder must never take down the engine tick.
    # One guards delivery (a raised deliver_posts becomes a failed wake the
    # daily brain inherits, not a crashed tick); one guards the episode record,
    # which is observation and must not cost a delivered post.
    "runtime/awareness/respond.py": 2,
    "runtime/channel_router.py": 20,
    "runtime/discord_commands.py": 8,  # +1: command telemetry is fail-soft and logs before continuing
    "runtime/discord_posting.py": 2,
    "runtime/elder_standing.py": 1,
    # Both fail OPEN and log: an unreadable/unwritable dedup store must never
    # suppress a deliverable. A duplicate email is visible; a silent omission
    # is the failure this whole area spent 2026-08-03 fixing.
    "runtime/email_dedup.py": 2,
    "runtime/email_verification.py": 1,
    # runtime/health.py removed: the daily health check was retired 2026-07-28
    # (it read an incident ledger that never recorded a row).
    "runtime/helpers/_common.py": 1,
    "runtime/helpers/_members.py": 2,
    "runtime/helpers/_reports.py": 10,
    # +1 (2026-08-03): the weekly email composer logs and falls back to the
    # reformatted Discord post — a plainer email beats a missing one.
    "runtime/jobs/_core.py": 16,
    "runtime/jobs/_battle_intel.py": 2,  # Stage-A/B jobs: mark_job_failure on any tick error
    # 6 -> 2 (2026-08-03): the Discord version of the intel report was removed —
    # email is the path for it — taking its four guards with it. The two that
    # remain belong to the email job: one guards context assembly so a failed
    # report marks the job failed instead of killing the scheduler; the other
    # keeps a memory-write failure from unsending an email already gone.
    "runtime/jobs/_intel.py": 2,
    # All three guard per-clan API fetches (opponent profile, our own profile,
    # river race log): one bad clan must degrade that section of the scouting
    # report, never sink the whole thing.
    "runtime/war_intel.py": 3,
    # 4 -> 5 (2026-08-09): the scheduled-period sweep marks its own job failed
    # if candidate discovery breaks. A broken reliability monitor must surface
    # without escaping into APScheduler and disappearing as framework noise.
    "runtime/jobs/_maintenance.py": 5,
    "runtime/jobs/_memory.py": 23,  # +1: optional contradiction-card channel lookup fails soft (#229)
    "runtime/jobs/_promotion.py": 3,
    "runtime/jobs/_tournament.py": 7,  # autowatch scan + clan-chat relay
    "runtime/leader_action_feedback.py": 1,
    "runtime/leader_action_ui.py": 9,
    "runtime/leader_note_interpreter.py": 5,  # interpret/apply/undo/fix all fail-open off the delivery path
    "runtime/onboarding.py": 3,
    "runtime/outreach.py": 3,  # raise_card + send_dm + compose fail-soft in the flow loop
    "runtime/prompt_feedback.py": 5,  # +1: member_outreach decision handling
    # 3 -> 4 (2026-08-05): the startup budget line. The boot message is what
    # tells us the bot came up at all; an unreadable spend counter must degrade
    # that line, never suppress the message.
    "runtime/startup.py": 4,
    "runtime/status.py": 3,
    # One catch isolates eligible jobs inside the catch-up sweep. Each job's
    # failure is logged and persisted, while later owed periods still run.
    "runtime/scheduled_catchup.py": 1,
    "runtime/system_signals.py": 1,
    # was runtime/webapp/ticks.py — the Observatory went, the tick record stayed.
    "runtime/tick_history.py": 2,
    "storage/_formatting.py": 2,
    # +1 (v34): the 7-day LLM cost now reads the telemetry DB and fails soft —
    # a status page must render even when the telemetry file is unreadable.
    "storage/identity.py": 3,
    # Telemetry must never be what breaks the workload it measures, so every
    # public writer swallows and logs. Same reason for db_watch: an instrument
    # that can raise into the engine tick is worse than no instrument.
    # +1 (2026-08-03): _top_sites serializes the per-site breakdown. If that
    # fails the transaction row must still record without it — a missing detail
    # column is recoverable, losing the measurement is not.
    "storage/db_watch.py": 5,
    # 5 -> 6 (2026-08-04): record_wake_observation follows the same fail-soft
    # rule as every other writer here. That policy hid a real NameError (json
    # was never imported) until a test asserted the row actually persisted —
    # which is the argument for asserting persistence, not for raising.
    # 6 -> 5 (2026-08-06): record_lock_wait deleted with the db_lock_waits table
    # it wrote — no caller, and no row in its lifetime.
    "storage/telemetry.py": 5,
    "storage/metadata.py": 1,  # telemetry retention never fails clan maintenance
    # storage/incidents.py removed with the ledger it wrote (2026-07-28).
    "storage/leader_actions.py": 2,
    # rebuild_interpreted manages its own connection instead of using
    # @managed_connection, so it must reproduce the decorator's rollback/close —
    # the catch re-raises after rolling back, exactly like the decorator's.
    "storage/battle_intel.py": 1,
}

_LOG_CALLS = {"critical", "debug", "error", "exception", "info", "warn", "warning"}
_REPORT_CALLS = {
    "add",
    "append",
    "edit",
    "mark_job_failure",
    "send",
    "send_message",
    "setdefault",
}

# Exceptions that mean the world is broken, not that a value failed to parse.
# `except (TypeError, ValueError)` around an int() is a parse guard and stays
# unpoliced -- there are ~150 of those and they are all fine. These are
# different: the database is down, the network failed, a file is missing,
# Discord said no. Swallowing one silently hides an outage.
#
# The narrow-catch rule exists because the broad-catch rule was not enough. The
# post-test invariant sweep died for three weeks behind `except
# sqlite3.OperationalError: pass`, and an unrouted channel share was dropped
# behind a resolve failure -- neither was visible to a policy that only reads
# `except Exception`.
_INFRASTRUCTURE_EXCEPTIONS = {
    "CalledProcessError",
    "ConnectionError",
    "ConnectTimeout",
    "DatabaseError",
    "DiscordServerError",
    "Forbidden",
    "HTTPError",
    "HTTPException",
    "IOError",
    "IntegrityError",
    "InterfaceError",
    "NotFound",
    "OSError",
    "OperationalError",
    "ProgrammingError",
    "RateLimited",
    "ReadTimeout",
    "RequestException",
    "SubprocessError",
    "Timeout",
    "TimeoutError",
}

# A silent infrastructure catch that is genuinely fine opts out at the catch
# itself, with `# hygiene: <reason>` on the except line or inside the handler.
#
# This was a table of (file, line) pairs for about an hour, until adding three
# lines to runtime/app.py silently invalidated an entry 1,100 lines below and
# the check failed on an unrelated edit. A justification that lives anywhere
# but the code it justifies will drift away from it.
_OPT_OUT_MARKER = "# hygiene:"


def _sources() -> list[Path]:
    paths = [path for directory in SOURCE_DIRS for path in (ROOT / directory).rglob("*.py")]
    paths.extend(ROOT / name for name in ROOT_SOURCES)
    return sorted(path for path in paths if path.relative_to(ROOT).as_posix() not in EXCLUDED)


def _exception_name(handler: ast.ExceptHandler) -> str | None:
    if handler.type is None:
        return None
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    return "other"


def _caught_names(node: ast.expr | None) -> set[str]:
    """Every exception name in a handler, through tuples and dotted paths."""
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        return {name for element in node.elts for name in _caught_names(element)}
    return set()


def _opts_out(handler: ast.ExceptHandler, lines: list[str]) -> bool:
    """True when the catch carries a written justification at the site."""
    start = handler.lineno - 1
    end = handler.end_lineno or handler.lineno
    return any(_OPT_OUT_MARKER in line for line in lines[start:end])


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """True when nothing about this failure escapes the handler."""
    nodes = list(ast.walk(ast.Module(body=handler.body, type_ignores=[])))
    if any(isinstance(node, ast.Raise) for node in nodes):
        return False
    calls = {_call_name(node) for node in nodes if isinstance(node, ast.Call)}
    return not (calls & (_LOG_CALLS | _REPORT_CALLS))


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _classification(handler: ast.ExceptHandler) -> str:
    body = ast.Module(body=handler.body, type_ignores=[])
    nodes = list(ast.walk(body))
    calls = {_call_name(node) for node in nodes if isinstance(node, ast.Call)}
    if any(isinstance(node, ast.Raise) for node in nodes):
        return "reraised"
    if calls & _LOG_CALLS:
        return "logged"
    if calls & _REPORT_CALLS:
        return "reported"
    if any(
        isinstance(
            node,
            (
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
                ast.Break,
                ast.Continue,
                ast.Return,
            ),
        )
        for node in nodes
    ):
        return "fallback"
    if calls:
        return "fallback"
    return "unhandled"


def check() -> tuple[list[str], Counter]:
    findings: list[str] = []
    counts: Counter[str] = Counter()
    classes: Counter[str] = Counter()

    for path in _sources():
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=relative)
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            name = _exception_name(handler)

            # Applies at every breadth: a narrow catch can hide an outage just
            # as completely as a broad one, and used to do so unpoliced.
            if (
                _caught_names(handler.type) & _INFRASTRUCTURE_EXCEPTIONS
                and _is_silent(handler)
                and not _opts_out(handler, lines)
            ):
                findings.append(
                    f"{relative}:{handler.lineno}: infrastructure failure swallowed "
                    f"without a log — an outage here would be invisible. Log it, or "
                    f"justify it with `{_OPT_OUT_MARKER} <reason>` at the catch"
                )

            if name not in {None, "BaseException", "Exception"}:
                continue
            counts[relative] += 1
            if name in {None, "BaseException"}:
                findings.append(
                    f"{relative}:{handler.lineno}: catch Exception, not "
                    f"{name or 'a bare exception'}"
                )
            if any(isinstance(node, ast.Pass) for node in handler.body):
                findings.append(
                    f"{relative}:{handler.lineno}: broad exception cannot pass silently"
                )
            classification = _classification(handler)
            classes[classification] += 1
            if classification == "unhandled":
                findings.append(
                    f"{relative}:{handler.lineno}: broad exception needs an explicit "
                    "fallback, report, log, status update, or re-raise"
                )

    actual = {path: count for path, count in sorted(counts.items())}
    if actual != BROAD_EXCEPTION_BASELINE:
        all_paths = sorted(set(actual) | set(BROAD_EXCEPTION_BASELINE))
        for path in all_paths:
            current = actual.get(path, 0)
            expected = BROAD_EXCEPTION_BASELINE.get(path, 0)
            if current != expected:
                findings.append(
                    f"{path}: broad-exception baseline changed "
                    f"({expected} -> {current}); review and update the baseline"
                )

    return findings, classes


def main() -> int:
    findings, classes = check()
    if findings:
        print("Exception hygiene checks failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(classes.items()))
    print(
        f"Exception hygiene passed: {sum(classes.values())} reviewed broad catches "
        f"across {len(BROAD_EXCEPTION_BASELINE)} files ({summary})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
