"""War projections — rebuilt read models from RiverRace events.

Two projections mirror the legacy parity tables:

- WarCurrentStateProjection -> war_current_state_proj
  Latest observed live war-state per clan. Legacy war_current_state is a single
  global "slide" table (latest row per content change, one logical clan); we fold
  CurrentStateObserved into one row per (clan_tag, season, section) and the parity
  check compares the most-recent row per clan against legacy's latest. Each
  CurrentStateObserved already represents a *changed* content state (aggregate
  dedup), so the projection holds the final state of each war.

- WarParticipationProjection -> war_participation_proj
  Per (war key, player_tag) finalized standing from the river race log. Mirrors
  legacy war_participation (which keys on war_race_id + player_tag).
"""
from __future__ import annotations

from event_core.domain.riverrace import CURRENT_STATE_FIELDS, PARTICIPANT_FIELDS
from event_core.projections.runner import ProjectionRunner

_STATE_TEXT = {"war_state", "clan_tag", "clan_name", "period_type"}
_PART_TEXT = {"player_tag", "player_name"}


class WarCurrentStateProjection(ProjectionRunner):
    name = "war_current_state_proj"
    aggregate_name = "RiverRace"

    def setup(self) -> None:
        cols = ",\n            ".join(
            f"{a} {'TEXT' if a in _STATE_TEXT else 'INTEGER'}"
            for a in CURRENT_STATE_FIELDS.values()
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS war_current_state_proj (
                aggregate_id TEXT PRIMARY KEY,
                season_id    INTEGER,
                observed_at  TEXT,
                {cols}
            )
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS war_current_state_proj")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        agg_id = str(event.originator_id)
        if cls == "Registered":
            self.conn.execute(
                "INSERT OR IGNORE INTO war_current_state_proj(aggregate_id, season_id) VALUES(?,?)",
                (agg_id, event.season_id),
            )
        elif cls == "CurrentStateObserved":
            obs = dict(event.observation)
            sets = ["observed_at=?"]
            vals = [event.observed_at]
            for col in CURRENT_STATE_FIELDS.values():
                if col in obs:
                    sets.append(f"{col}=?")
                    vals.append(obs[col])
            vals.append(agg_id)
            self.conn.execute(
                f"UPDATE war_current_state_proj SET {', '.join(sets)} WHERE aggregate_id=?",
                vals,
            )


class WarParticipationProjection(ProjectionRunner):
    name = "war_participation_proj"
    aggregate_name = "RiverRace"

    def setup(self) -> None:
        cols = ",\n            ".join(
            f"{f} {'TEXT' if f in _PART_TEXT else 'INTEGER'}" for f in PARTICIPANT_FIELDS
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS war_participation_proj (
                aggregate_id  TEXT,
                clan_tag      TEXT,
                season_id     INTEGER,
                section_index INTEGER,
                {cols},
                observed_at   TEXT,
                PRIMARY KEY (aggregate_id, player_tag)
            )
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS war_participation_proj")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        agg_id = str(event.originator_id)
        if cls != "LogStandingObserved":
            return
        # We need the war key on each row; pull it from the aggregate, which is
        # cheap (snapshotting keeps loads bounded) and deterministic on replay.
        agg = self.app.repository.get(event.originator_id)
        for p in event.participants:
            cols = ["aggregate_id", "clan_tag", "season_id", "section_index"]
            vals = [agg_id, agg.clan_tag, agg.season_id, agg.section_index]
            for f in PARTICIPANT_FIELDS:
                cols.append(f)
                vals.append(p[f])
            cols.append("observed_at")
            vals.append(event.observed_at)
            placeholders = ",".join("?" for _ in cols)
            update = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("aggregate_id",))
            self.conn.execute(
                f"INSERT INTO war_participation_proj({','.join(cols)}) VALUES({placeholders}) "
                f"ON CONFLICT(aggregate_id, player_tag) DO UPDATE SET {update}",
                vals,
            )
