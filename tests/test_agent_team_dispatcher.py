from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "AGENT-TEAM/scripts/dispatcher.py"
SPEC = importlib.util.spec_from_file_location("agent_team_dispatcher", MODULE_PATH)
assert SPEC and SPEC.loader
dispatcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dispatcher
SPEC.loader.exec_module(dispatcher)


@pytest.fixture
def config():
    return dispatcher.load_config(ROOT / "AGENT-TEAM/dispatch.toml")


def issue(number: int, *labels: str, state: str = "OPEN"):
    return dispatcher.Issue(
        number=number,
        title=f"Issue {number}",
        state=state,
        labels=frozenset(labels),
        created_at=f"2026-07-{number:02d}T00:00:00Z",
        updated_at=f"2026-07-{number:02d}T00:00:00Z",
        url=f"https://example.test/issues/{number}",
    )


def test_config_routes_all_elixir_roles(config):
    assert set(config.routes) == {
        "dispatch:operations",
        "dispatch:build",
        "dispatch:evaluator",
        "dispatch:data",
        "dispatch:quality",
        "dispatch:product",
        "dispatch:manager",
    }
    assert len({route.priority for route in config.routes.values()}) == len(config.routes)
    assert len({route.role for route in config.routes.values()}) == len(config.routes)
    assert all(route.role_file.is_file() for route in config.routes.values())


def test_role_files_resolve_against_the_checkout_not_the_configured_cwd(tmp_path):
    """Role files ship in the repo, so they must resolve against the checkout.

    `cwd` in dispatch.toml is the operator's absolute working directory. Resolving
    role files through it meant the assertion above could only ever pass on that
    one machine -- it passed on every laptop run and failed the moment CI (a Linux
    checkout with no /Users/otto) saw it. Rewriting cwd to a path that does not
    exist reproduces CI locally, so this class of drift fails where we can see it.
    """
    checkout = tmp_path / "elixir-bot"
    (checkout / "AGENT-TEAM").mkdir(parents=True)
    for name in ("dispatch.toml", *(r.name for r in (ROOT / "AGENT-TEAM").glob("*.md"))):
        (checkout / "AGENT-TEAM" / name).write_bytes((ROOT / "AGENT-TEAM" / name).read_bytes())

    config_path = checkout / "AGENT-TEAM/dispatch.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f'cwd = "{dispatcher.load_config(ROOT / "AGENT-TEAM/dispatch.toml").cwd}"',
            'cwd = "/nonexistent/somewhere-else"',
        ),
        encoding="utf-8",
    )
    relocated = dispatcher.load_config(config_path)

    assert all(route.role_file.is_file() for route in relocated.routes.values())
    assert relocated.cwd == Path("/nonexistent/somewhere-else")
    assert relocated.repo_root == checkout


def test_explicit_dispatch_wins_over_inference(config):
    selected = dispatcher.infer_route(issue(1, "bug", "needs-eval", "dispatch:quality"), config)
    assert selected is not None
    route, source = selected
    assert route.label == "dispatch:quality"
    assert source == "explicit"


@pytest.mark.parametrize("stop", ["proposal", "blocked", "needs-design"])
def test_human_stop_labels_prevent_dispatch(config, stop):
    assert dispatcher.infer_route(issue(1, "bug", "dispatch:build", stop), config) is None


def test_multiple_explicit_routes_fail_closed(config):
    with pytest.raises(dispatcher.DispatchError, match="multiple dispatch labels"):
        dispatcher.infer_route(issue(1, "dispatch:build", "dispatch:operations"), config)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("bug", "needs-deploy"), "dispatch:operations"),
        (("needs-data",), "dispatch:data"),
        (("needs-quality",), "dispatch:quality"),
        (("needs-eval",), "dispatch:evaluator"),
        (("regression",), "dispatch:build"),
        (("approved", "ready", "meta"), "dispatch:build"),
        (("approved", "ready", "eval"), "dispatch:evaluator"),
        (("data",), "dispatch:product"),
        (("quality",), "dispatch:product"),
        (("operations",), "dispatch:operations"),
    ],
)
def test_inference_routes_legacy_issue_states(config, labels, expected):
    selected = dispatcher.infer_route(issue(1, *labels), config)
    assert selected is not None
    assert selected[0].label == expected
    assert selected[1] == "inferred"


