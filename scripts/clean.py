#!/usr/bin/env python3
"""Clean local development cruft from the repo.

By default this removes transient cache/build directories only.
Use --db to also remove local runtime files like `elixir.db` and `elixir.pid`.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
SKIP_ROOT_DIRS = {
    ".git",
    "venv",
}
OPTIONAL_FILES = {
    ROOT / "elixir.db",
    ROOT / "elixir.pid",
}


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove local caches and optional runtime artifacts.")
    parser.add_argument("--db", action="store_true", help="Also remove local SQLite and PID files.")
    args = parser.parse_args()

    removed: list[str] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_ROOT_DIRS for part in path.parts):
            continue
        if path.name in CACHE_DIR_NAMES and _remove_path(path):
            removed.append(str(path.relative_to(ROOT)))

    if args.db:
        for path in sorted(OPTIONAL_FILES):
            if _remove_path(path):
                removed.append(str(path.relative_to(ROOT)))

    if removed:
        print("Removed:")
        for item in removed:
            print(f"- {item}")
    else:
        print("Nothing to remove.")


if __name__ == "__main__":
    main()
