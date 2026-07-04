"""OfflineEngine — the rehearsal seam (migration.md Phase 0 item 4 / Phase 6).

Replays archived raw payloads through ingest → emit → project, then runs
recognition + delivery once at finish() with a stub sender (no API, no
Discord, no LLM — composition falls back to the deterministic renderer).
scripts/migrate_v51/rehearsal.py drives this.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

from engine import delivery, ingest, projections, recognition
from engine.clock import infer_season_id, war_clock
from engine.db import canon_tag, connect, ensure_player
from engine.emitters import emit
from engine.emitters.clan import project_clan_aspects
from engine.emitters.player import project_player_aspects
from engine.emitters.war import project_race_aspect
from engine.recognition.compose import render_intent

HOME_CLAN = "#J2RGCRVG"


class OfflineEngine:
    def __init__(self, db_path: str, archive_path: str = "elixir-v5-archive-2026H2.db"):
        if not os.path.exists(db_path):
            from scripts.migrate_v51.schema_v51 import build

            build(db_path, archive_path)
        self.conn = connect(db_path)
        self.clock = None
        self.counters: dict[str, int] = {}
        self._touched: set[str] = set()

    def _count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def apply(self, endpoint: str, entity_key: str, payload_json: str, fetched_at: str) -> None:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            self._count("bad_payload")
            return
        now = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        if endpoint == "clan":
            self._count("clan_polls")
            for aspect, aspect_payload in project_clan_aspects(payload).items():
                self._count(
                    "events", emit(self.conn, "clan", HOME_CLAN, aspect, aspect_payload, fetched_at)
                )
        elif endpoint == "currentriverrace":
            self._count("race_polls")
            season_id = infer_season_id(self.conn, payload)
            self.clock = war_clock(payload, now, season_id=season_id)
            if self.clock.season_id is not None:
                race_aspect = project_race_aspect(payload, self.clock.season_id)
                self._count(
                    "events",
                    emit(self.conn, "riverrace", HOME_CLAN, "race", race_aspect, fetched_at),
                )
        elif endpoint == "player":
            tag = canon_tag(entity_key)
            ensure_player(self.conn, tag, payload.get("name"), fetched_at)
            self._count("profile_polls")
            for aspect, aspect_payload in project_player_aspects(payload).items():
                self._count("events", emit(self.conn, "player", tag, aspect, aspect_payload, fetched_at))
            self._touched.add(tag)
        elif endpoint == "player_battlelog":
            tag = canon_tag(entity_key)
            ensure_player(self.conn, tag, None, fetched_at)
            self._count("battlelog_polls")
            self._count(
                "battles",
                ingest.mirror_battles(self.conn, tag, payload, fetched_at, self.clock, now=now),
            )
            self._touched.add(tag)
        # riverracelog / cards / events / clan_by_tag: no offline consumer
        self.conn.commit()

    def finish(self, now: datetime | None = None) -> dict:
        """Run recognition + delivery once. `now` freezes the wall clock so
        the time-travel simulator (scripts/simulate.py) is deterministic;
        rehearsal callers omit it and get real time as before."""
        now_iso = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sent: list[tuple[str, str]] = []

        def send_fn(lane: str, copy: str) -> str:
            sent.append((lane, copy))
            return f"offline-{len(sent)}"

        def compose_fn(intent) -> str | None:
            return render_intent(intent)  # deterministic; no LLM offline

        for tag in sorted(self._touched):
            projections.refresh_form(self.conn, tag, now=now_iso)
            projections.refresh_management_inputs(self.conn, tag, now=now_iso)
        clock_dict = asdict(self.clock) if self.clock is not None else None
        rec = recognition.run_recognizers(self.conn, clock_dict, now_iso)
        d = delivery.consume(self.conn, send_fn, compose_fn, now_iso)
        self.conn.commit()
        out = dict(self.counters)
        out.update({f"recognize_{k}": v for k, v in rec.items()})
        out.update({f"deliver_{k}": v for k, v in d.items()})
        out["posts_composed"] = len(sent)
        return out
