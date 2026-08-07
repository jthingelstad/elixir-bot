from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _admin_script_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project = tmp_path / "repo"
    scripts_dir = project / "scripts"
    venv_bin = project / ".venv" / "bin"
    tools_dir = tmp_path / "tools"
    home_dir = tmp_path / "home"
    log_path = tmp_path / "admin-calls.log"

    scripts_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    tools_dir.mkdir()
    (home_dir / "Library" / "LaunchAgents").mkdir(parents=True)
    (home_dir / "Library" / "LaunchAgents" / "com.poapkings.elixir.plist").write_text(
        "fake plist\n",
        encoding="utf-8",
    )

    admin_script = scripts_dir / "admin.sh"
    shutil.copy(PROJECT_ROOT / "scripts" / "admin.sh", admin_script)
    admin_script.chmod(0o755)
    (scripts_dir / "backup_db.py").write_text("# fake backup entrypoint\n", encoding="utf-8")

    python_stub = venv_bin / "python"
    python_stub.write_text(
        '#!/bin/bash\necho "python $*" >> "$ADMIN_TEST_LOG"\nexit "${ADMIN_TEST_PYTHON_EXIT:-0}"\n',
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    # `launchctl list` has to emit a realistic table, because status() reads it.
    # Real output is TAB-separated: PID, last exit status, label. Tests override
    # ADMIN_TEST_LAUNCHCTL_LIST to simulate stopped / crash-looping states.
    launchctl_stub = tools_dir / "launchctl"
    launchctl_stub.write_text(
        "#!/bin/bash\n"
        'echo "launchctl $*" >> "$ADMIN_TEST_LOG"\n'
        'if [ "$1" = "list" ]; then\n'
        "  printf '%b\\n' \"${ADMIN_TEST_LAUNCHCTL_LIST-8537\\t0\\tcom.poapkings.elixir}\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launchctl_stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "ADMIN_TEST_LOG": str(log_path),
            "HOME": str(home_dir),
            "PATH": f"{tools_dir}:{env['PATH']}",
        }
    )
    return admin_script, env, log_path


def _status(tmp_path: Path, listing: str):
    """Run `admin.sh status` against a simulated `launchctl list` table."""
    admin_script, env, _log = _admin_script_fixture(tmp_path)
    env["ADMIN_TEST_LAUNCHCTL_LIST"] = listing
    return subprocess.run(
        ["bash", str(admin_script), "status"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )


def test_restart_backs_up_before_stopping_service(tmp_path):
    admin_script, env, log_path = _admin_script_fixture(tmp_path)

    result = subprocess.run(
        ["bash", str(admin_script), "restart"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8").splitlines()
    control_log = admin_script.parents[1] / "logs" / "elixir-control.log"
    control_line = control_log.read_text(encoding="utf-8").strip()
    backup_index = next(i for i, line in enumerate(calls) if line.startswith("python "))
    stop_index = next(i for i, line in enumerate(calls) if " bootout " in line)
    start_index = next(i for i, line in enumerate(calls) if " bootstrap " in line)
    assert backup_index < stop_index < start_index
    assert "action=restart" in control_line


def test_restart_aborts_without_stopping_when_backup_fails(tmp_path):
    admin_script, env, log_path = _admin_script_fixture(tmp_path)
    env["ADMIN_TEST_PYTHON_EXIT"] = "9"

    result = subprocess.run(
        ["bash", str(admin_script), "restart"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 9
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("python ") for line in calls)
    assert not any(" bootout " in line for line in calls)
    assert not any(" bootstrap " in line for line in calls)


def test_status_does_not_mistake_the_sibling_agent_for_the_bot(tmp_path):
    """The bug this pins: `com.poapkings.elixir-drop-cr-bridge` CONTAINS the
    bot's label, and status used a substring grep. On 2026-08-07 the bot was
    crash-looping on a Discord 503 and `restart` printed "elixir-bot is
    running." twice while it was down."""
    result = _status(tmp_path, "1521\\t0\\tcom.poapkings.elixir-drop-cr-bridge")

    assert "is running" not in result.stdout, result.stdout
    assert result.returncode != 0, "a stopped bot must not report success"


def test_status_reports_stopped_when_loaded_but_not_running(tmp_path):
    """The crash-loop state exactly: launchd still has the job, but column 1 is
    "-" because no process is alive. Column 2 is the LAST exit status, not the
    current one, so a non-zero there next to a live PID is history."""
    result = _status(tmp_path, "-\\t1\\tcom.poapkings.elixir")

    assert "STOPPED" in result.stdout, result.stdout
    assert result.returncode != 0


def test_status_reports_running_with_pid(tmp_path):
    result = _status(tmp_path, "8537\\t1\\tcom.poapkings.elixir")

    assert "is running" in result.stdout
    assert "8537" in result.stdout, "the pid makes the claim checkable"
    assert result.returncode == 0


def test_restart_fails_loudly_when_the_bot_does_not_come_up(tmp_path):
    """A restart that leaves the bot down must exit non-zero rather than print a
    reassuring line and return 0."""
    admin_script, env, _log = _admin_script_fixture(tmp_path)
    env["ADMIN_TEST_LAUNCHCTL_LIST"] = "-\\t1\\tcom.poapkings.elixir"

    result = subprocess.run(
        ["bash", str(admin_script), "restart"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0, result.stdout


def test_activity_run_uses_registered_activity_runner(tmp_path):
    admin_script, env, log_path = _admin_script_fixture(tmp_path)

    result = subprocess.run(
        ["bash", str(admin_script), "activity", "run", "engine-tick"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert calls == ["python -m runtime.activity_runner run engine-tick"]
