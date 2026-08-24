from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

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
automation_memory = _load("agent_team_automation_memory", "AGENT-TEAM/scripts/automation_memory.py")
prepare_commit = _load("agent_team_prepare_commit", "AGENT-TEAM/scripts/prepare_commit.py")
PREFLIGHT = ROOT / "AGENT-TEAM/scripts/preflight.sh"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run_preflight(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PREFLIGHT)], cwd=cwd, check=False, capture_output=True, text=True
    )


@pytest.fixture
def synchronized_git_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    _git(tmp_path, "init", "--initial-branch=main", str(repo))
    _git(repo, "config", "user.name", "Agent Team Tests")
    _git(repo, "config", "user.email", "agent-team-tests@example.invalid")
    (repo / "tracked.txt").write_text("initial\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    return repo, remote


def test_registry_has_exactly_three_active_objective_owners():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    entries = plan["automation"]

    assert plan["version"] == 2
    assert plan["repo"] == "."
    assert len(entries) == 3
    assert {entry["objective"] for entry in entries} == {"run", "game", "agent"}
    assert all(entry["status"] == "ACTIVE" for entry in entries)
    assert all((ROOT / entry["objective_file"]).is_file() for entry in entries)
    assert all("dispatch_label" not in entry for entry in entries)


def test_automation_prompt_encodes_end_to_end_ownership_and_human_boundary():
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    for entry in plan["automation"]:
        prompt = automation_audit.prompt(entry)
        assert entry["objective"] in prompt
        assert "Measure current evidence" in prompt
        assert "source fix" in prompt
        assert "Use issues only for multi-run work" in prompt
        assert "objective lease only before mutation" in prompt
        assert "human and privacy boundaries" in prompt
        assert "Current state" in prompt
        assert "Active watches" in prompt
        assert "one replace-in-place Latest run" in prompt
        assert "automation_memory.py" in prompt


def test_workflow_pins_acceptance_and_memory_ownership():
    workflow = (ROOT / "AGENT-TEAM/WORKFLOW.md").read_text()
    readme = (ROOT / "AGENT-TEAM/README.md").read_text()
    run = (ROOT / "AGENT-TEAM/run-elixir.md").read_text()
    game = (ROOT / "AGENT-TEAM/understand-clash-royale.md").read_text()
    improve = (ROOT / "AGENT-TEAM/improve-elixir.md").read_text()

    assert "Run Elixir owns deployment acceptance" in workflow
    assert "origin-owner restart" in workflow
    assert "prepare_commit.py" in workflow
    assert "objective that originated a change owns semantic acceptance" in workflow
    assert "Current state" in workflow
    assert "Active watches" in workflow
    assert "Replace `Latest run` on every pass" in workflow
    assert "Outcome: HEALTHY | CHANGED | WATCHING | BLOCKED | NEEDS JAMIE" in workflow
    assert "Run <objective> now and own the highest-impact measured gap." in readme
    assert "Intelligence and efficiency remain objective-owned" in readme
    assert "Do not add a fourth Intelligence" in readme
    assert "Once per ISO week" in run
    assert "a lower bill" in run
    assert "alone is not proof of efficiency" in run
    assert "captured-but-unused data inventory" in game
    assert "intelligence and efficiency baseline" in improve
    assert "insufficient_sample" in improve
    assert "On Friday, also take a small team-health pulse" in improve
    assert "external_game_pulse.py" in game
    assert "Do not scrape" in game


def test_automation_memory_path_uses_env_or_local_fallback(tmp_path, monkeypatch):
    plan_path = tmp_path / "automations.toml"
    plan_path.write_text("[[automation]]\nid = 'elixir-data-analyst'\n")
    monkeypatch.setattr(automation_memory, "DEFAULT_CODEX_HOME", tmp_path / "fallback")

    assert automation_memory.memory_path("elixir-data-analyst", plan_path=plan_path) == (
        tmp_path / "fallback" / "automations" / "elixir-data-analyst" / "memory.md"
    )
    assert automation_memory.memory_path(
        "elixir-data-analyst", environ={"CODEX_HOME": "/tmp/codex"}, plan_path=plan_path
    ) == Path("/tmp/codex/automations/elixir-data-analyst/memory.md")
    with pytest.raises(ValueError, match="unknown automation id"):
        automation_memory.memory_path("unknown", plan_path=plan_path)


def test_prepare_commit_requires_safe_repository_relative_paths(tmp_path):
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('ok')\n")

    assert prepare_commit._relative_paths(["tracked.py"], cwd=tmp_path) == ["tracked.py"]
    with pytest.raises(ValueError, match="repository-relative"):
        prepare_commit._relative_paths(["../outside.py"], cwd=tmp_path)
    with pytest.raises(ValueError, match="not a file"):
        prepare_commit._relative_paths(["missing.py"], cwd=tmp_path)


def test_audit_rejects_any_plan_other_than_one_owner_per_objective(tmp_path):
    plan = tomllib.loads((ROOT / "AGENT-TEAM/automations.toml").read_text())
    duplicate = {"automation": [plan["automation"][0], plan["automation"][0]]}

    _, failures = automation_audit.audit(duplicate, codex_home=tmp_path)

    assert "objectives must have exactly one owner" in failures


def test_checkout_lease_is_atomic_and_owner_scoped(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    claimed = objective_lease.claim(
        "run",
        now=now,
        holder_id="thread-1",
        holder_pid=4321,
        hostname="test-host",
        starting_head="abc123",
        lease_id="lease-1",
    )
    assert claimed == {
        "objective": "run",
        "lease_id": "lease-1",
        "claimed_at": "2026-08-11T12:00:00Z",
        "holder_id": "thread-1",
        "holder_pid": 4321,
        "hostname": "test-host",
        "starting_head": "abc123",
    }
    assert lease_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(SystemExit, match="already held"):
        objective_lease.claim("game", now=now)
    with pytest.raises(SystemExit, match="belongs to 'run'"):
        objective_lease.release("agent", lease_id="lease-1")
    with pytest.raises(SystemExit, match="another run"):
        objective_lease.release("run", lease_id="wrong")

    monkeypatch.setattr(objective_lease, "_checkout_is_clean", lambda: True)
    objective_lease.release("run", lease_id="lease-1")
    assert not lease_path.exists()


def test_codex_override_is_lean_and_points_to_objective_contract():
    override = ROOT / "AGENTS.override.md"
    text = override.read_text()

    assert override.stat().st_size < 32 * 1024
    assert "AGENT-TEAM/WORKFLOW.md" in text
    assert "exception ledger" in text
    assert "NEEDS JAMIE" in text


def test_stale_lease_requires_proof_holder_is_gone_and_checkout_is_unchanged(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    monkeypatch.setattr(objective_lease.socket, "gethostname", lambda: "test-host")
    claimed_at = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    objective_lease.claim(
        "game",
        now=claimed_at,
        holder_id="thread-2",
        holder_pid=9876,
        hostname="test-host",
        starting_head="abc123",
    )

    with pytest.raises(SystemExit, match="only"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc))

    checkout = {"dirty": True, "head": "abc123"}

    def fake_git(*args):
        if args == ("status", "--porcelain"):
            return " M something.py" if checkout["dirty"] else ""
        if args == ("rev-parse", "HEAD"):
            return checkout["head"]
        raise AssertionError(args)

    monkeypatch.setattr(objective_lease, "_git", fake_git)
    with pytest.raises(SystemExit, match="worktree is dirty"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))

    checkout["dirty"] = False
    checkout["head"] = "def456"
    with pytest.raises(SystemExit, match="HEAD changed"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))

    checkout["head"] = "abc123"
    monkeypatch.setattr(objective_lease, "_process_exists", lambda pid: True)
    with pytest.raises(SystemExit, match="still active"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(objective_lease, "_process_exists", lambda pid: False)
    cleared = objective_lease.clear_stale(
        hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    )
    assert cleared["objective"] == "game"
    assert not lease_path.exists()


def test_stale_lease_without_durable_process_requires_manual_clear(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    monkeypatch.setattr(objective_lease.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(
        objective_lease,
        "_git",
        lambda *args: "abc123" if args == ("rev-parse", "HEAD") else "",
    )
    objective_lease.claim(
        "agent",
        now=datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc),
        holder_id="thread-3",
        hostname="test-host",
        starting_head="abc123",
    )

    with pytest.raises(SystemExit, match="no durable holder process"):
        objective_lease.clear_stale(hours=8, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc))
    with pytest.raises(SystemExit, match="requires --confirm-inactive"):
        objective_lease.clear_manual(holder_id="thread-3", confirm_inactive=False)
    with pytest.raises(SystemExit, match="not 'wrong-thread'"):
        objective_lease.clear_manual(holder_id="wrong-thread", confirm_inactive=True)

    cleared = objective_lease.clear_manual(holder_id="thread-3", confirm_inactive=True)
    assert cleared["objective"] == "agent"
    assert not lease_path.exists()


def test_legacy_lease_requires_explicit_compatibility_holder(tmp_path, monkeypatch):
    lease_path = tmp_path / ".git" / "agent-team-objective-lease.json"
    lease_path.parent.mkdir()
    lease_path.write_text('{"claimed_at":"2026-08-11T00:00:00Z","objective":"run"}\n')
    monkeypatch.setattr(objective_lease, "LEASE_PATH", lease_path)
    monkeypatch.setattr(objective_lease, "_checkout_is_clean", lambda: True)

    with pytest.raises(SystemExit, match="legacy-unidentified"):
        objective_lease.clear_manual(holder_id="unknown", confirm_inactive=True)

    cleared = objective_lease.clear_manual(holder_id="legacy-unidentified", confirm_inactive=True)
    assert cleared["objective"] == "run"
    assert not lease_path.exists()


def test_preflight_accepts_only_clean_synchronized_main(synchronized_git_repo):
    repo, _ = synchronized_git_repo

    result = _run_preflight(repo)

    assert result.returncode == 0
    assert "clean main exactly synchronized with origin/main" in result.stdout


def test_preflight_fails_when_fetch_fails(synchronized_git_repo, tmp_path):
    repo, _ = synchronized_git_repo
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "git fetch origin failed" in result.stdout
    assert "safe to work" not in result.stdout


def test_preflight_rejects_non_main_and_untracked_branch(synchronized_git_repo):
    repo, _ = synchronized_git_repo
    _git(repo, "switch", "--create", "feature")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "branch must be main" in result.stdout
    assert "no upstream configured" in result.stdout


def test_preflight_rejects_detached_head(synchronized_git_repo):
    repo, _ = synchronized_git_repo
    _git(repo, "switch", "--detach")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "detached HEAD" in result.stdout


def test_preflight_rejects_ahead_main(synchronized_git_repo):
    repo, _ = synchronized_git_repo
    (repo / "tracked.txt").write_text("ahead\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "ahead")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "AHEAD of origin/main by 1" in result.stdout
    assert "safe to work" not in result.stdout


def test_preflight_rejects_main_without_upstream(synchronized_git_repo):
    repo, _ = synchronized_git_repo
    _git(repo, "branch", "--unset-upstream")

    result = _run_preflight(repo)

    assert result.returncode != 0
    assert "no upstream configured for main" in result.stdout


def test_active_error_watch_uses_objective_routing():
    runbook = (ROOT / "AGENT-TEAM/error-watch.md").read_text()

    assert "**Data Analyst**" not in runbook
    assert "**Lane:**" not in runbook
    assert "Understand Clash Royale" in runbook
    assert "objective:game" in runbook


def test_retired_dispatcher_files_are_absent():
    assert not (ROOT / "AGENT-TEAM/dispatch.toml").exists()
    assert not (ROOT / "AGENT-TEAM/dispatcher.md").exists()
    assert not (ROOT / "AGENT-TEAM/scripts/dispatcher.py").exists()
