"""journal.py — rolling Elixir messages for poapkings.com website.

Maintains a small JSON file (up to MAX_MESSAGES) that powers a speech-bubble
widget on the site.  Each message is a short editorial written in Elixir's
voice about recent clan activity.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone

log = logging.getLogger("journal")

ELIXIR_JSON_FILENAME = "src/_data/elixir.json"
MAX_MESSAGES = 5


def _json_path(repo_path: str) -> str:
    return os.path.join(repo_path, ELIXIR_JSON_FILENAME)


def load_messages(repo_path: str) -> list:
    """Load the current message list from elixir.json."""
    path = _json_path(repo_path)
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("messages", [])


def save_message(repo_path: str, text: str) -> dict:
    """Add a message, trim to MAX_MESSAGES, and write the file. Returns the new message."""
    message = {
        "text": text,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    path = _json_path(repo_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    messages = load_messages(repo_path)
    messages.append(message)
    messages = messages[-MAX_MESSAGES:]

    with open(path, "w") as f:
        json.dump({"messages": messages}, f, indent=2)

    return message


def commit_and_push(repo_path: str) -> bool:
    """Commit and push elixir.json. Returns True on success."""
    try:
        subprocess.run(
            ["git", "add", ELIXIR_JSON_FILENAME],
            cwd=repo_path, check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_path, capture_output=True,
        )
        if result.returncode == 0:
            return True  # nothing to commit
        subprocess.run(
            ["git", "commit", "-m", "Elixir daily update"],
            cwd=repo_path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_path, check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        log.error("git error: %s", e)
        return False
