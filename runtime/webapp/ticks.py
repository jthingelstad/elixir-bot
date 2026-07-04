"""In-memory engine-tick history for the /ticks page.

No persisted tick history exists — `runtime_job_status` is a latest-state
upsert per job and the tick counters live only in its `last_summary` (900-char
truncated). This ring buffer, fed from `_engine_tick`, is the in-process
answer: ~48 h of full counter dicts at the 10-minute cadence, gone on restart
(the page says so and falls back to the persisted latest row).
"""

from __future__ import annotations

import collections
from datetime import datetime, timezone

_TICKS: collections.deque = collections.deque(maxlen=288)  # ~48h at 10-min ticks


def record_tick(counters: dict) -> None:
    entry = dict(counters or {})
    entry.setdefault(
        "recorded_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _TICKS.appendleft(entry)


def recent_ticks(limit: int = 100) -> list[dict]:
    return [dict(t) for t in list(_TICKS)[: max(1, int(limit))]]
