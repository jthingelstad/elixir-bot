"""The model-call policy is the one place a ceiling or an effort level is chosen.

Before 2026-08-08 `max_tokens` was a literal at ~25 call sites across six files
— 200, 300, 400, 512, 600, 900, 1200, 1600, 2000, 2048, 2500, 4096, 8192, 9000,
16384 — and `effort` was set nowhere at all. Nobody could see the set, so nobody
could tell a deliberate ceiling from a copied one, and three separate workflows
truncated silently for weeks.

Centralizing only helps if it stays centralized, which is what these guard.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import agent.core as core

AGENT_DIR = pathlib.Path(__file__).resolve().parents[1] / "agent"
CALL_FUNCS = {"_chat_with_tools", "_create_chat_completion", "_generate_simple_message"}

# Call sites allowed to pass max_tokens explicitly, and why. Everything else must
# take its ceiling from MODEL_CALL_POLICY.
ALLOWED_EXPLICIT = {
    # Retries that deliberately want MORE headroom than the workflow's usual
    # ceiling — the override is the entire point of the retry.
    ("agent/workflows.py", "awareness"),
    ("agent/workflows.py", "deck_review"),
    # Dynamic workflow families (`event:<name>`, the chassis attention budget,
    # the per-lane dispatcher) whose names are open-ended, so they cannot have a
    # policy row and would otherwise fall through to the default.
    ("agent/workflows.py", None),
    ("agent/chassis.py", None),
    ("agent/chat.py", None),
}


def _call_sites():
    for path in sorted(AGENT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fn not in CALL_FUNCS:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            wv = kw.get("workflow")
            workflow = wv.value if isinstance(wv, ast.Constant) else None
            yield (
                f"agent/{path.name}",
                node.lineno,
                workflow,
                "max_tokens" in kw,
            )


def test_no_call_site_reintroduces_a_literal_ceiling():
    """A new hardcoded max_tokens is the drift this refactor exists to stop."""
    offenders = [
        f"{path}:{line} (workflow={workflow})"
        for path, line, workflow, has_mt in _call_sites()
        if has_mt and (path, workflow) not in ALLOWED_EXPLICIT
    ]
    assert not offenders, (
        "these call sites set max_tokens themselves instead of taking it from "
        "agent.core.MODEL_CALL_POLICY; add a policy row, or add the site to "
        f"ALLOWED_EXPLICIT with a reason: {offenders}"
    )


def test_every_named_workflow_that_calls_the_model_has_a_policy_row():
    """A missing row silently falls back to the 4096 default, which is wrong in
    both directions — too small for the weekly writing, far too large for a
    one-line reply."""
    missing = sorted(
        {
            workflow
            for _p, _l, workflow, _mt in _call_sites()
            if workflow and workflow not in core.MODEL_CALL_POLICY
        }
    )
    assert not missing, f"no MODEL_CALL_POLICY row for: {missing}"


@pytest.mark.parametrize("workflow,policy", sorted(core.MODEL_CALL_POLICY.items()))
def test_policy_values_are_sane(workflow, policy):
    assert policy.effort in ("low", "medium", "high", "xhigh", "max"), workflow
    # 200 is the smallest real ceiling in use (a one-line release blurb); 16384
    # is the largest. Outside that range is a typo, not a decision.
    assert 200 <= policy.max_tokens <= 16384, workflow


def test_unknown_workflow_falls_back_instead_of_raising():
    """A missing row must degrade to a sane call, not take down the caller."""
    policy = core.policy_for("definitely_not_a_workflow")
    assert policy.max_tokens == core.DEFAULT_MAX_TOKENS
    assert policy.effort == core.DEFAULT_EFFORT


def test_thinking_is_bounded_for_every_workflow_that_can_think():
    """Effort is the only thinking control these models still accept — a row
    without one would inherit the API default and be unbounded again."""
    for workflow, policy in core.MODEL_CALL_POLICY.items():
        assert policy.effort, workflow
