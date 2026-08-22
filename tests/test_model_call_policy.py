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
    # Shared helpers and explicit retries can override the registry ceiling.
    ("agent/workflows.py", None),
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
                "timeout" in kw,
            )


def test_no_call_site_reintroduces_a_literal_ceiling():
    """A new hardcoded max_tokens is the drift this refactor exists to stop."""
    offenders = [
        f"{path}:{line} (workflow={workflow})"
        for path, line, workflow, has_mt, _has_to in _call_sites()
        if has_mt and (path, workflow) not in ALLOWED_EXPLICIT
    ]
    assert not offenders, (
        "these call sites set max_tokens themselves instead of taking it from "
        "agent.core.MODEL_CALL_POLICY; add a policy row, or add the site to "
        f"ALLOWED_EXPLICIT with a reason: {offenders}"
    )


def test_no_call_site_reintroduces_a_literal_timeout():
    """Timeouts sprawled the same way ceilings did, and worse: a module-level
    override map and ~6 call-site literals disagreed about precedence, with the
    map silently winning over an explicit argument."""
    offenders = [
        f"{path}:{line} (workflow={workflow})"
        for path, line, workflow, _has_mt, has_to in _call_sites()
        if has_to and (path, workflow) not in ALLOWED_EXPLICIT
    ]
    assert not offenders, (
        "these call sites set timeout themselves instead of taking it from "
        f"agent.core.MODEL_CALL_POLICY: {offenders}"
    )


def test_the_old_timeout_override_map_is_gone():
    """It was a second, competing source of truth for the same number."""
    assert not hasattr(core, "WORKFLOW_TIMEOUT_OVERRIDES")


def test_every_named_workflow_that_calls_the_model_has_a_policy_row():
    """A missing row silently falls back to the 4096 default, which is wrong in
    both directions — too small for the weekly writing, far too large for a
    one-line reply."""
    missing = sorted(
        {
            workflow
            for _p, _l, workflow, _mt, _to in _call_sites()
            if workflow and workflow not in core.MODEL_CALL_POLICY
        }
    )
    assert not missing, f"no MODEL_CALL_POLICY row for: {missing}"


def test_every_policy_is_projected_from_the_workflow_registry():
    from agent.workflow_registry import WORKFLOW_SPECS

    assert set(core.MODEL_CALL_POLICY) == set(WORKFLOW_SPECS)
    for name, spec in WORKFLOW_SPECS.items():
        policy = core.MODEL_CALL_POLICY[name]
        assert (policy.max_tokens, policy.effort, policy.timeout) == (
            spec.max_tokens,
            spec.effort,
            spec.timeout,
        )


def test_leader_action_feedback_has_room_for_a_compact_profile():
    """Two live feedback profiles exhausted 1,200 output tokens on 2026-08-22.

    This internal JSON includes a summary, bounded guidance, and cited examples;
    it needs output headroom beyond the former cap even though it is toolless.
    """
    assert core.policy_for("leader_action_feedback").max_tokens >= 2048


def test_newly_registered_direct_workflows_preserve_their_effective_model_family():
    from agent.workflow_registry import workflow_model_family

    # Before registration these names all took the safe lightweight fallback.
    # Consolidation must not silently promote one-line or evaluator calls.
    for workflow in ("event_blurb", "help", "war_intel", "post_quality_eval"):
        assert workflow_model_family(workflow) == "lightweight"


@pytest.mark.parametrize("workflow,policy", sorted(core.MODEL_CALL_POLICY.items()))
def test_policy_values_are_sane(workflow, policy):
    assert policy.effort in ("low", "medium", "high", "xhigh", "max"), workflow
    # 120 is the smallest real ceiling in use (a binary triage); 16384
    # is the largest. Outside that range is a typo, not a decision.
    assert 120 <= policy.max_tokens <= 16384, workflow
    # A timeout is retried twice by the SDK, so the real wall clock before the
    # caller learns anything is timeout x 3. 300s is already 15 minutes; more
    # than that is a hang, not a slow call.
    assert 15 <= policy.timeout <= 300, workflow


# Longest SUCCESSFUL call observed per workflow, in seconds, from the telemetry
# database on 2026-08-09. A timeout at or under these has already been proven to
# clip real work — and clipping costs 3x the timeout in wall clock, because the
# SDK retries twice before giving up. All five recorded timeout failures took
# ~181.7s, which is 60 x 3 exactly.
OBSERVED_MAX_SECONDS = {
    "awareness": 152.4,
    "release_notes": 111.1,
    "deck_review": 104.8,
    "weekly_recap_email": 102.8,
    "memory_synthesis": 87.3,
    "leader_action_feedback": 60.0,
    "weekly_recap": 53.8,
    "ask_elixir_daily": 53.7,
    "wake_response_chat": 49.7,
    "interactive": 41.0,
    "awareness_repair": 38.1,
    "recruiting_copy": 30.2,
    "intel_report": 25.8,
    "member_report": 24.7,
    "clanops": 23.6,
    "screenshot_readout": 21.8,
}


@pytest.mark.parametrize("workflow,observed", sorted(OBSERVED_MAX_SECONDS.items()))
def test_timeout_clears_the_longest_observed_call(workflow, observed):
    """Every one of these has actually taken this long and succeeded."""
    timeout = core.policy_for(workflow).timeout
    assert timeout > observed, (
        f"{workflow} has completed in {observed}s but its timeout is {timeout}s; "
        "a timeout under the working duration costs 3x the timeout in wall clock "
        "(two SDK retries) and then fails anyway"
    )


def test_timeouts_leave_real_margin_over_observed_work():
    """Not just above the maximum — comfortably above it. Two workflows used to
    sit right on the line: deck_review ran at the 60s default with a 104.8s
    success (a retry-rescued call), and leader_action_feedback's longest success
    is 60.0s to the tenth of a second."""
    tight = {
        workflow: (observed, core.policy_for(workflow).timeout)
        for workflow, observed in OBSERVED_MAX_SECONDS.items()
        if core.policy_for(workflow).timeout < observed * 1.3
    }
    assert not tight, f"timeout within 30% of observed working duration: {tight}"


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
