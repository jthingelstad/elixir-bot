from __future__ import annotations

import importlib.util
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


automation_audit = _load("agent_team_automation_audit", "AGENT-TEAM/scripts/automation_audit.py")
objective_lease = _load("agent_team_objective_lease", "AGENT-TEAM/scripts/objective_lease.py")


def test_registry_has_exactly_three_active_objective_owners():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    entries = plan["automation"]

    assert plan["version"] == 4
    assert len(entries) == 3
    assert {entry["objective"] for entry in entries} == {"run", "game", "agent"}
    assert all(entry["status"] == "ACTIVE" for entry in entries)
    assert all((ROOT / entry["role_file"]).is_file() for entry in entries)
    assert all("dispatch_label" not in entry for entry in entries)


def test_automation_prompt_encodes_end_to_end_ownership_and_human_boundary():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    for entry in plan["automation"]:
        prompt = automation_audit._prompt(entry)
        assert entry["objective"] in prompt
        assert "Measure live evidence" in prompt
        assert "source fix" in prompt
        assert "instead of creating role handoff tickets" in prompt
        assert "local objective lease" in prompt
        assert "member-visible and irreversible human boundary" in prompt
        assert "Current state" in prompt
        assert "Active watches" in prompt
        assert "one replace-in-place Latest run" in prompt


def test_workflow_pins_acceptance_and_memory_ownership():
    workflow = (ROOT / "AGENT-TEAM/WORKFLOW.md").read_text()
    run = (ROOT / "AGENT-TEAM/run-elixir.md").read_text()
    improve = (ROOT / "AGENT-TEAM/improve-elixir.md").read_text()

    assert "Run Elixir owns deployment acceptance" in workflow
    assert "objective that originated a change owns semantic acceptance" in workflow
    assert "Current state" in workflow
    assert "Active watches" in workflow
    assert "Replace `Latest run` on every pass" in workflow
    assert "Once per ISO week" in run
    assert "On Friday, also take a small team-health pulse" in improve


def test_audit_rejects_any_plan_other_than_one_owner_per_objective(tmp_path):
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    duplicate = {"automation": [plan["automation"][0], plan["automation"][0]]}

    _, failures = automation_audit.audit(duplicate, codex_home=tmp_path)

    assert "plan must contain exactly one run, game, and agent objective" in failures


def test_checkout_lease_is_atomic_and_owner_scoped(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    claimed = objective_lease.claim("run", now=now)
    assert claimed == {"objective": "run", "claimed_at": "2026-08-11T12:00:00Z"}
    assert lease_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(SystemExit, match="already held"):
        objective_lease.claim("game", now=now)
    with pytest.raises(SystemExit, match="belongs to 'run'"):
        objective_lease.release("agent")

    objective_lease.release("run")
    assert not lease_path.exists()


def test_stale_lease_requires_age_and_clean_worktree(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    claimed_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    objective_lease.claim("game", now=claimed_at)

    with pytest.raises(SystemExit, match="only"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(
        objective_lease.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=" M something.py\n"),
    )
    with pytest.raises(SystemExit, match="worktree is dirty"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(
        objective_lease.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=""),
    )
    cleared = objective_lease.clear_stale(
        hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    )
    assert cleared["objective"] == "game"
    assert not lease_path.exists()


def test_retired_dispatcher_files_are_absent():
    assert not (ROOT / "AGENT-TEAM/dispatch.toml").exists()
    assert not (ROOT / "AGENT-TEAM/dispatcher.md").exists()
    assert not (ROOT / "AGENT-TEAM/scripts/dispatcher.py").exists()