def test_any_active_claim_serializes_the_shared_checkout(config):
    issues = [
        issue(1, "dispatch:build"),
        issue(2, "wip", "dispatch:quality"),
    ]
    assert dispatcher.select_candidates(issues, config) == []
    assert [item.number for item in dispatcher.active_claims(issues, config)] == [2]


def test_route_priority_then_explicit_state_orders_candidates(config):
    candidates = dispatcher.select_candidates(
        [
            issue(1, "needs-eval"),
            issue(2, "dispatch:build"),
            issue(3, "bug"),
            issue(4, "dispatch:operations"),
        ],
        config,
    )
    assert [item.issue.number for item in candidates] == [4, 2, 3, 1]


def test_handoff_prompt_requires_explicit_claim(config):
    route = config.routes["dispatch:build"]
    with pytest.raises(dispatcher.DispatchError, match="not claimed"):
        dispatcher.build_handoff_prompt(issue(245, "dispatch:build"), route, config)


def test_handoff_prompt_encodes_visible_task_and_authoritative_transition(config):
    route = config.routes["dispatch:build"]
    prompt = dispatcher.build_handoff_prompt(issue(245, "wip", "dispatch:build"), route, config)
    assert "normal local Codex project task" in prompt
    assert "`#245 Build`" in prompt
    assert "AGENTS.md" in prompt
    assert "AGENT-TEAM/WORKFLOW.md" in prompt
    assert "AGENT-TEAM/README.md" in prompt
    assert "AGENT-TEAM/build-manager.md" in prompt
    assert "Accept this" in prompt
    assert "specific claim as yours" in prompt
    assert "exactly one\nnext `dispatch:*` label" in prompt
    assert "Never invoke the next role directly" in prompt
    assert "Do not use a subagent, `codex exec`, launchd" in prompt


def test_transition_requires_current_route_to_clear(config):
    transition = dispatcher.assess_transition(
        "dispatch:build", issue(245, "dispatch:build"), config
    )
    assert not transition.valid
    assert transition.outcome == "current dispatch label remains"


def test_transition_accepts_one_next_route(config):
    transition = dispatcher.assess_transition(
        "dispatch:build", issue(245, "needs-deploy", "dispatch:operations"), config
    )
    assert transition.valid
    assert transition.outcome == "handoff"
    assert transition.next_route == "dispatch:operations"


def test_transition_accepts_closed_or_human_stop(config):
    assert dispatcher.assess_transition(
        "dispatch:product", issue(245, state="CLOSED"), config
    ).valid
    stopped = dispatcher.assess_transition("dispatch:product", issue(245, "proposal"), config)
    assert stopped.valid
    assert stopped.outcome == "human-stop"


def test_transition_rejects_unreleased_claim_even_when_closed(config):
    transition = dispatcher.assess_transition(
        "dispatch:build", issue(245, "wip", state="CLOSED"), config
    )
    assert not transition.valid
    assert transition.outcome == "serial claim remains"


def test_open_orphan_is_not_success(config):
    transition = dispatcher.assess_transition("dispatch:evaluator", issue(245, "eval"), config)
    assert not transition.valid
    assert "no next dispatch" in transition.outcome


def test_cli_refuses_automatic_launch(monkeypatch, config, capsys):
    called = False

    def unexpected_shadow(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("automatic launch path should not exist")

    monkeypatch.setattr(dispatcher, "shadow", unexpected_shadow)
    assert dispatcher.main(["--config", str(config.path)]) == 2
    assert not called
    assert "Automatic role launch does not exist" in capsys.readouterr().err
    assert not hasattr(dispatcher, "run_role")
    assert not hasattr(dispatcher, "LAUNCH_AGENT")


def test_automation_registry_keeps_only_windowed_or_recovery_schedules_active(config):
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    entries = {item["id"]: item for item in plan["automation"]}
    assert entries["elixir-build-manager"]["status"] == "PAUSED"
    assert entries["elixir-build-manager"]["schedule_kind"] == "event_driven"
    assert all(
        item["schedule_kind"] in {"time_window", "recovery"}
        for item in entries.values()
        if item["status"] == "ACTIVE"
    )
    assert {item["dispatch_label"] for item in entries.values()} == set(config.routes)
