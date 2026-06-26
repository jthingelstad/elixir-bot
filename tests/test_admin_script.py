from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _admin_script_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    project = tmp_path / "repo"
    scripts_dir = project / "scripts"
    venv_bin = project / "venv" / "bin"
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

    (venv_bin / "activate").write_text(
        'export PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):$PATH"\n',
        encoding="utf-8",
    )
    python_stub = venv_bin / "python"
    python_stub.write_text(
        "#!/bin/bash\n"
        'echo "python $*" >> "$ADMIN_TEST_LOG"\n'
        'exit "${ADMIN_TEST_PYTHON_EXIT:-0}"\n',
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    launchctl_stub = tools_dir / "launchctl"
    launchctl_stub.write_text(
        "#!/bin/bash\n"
        'echo "launchctl $*" >> "$ADMIN_TEST_LOG"\n'
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
    backup_index = next(i for i, line in enumerate(calls) if line.startswith("python "))
    stop_index = next(i for i, line in enumerate(calls) if " bootout " in line)
    start_index = next(i for i, line in enumerate(calls) if " bootstrap " in line)
    assert backup_index < stop_index < start_index


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


def test_activity_run_uses_registered_activity_runner(tmp_path):
    admin_script, env, log_path = _admin_script_fixture(tmp_path)

    result = subprocess.run(
        ["bash", str(admin_script), "activity", "run", "v5-reactive-tick"],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert calls == ["python -m runtime.activity_runner run v5-reactive-tick"]
