#!/usr/bin/env python3
"""Format and stage only named current-run files before the full commit gate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path = REPO, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _relative_paths(paths: list[str], *, cwd: Path) -> list[str]:
    if not paths:
        raise ValueError("name at least one current-run file")
    normalized: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"path must be repository-relative: {raw}")
        resolved = cwd / path
        if not resolved.is_file():
            raise ValueError(f"not a file in this checkout: {raw}")
        normalized.append(path.as_posix())
    return list(dict.fromkeys(normalized))


def prepare(paths: list[str], *, cwd: Path = REPO) -> list[str]:
    """Format Python paths, then stage exactly the requested paths.

    Existing staged work outside ``paths`` is a safety error; this helper never
    turns an ambiguous shared index into a publishable commit.
    """
    names = _relative_paths(paths, cwd=cwd)
    staged_before = set(_git("diff", "--cached", "--name-only", cwd=cwd).stdout.splitlines())
    unexpected = staged_before - set(names)
    if unexpected:
        raise ValueError("unrelated staged files: " + ", ".join(sorted(unexpected)))

    python_names = [name for name in names if name.endswith(".py")]
    if python_names:
        subprocess.run([sys.executable, "-m", "ruff", "format", *python_names], cwd=cwd, check=True)
    _git("diff", "--check", cwd=cwd)
    _git("add", "--", *names, cwd=cwd)
    _git("diff", "--cached", "--check", cwd=cwd)

    staged_after = set(_git("diff", "--cached", "--name-only", cwd=cwd).stdout.splitlines())
    if not staged_after:
        raise ValueError("nothing staged after preparation")
    unexpected = staged_after - set(names)
    if unexpected:
        raise ValueError("staged files escaped requested scope: " + ", ".join(sorted(unexpected)))
    return sorted(staged_after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="repository-relative current-run files")
    args = parser.parse_args()
    try:
        for path in prepare(args.paths):
            print(f"staged {path}")
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"prepare-commit: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
