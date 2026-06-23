"""Live tick engine — apply payloads + advance Followers incrementally.

`apply_payloads` routes fetched CR payloads through the SAME ingest functions
backfill uses (one code path). `advance` runs every projection + detector +
leadership generator + communication policy from their tracked positions — so a
live tick processes only the new events, not a full rebuild.
"""
from __future__ import annotations


def apply_payloads(app, conn, payloads: dict, observed_at: str) -> dict:
    """Ingest one tick's fetched payloads. `payloads` keys (all optional):
    player_profiles: [profile dict], clan: clan dict, battlelogs: {tag: [battle]},
    currentriverrace: war dict."""
    from event_core.domain.player import canon_tag
    from event_core.ingest.battles import write_battle_telemetry
    from event_core.ingest.clan import ingest_clan_state
    from event_core.ingest.collections import ingest_player_collections
    from event_core.ingest.profile import ingest_player_payload
    from event_core.ingest.roster import ingest_clan_payload

    out = {k: 0 for k in ("profiles", "collections", "roster", "clan_state", "roster_lifecycle", "battles")}

    for profile in payloads.get("player_profiles", []):
        if ingest_player_payload(app, profile, observed_at):
            out["profiles"] += 1
        changed = ingest_player_collections(app, profile, observed_at)
        out["collections"] += sum(1 for v in changed.values() if v)

    clan = payloads.get("clan")
    if clan:
        clan_tag = clan.get("tag")
        out["roster"] += ingest_clan_payload(app, clan, observed_at)
        if ingest_clan_state(app, clan, observed_at, clan_tag):
            out["clan_state"] += 1
        roster = {
            canon_tag(m["tag"]): (m.get("role") or "member")
            for m in (clan.get("memberList") or [])
            if m.get("tag")
        }
        out["roster_lifecycle"] += app.observe_clan_roster(clan_tag, roster, observed_at)

    for tag, battle_log in payloads.get("battlelogs", {}).items():
        out["battles"] += write_battle_telemetry(conn, tag, battle_log, observed_at)

    rr = payloads.get("currentriverrace")
    if rr and clan:
        from event_core.ingest.war import ingest_currentriverrace_payload

        ingest_currentriverrace_payload(app, clan.get("tag"), rr, observed_at)

    return out


def advance(app, conn) -> dict:
    """Process new events incrementally: detectors (emit Detections), then
    leadership (Detection -> Rec/Case), then the communication policy
    (Detection/Rec -> Intent), then the detections read model. Every step resumes
    from its tracked position; none reset.

    The Observed-World current-state projections (player_current_profile,
    member_current_state_proj, player_current_collections, clan_daily_metrics_proj,
    war_current_state_proj, war_participation_proj, roster_lifecycle) were RETIRED
    from the live tick on 2026-06-21: they had no live readers and only duplicated
    the v4 operational tables, costing a per-tick dual-write
    (see docs/archive/event-core-v5/event-core-v5-architecture-boundary.md). The Mind consumes the
    event store + battle_telemetry + the detections projection directly. The
    projections + their parity checks still live in build_foundation as an offline
    validation harness."""
    from event_core.mind.communication import CommunicationPolicy
    from event_core.mind.detectors import ALL_DETECTORS, CohortWaveDetector
    from event_core.mind.leadership import InactivityRiskDetector, LeadershipGenerator
    from event_core.projections.detections import DetectionsProjection
    from event_core.ingest.battles import BATTLE_TELEMETRY_DDL

    # Battle detectors scan battle_telemetry; ensure it exists even on a tick with
    # no battlelogs (a fresh store).
    conn.execute(BATTLE_TELEMETRY_DDL)
    conn.commit()
    result = {}

    detected = 0
    for cls in (*ALL_DETECTORS, InactivityRiskDetector):
        detected += cls(app, conn).run()

    # Project per-event detections so the cohort detector can scan them, then run
    # cohort (clan-wide waves), then re-project so cohort_wave is queryable.
    dp = DetectionsProjection(app, conn)
    dp.setup()
    dp.run()
    detected += CohortWaveDetector(app, conn).run()
    result["detections"] = detected

    result["leadership"] = LeadershipGenerator(app, conn).run()
    result["intents"] = CommunicationPolicy(app, conn).run()

    result["detections_projected"] = DetectionsProjection(app, conn).run()
    return result
