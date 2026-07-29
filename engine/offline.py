"""OfflineEngine — the rehearsal seam (migration.md Phase 0 item 4 / Phase 6).

Replays archived raw payloads through ingest → emit → project, with no API,
Discord, or LLM. This is general replay infrastructure for the LIVE tick path;
scripts/replay_gate.py drives it as a hard verification gate.

The optional ``legacy_proactive`` seam that invoked the deterministic
recognizer/renderer here was removed with that stack (#207): the awareness loop
is the sole proactive owner, and keeping a retired pipeline executable purely
for comparison is what the trim-and-fit sprint exists to stop.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from engine import materialize, observations
from engine.clock import infer_season_id
from engine.db import connect
from engine.normalize import parse_cr_time

HOME_CLAN = "#J2RGCRVG"


class OfflineEngine:
    def __init__(self, db_path: str, archive_path: str = "elixir-v5-archive-2026H2.db"):
        if not os.path.exists(db_path):
            from scripts.migrate_v51.schema_v51 import build

            build(db_path, archive_path)
        self.conn = connect(db_path)
        self.clock = None
        self.counters: dict[str, int] = {}
        # Replay starts from a database that already knows its historical
        # season boundaries. Freeze that timeline before apply() mutates any
        # war rows. Using infer_season_id's latest-section heuristic while
        # walking old payloads makes every replay advance the season number
        # again (s134 -> s135 -> s136), which is deterministic live but wrong
        # for historical playback.
        self._season_timeline: list[tuple[int, datetime, datetime | None]] = []
        for row in self.conn.execute(
            "SELECT season_id, started_at, ended_at FROM war_seasons "
            "WHERE started_at IS NOT NULL ORDER BY season_id"
        ).fetchall():
            start = parse_cr_time(row["started_at"])
            end = parse_cr_time(row["ended_at"])
            if start is not None:
                self._season_timeline.append((int(row["season_id"]), start, end))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> OfflineEngine:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _season_id_at(self, payload: dict, observed_at: datetime) -> int | None:
        live = payload.get("seasonId")
        if live is not None:
            return int(live)
        for season_id, start, end in reversed(self._season_timeline):
            if start <= observed_at and (end is None or observed_at < end):
                return season_id
        return infer_season_id(self.conn, payload)

    def apply(self, endpoint: str, entity_key: str, payload_json: str, fetched_at: str) -> None:
        try:
            payload = json.loads(payload_json)
        except TypeError, ValueError:
            self._count("bad_payload")
            return
        now = parse_cr_time(fetched_at)
        if now is None:
            self._count("bad_observed_at")
            return
        expected_key = HOME_CLAN if endpoint in {"clan", "currentriverrace"} else entity_key
        if endpoint in {"clan", "currentriverrace", "player", "player_battlelog"}:
            decision, observation = observations.observe(
                endpoint,
                expected_key,
                payload,
                fetched_at,
                source="offline_replay",
            )
            if not decision.accepted:
                self._count("observation_rejections")
                self._count(f"{endpoint}_observation_rejections")
                return
            self._count("observations_accepted")
            assert observation is not None
            season_id = self._season_id_at(payload, now) if endpoint == "currentriverrace" else None
            applied = materialize.apply_observation(
                self.conn,
                observation,
                clock=self.clock,
                now=now,
                season_id_override=season_id,
            )
            self._count("events", applied.events_emitted)
            self._count("battles", applied.battles_ingested)
            self._count("players_projected", applied.players_projected)
            if applied.clock is not None:
                self.clock = applied.clock
        if endpoint == "clan":
            self._count("clan_polls")
        elif endpoint == "currentriverrace":
            self._count("race_polls")
        elif endpoint == "player":
            self._count("profile_polls")
        elif endpoint == "player_battlelog":
            self._count("battlelog_polls")
        # riverracelog / cards / events / clan_by_tag: no offline consumer
        self.conn.commit()

    def finish(self, now: datetime | None = None) -> dict:
        """Commit projections and return the replay counters.

        Production :func:`engine.tick.run_tick` has no delivery surface — the
        awareness brain is the sole proactive poster — so offline replay has
        none either. ``now`` is accepted for signature compatibility with
        deterministic callers that freeze the wall clock.
        """
        self.conn.commit()
        out = dict(self.counters)
        out["proactive_mode"] = "awareness_only"
        return out
