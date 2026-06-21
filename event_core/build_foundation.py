"""Build the foundation slice end-to-end and report parity.

Clean-builds the v5 event store + projection DB from the frozen legacy archive:
backfill players -> run the current-profile projection -> exact-parity check.
Idempotent: deletes prior v5 events/projection DBs first so it is a from-zero
rebuild every run (this is also the replay-determinism harness).
"""
from __future__ import annotations

import json
import os

from event_core import config


def _rm(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def build(clean: bool = True) -> dict:
    if clean:
        _rm(config.EVENTS_DB)
        _rm(config.PROJECTIONS_DB)

    config.configure_eventstore_env(config.EVENTS_DB)

    from event_core import db
    from event_core.application import ObservedWorld
    from event_core.backfill import (
        backfill_battles,
        backfill_clan_state,
        backfill_clan_roster,
        backfill_clans,
        backfill_collections,
        backfill_players,
    )
    from event_core.clan_validate import check_clan_daily_metrics_parity
    from event_core.collections_validate import check_collections_parity
    from event_core.parity import (
        check_battle_telemetry_parity,
        check_member_current_state_parity,
        check_player_profile_parity,
    )
    from event_core.projections.clan_metrics import ClanDailyMetrics
    from event_core.projections.collections import PlayerCurrentCollections
    from event_core.projections.member_state import MemberCurrentState
    from event_core.projections.player_state import PlayerCurrentProfile
    from event_core.projections.roster_lifecycle import RosterLifecycle
    from event_core.projections.war import (
        WarCurrentStateProjection,
        WarParticipationProjection,
    )
    from event_core.war_backfill import backfill_currentriverrace, backfill_war_log
    from event_core.war_validate import (
        check_war_current_state_parity,
        check_war_participation_parity,
    )

    app = ObservedWorld()
    # Observed World ingest (one code path; backfill == live ingest fed history)
    bf_players = backfill_players(app)
    bf_clans = backfill_clans(app)
    bf_collections = backfill_collections(app)
    bf_clan_state = backfill_clan_state(app)
    bf_war_log = backfill_war_log(app, projections_path=config.PROJECTIONS_DB)
    bf_war_cur = backfill_currentriverrace(app, projections_path=config.PROJECTIONS_DB)
    bf_clan_roster = backfill_clan_roster(app)
    bf_battles = backfill_battles()

    conn = db.connect(config.PROJECTIONS_DB)
    applied = {}
    for name, proj in {
        "profile": PlayerCurrentProfile(app, conn),
        "roster": MemberCurrentState(app, conn),
        "collections": PlayerCurrentCollections(app, conn),
        "clan_daily_metrics": ClanDailyMetrics(app, conn),
        "war_current_state": WarCurrentStateProjection(app, conn),
        "war_participation": WarParticipationProjection(app, conn),
        "roster_lifecycle": RosterLifecycle(app, conn),
    }.items():
        proj.reset()
        applied[name] = proj.run()
    conn.close()

    return {
        "backfill": {
            "players": bf_players,
            "clans": bf_clans,
            "collections": bf_collections,
            "clan_state": bf_clan_state,
            "war_log": bf_war_log,
            "currentriverrace": bf_war_cur,
            "clan_roster": bf_clan_roster,
            "battles": bf_battles,
        },
        "projection_events_applied": applied,
        "parity": {
            "player_profile": check_player_profile_parity(),
            "member_current_state": check_member_current_state_parity(),
            "battle_telemetry": check_battle_telemetry_parity(),
            "collections": check_collections_parity(),
            "clan_daily_metrics": check_clan_daily_metrics_parity(),
            "war_current_state": check_war_current_state_parity(proj_db=config.PROJECTIONS_DB),
            "war_participation": check_war_participation_parity(proj_db=config.PROJECTIONS_DB),
        },
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, default=str))
