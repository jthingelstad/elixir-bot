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
    "agent/core.py": 3,
    "agent/cr_api_tool.py": 1,
    "agent/factual_admission.py": 1,
    "agent/intent_router.py": 1,
    "agent/release_notes.py": 2,
    "agent/tool_exec.py": 8,  # -1: #225 removed the obsolete watch-to-case fallback
    "agent/workflows.py": 10,
    "capabilities/decks.py": 2,
    "capabilities/members.py": 1,
    "capabilities/war.py": 1,
    "cr_api.py": 4,
    "db/__init__.py": 2,
    "db/schema.py": 25,  # +1: V25 migration rollback/re-raise (same pattern as v2-v24)
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
    "runtime/alerts.py": 2,  # +1: schedule_job_failure_alert best-effort loop-schedule guard
    "runtime/app.py": 38,  # dead war-thread post-step retired in #223
    "runtime/awareness/deliver.py": 10,
    "runtime/awareness/gate.py": 2,
    "runtime/awareness/loop.py": 8,
    "runtime/awareness/read.py": 1,
    "runtime/awareness/store.py": 1,
    "runtime/channel_router.py": 20,
    "runtime/discord_commands.py": 8,  # +1: command telemetry is fail-soft and logs before continuing
    "runtime/discord_posting.py": 2,
    "runtime/elder_standing.py": 1,
    "runtime/email_verification.py": 1,
    # runtime/health.py removed: the daily health check was retired 2026-07-28
    # (it read an incident ledger that never recorded a row).
    "runtime/helpers/_common.py": 1,
    "runtime/helpers/_members.py": 2,
    "runtime/helpers/_reports.py": 10,
    "runtime/jobs/_core.py": 15,
    "runtime/jobs/_intel.py": 4,
    "runtime/jobs/_maintenance.py": 4,
    "runtime/jobs/_memory.py": 23,  # +1: optional contradiction-card channel lookup fails soft (#229)
    "runtime/jobs/_promotion.py": 3,
    "runtime/jobs/_tournament.py": 7,  # autowatch scan + clan-chat relay
    "runtime/leader_action_feedback.py": 1,
    "runtime/leader_action_ui.py": 9,
    "runtime/leader_note_interpreter.py": 5,  # interpret/apply/undo/fix all fail-open off the delivery path
    "runtime/onboarding.py": 3,
    "runtime/outreach.py": 3,  # raise_card + send_dm + compose fail-soft in the flow loop
    "runtime/prompt_feedback.py": 5,  # +1: member_outreach decision handling
    "runtime/startup.py": 3,
    "runtime/status.py": 3,
    "runtime/system_signals.py": 1,
    "runtime/webapp/chat.py": 2,
    "runtime/webapp/queries.py": 5,  # +1: command war-snapshot best-effort (like war_page)
    "runtime/webapp/routes.py": 4,
    "runtime/webapp/ticks.py": 2,
    "storage/_formatting.py": 2,
    "storage/identity.py": 2,
    # storage/incidents.py removed with the ledger it wrote (2026-07-28).
    "storage/leader_actions.py": 2,
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
