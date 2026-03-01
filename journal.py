"""journal.py — append-only log for Elixir observations -> elixir.json"""
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone

ELIXIR_JSON_FILENAME = "src/_data/elixir.json"


def _json_path(repo_path: str) -> str:
    return os.path.join(repo_path, ELIXIR_JSON_FILENAME)


def load_entries(repo_path: str) -> list:
    path = _json_path(repo_path)
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("entries", [])


def append_entry(repo_path: str, entry: dict) -> dict:
    """Append entry to elixir.json. Adds id + timestamp if missing. Returns final entry."""
    if "id" not in entry:
        entry["id"] = str(uuid.uuid4())
    if "timestamp" not in entry:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    path = _json_path(repo_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
    else:
        data = {"entries": []}

    data["entries"].append(entry)

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    return entry


def recent_entries(entries: list, n: int = 20) -> list:
    return entries[-n:]


def commit_and_push(repo_path: str, message: str) -> bool:
    """Commit and push elixir.json. Returns True on success."""
    try:
        subprocess.run(
            ["git", "add", ELIXIR_JSON_FILENAME],
            cwd=repo_path, check=True, capture_output=True
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_path, capture_output=True
        )
        if result.returncode == 0:
            return True  # nothing to commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_path, check=True, capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        import logging
        logging.getLogger("journal").error("git error: %s", e)
        return False
