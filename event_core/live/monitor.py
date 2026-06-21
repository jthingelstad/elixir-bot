"""One-shot v5 health monitor for post-go-live watching.

Read-only: service status, recent reactive-tick results, errors, follower lag,
detection/intent counts, recent posts. Run via `python -m event_core.live.monitor`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from event_core import config

LOG = Path(config.ROOT) / "elixir-v5.log"


def _service_up() -> bool:
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    return any("com.poapkings.elixir" in line for line in out.splitlines())


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()[-n:]


def snapshot() -> dict:
    lines = _tail(LOG, 400)
    ticks = [ln for ln in lines if "v5 reactive tick:" in ln][-3:]
    errors = [ln for ln in lines if re.search(r"ERROR|Traceback|Exception|catch-up failed", ln)
              and "PyNaCl" not in ln and "davey" not in ln][-8:]

    proj = sqlite3.connect(f"file:{config.PROJECTIONS_DB}?mode=ro", uri=True)
    proj.row_factory = sqlite3.Row
    try:
        tracking = {r["projection_name"]: r["last_global_position"]
                    for r in proj.execute("SELECT projection_name,last_global_position FROM projection_tracking")}
        detections = proj.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        consumer_pos = tracking.get("consumer:discord")
    finally:
        proj.close()

    return {
        "service_up": _service_up(),
        "recent_ticks": ticks or ["(no reactive tick yet)"],
        "recent_errors": errors or ["(none)"],
        "detections_projected": detections,
        "discord_consumer_position": consumer_pos,
        "follower_positions": tracking,
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2, default=str))
