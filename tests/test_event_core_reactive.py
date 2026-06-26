"""Phase 3 reactive-layer tests: communication policy + agent read tools."""
from __future__ import annotations

import os
import tempfile

import pytest

from event_core import config


@pytest.fixture()
def world():
    d = tempfile.mkdtemp()
    config.configure_eventstore_env(os.path.join(d, "events.db"))
    from event_core.application import ObservedWorld

    return ObservedWorld()


def test_intent_lifecycle_invariant():
    from event_core.domain.communication_intent import CommunicationIntent, InvalidTransition

    ci = CommunicationIntent(
        dedup_key="i1", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={},
    )
    ci.drop("not_noteworthy")
    assert ci.status == "dropped"
    with pytest.raises(InvalidTransition):
        ci.fulfil()


def test_policy_emits_scoped_intents_idempotently(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.domain.recommendation import Recommendation
    from event_core.mind.communication import CommunicationPolicy

    world.save(Detection(
        dedup_key="career_wins_milestone:#A:1000", detection_type="career_wins_milestone",
        detector="t", subject_tag="#A", occurred_at="2026-06-21T00:00:00Z",
        caused_by=["e1"], payload={"milestone": 1000},
    ))
    world.save(Recommendation(
        dedup_key="kick:#B", recommendation_type="kick", player_tag="#B",
        reason_codes=["inactivity"], policy_version="v", severity="medium", caused_by=["e2"],
    ))

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 2  # one public (detection) + one leadership (recommendation)

    pub = world.repository.get(intent_id("intent:detection:career_wins_milestone:#A:1000"))
    assert pub.scope == "public" and pub.subject_tag == "#A"
    lead = world.repository.get(intent_id("intent:recommendation:kick:#B"))
    assert lead.scope == "leadership"

    pol2 = CommunicationPolicy(world, conn)
    pol2.reset()
    assert pol2.run() == 0  # idempotent
    conn.close()


def test_policy_coalesces_celebrate_per_player_per_tick(world):
    """A grinder who trips several celebrate detectors in one tick gets ONE
    #player-highlights post, not several overlapping ones; other players are
    unaffected. The lower-value same-tick candidate is still auditably dropped."""
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import (
        CommunicationPolicy,
        PLAYER_HIGHLIGHT_COALESCED_REASON,
    )

    for dedup, dtype, subj in [
        ("card_level_milestone:#A:1", "card_level_milestone", "#A"),
        ("battle_trophy_push:#A:1", "battle_trophy_push", "#A"),
        ("career_wins_milestone:#B:1000", "career_wins_milestone", "#B"),
    ]:
        world.save(Detection(
            dedup_key=dedup, detection_type=dtype, detector="t", subject_tag=subj,
            occurred_at="2026-06-22T00:00:00Z", caused_by=["e"],
            payload={"milestone": 1000} if dtype == "career_wins_milestone" else {},
        ))
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 2  # #A's two celebrate detections coalesce to one; #B one
    selected = world.repository.get(intent_id("intent:detection:card_level_milestone:#A:1"))
    dropped = world.repository.get(intent_id("intent:detection:battle_trophy_push:#A:1"))
    assert selected.status == "raised"
    assert dropped.status == "dropped"
    assert dropped.drop_reason == PLAYER_HIGHLIGHT_COALESCED_REASON
    conn.close()


def test_policy_accrues_under_threshold_player_highlight_evidence(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import (
        CommunicationPolicy,
        PLAYER_HIGHLIGHT_ACCRUING_REASON,
        PLAYER_HIGHLIGHT_THRESHOLD,
    )

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(Detection(
        dedup_key="battle_trophy_push:#A:1", detection_type="battle_trophy_push",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T00:00:00Z",
        caused_by=["e1"], payload={"trophy_delta": 120},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 0

    world.save(Detection(
        dedup_key="badge_earned:#A:1", detection_type="badge_earned",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T01:00:00Z",
        caused_by=["e2"], payload={"badge_name": "Tenacious"},
    ))
    assert CommunicationPolicy(world, conn).run() == 1

    first = world.repository.get(intent_id("intent:detection:battle_trophy_push:#A:1"))
    second = world.repository.get(intent_id("intent:detection:badge_earned:#A:1"))
    assert first.status == "dropped"
    assert first.drop_reason == PLAYER_HIGHLIGHT_ACCRUING_REASON
    assert first.summary["recognition_score"] == 25
    assert first.summary["recognition_threshold"] == PLAYER_HIGHLIGHT_THRESHOLD
    assert first.summary["recognition_evidence_count"] == 1
    assert second.status == "raised"
    assert second.summary["recognition_decision"] == "accrued"
    assert second.summary["recognition_score"] == PLAYER_HIGHLIGHT_THRESHOLD
    assert [e["detection_type"] for e in second.summary["recognition_evidence"]] == [
        "battle_trophy_push",
        "badge_earned",
    ]
    conn.close()


def test_policy_cross_type_evidence_accumulates(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import (
        CommunicationPolicy,
        PLAYER_HIGHLIGHT_ACCRUING_REASON,
    )

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(Detection(
        dedup_key="best_trophies_peak:#A:6000", detection_type="best_trophies_peak",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T00:00:00Z",
        caused_by=["e1"], payload={"peak": 6000},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 0

    world.save(Detection(
        dedup_key="path_of_legend_promotion:#A:10", detection_type="path_of_legend_promotion",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T06:00:00Z",
        caused_by=["e2"], payload={"league": 10},
    ))
    assert CommunicationPolicy(world, conn).run() == 1

    first = world.repository.get(intent_id("intent:detection:best_trophies_peak:#A:6000"))
    second = world.repository.get(intent_id("intent:detection:path_of_legend_promotion:#A:10"))
    assert first.status == "dropped"
    assert first.drop_reason == PLAYER_HIGHLIGHT_ACCRUING_REASON
    assert second.status == "raised"
    assert second.summary["recognition_decision"] == "accrued"
    assert second.summary["recognition_score"] == 85
    assert {e["detection_type"] for e in second.summary["recognition_evidence"]} == {
        "best_trophies_peak",
        "path_of_legend_promotion",
    }
    conn.close()


def test_policy_bypass_event_raises_after_recent_lower_value_highlight(world):
    from event_core import db
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import CommunicationPolicy

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(CommunicationIntent(
        dedup_key="intent:detection:badge_earned:#A:1",
        intent_type="celebrate:badge_earned",
        subject_tag="#A",
        scope="public",
        priority=1,
        caused_by=["e1"],
        summary={"detection_type": "badge_earned", "occurred_at": "2026-06-01T00:00:00Z"},
    ))
    world.save(Detection(
        dedup_key="card_level_milestone:#A:16", detection_type="card_level_milestone",
        detector="t", subject_tag="#A", occurred_at="2026-06-08T00:00:00Z",
        caused_by=["e2"], payload={"card_name": "Wizard", "milestone": 16},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 1

    later = world.repository.get(intent_id("intent:detection:card_level_milestone:#A:16"))
    assert later.status == "raised"
    assert later.summary["recognition_decision"] == "bypass"
    assert later.summary["recognition_score"] == 95
    conn.close()


def test_policy_durable_milestone_raises_by_itself(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import CommunicationPolicy

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(Detection(
        dedup_key="career_wins_milestone:#A:1000", detection_type="career_wins_milestone",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T00:00:00Z",
        caused_by=["e1"], payload={"milestone": 1000, "from": 998, "to": 1002},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 1

    intent = world.repository.get(intent_id("intent:detection:career_wins_milestone:#A:1000"))
    assert intent.status == "raised"
    assert intent.summary["recognition_decision"] == "bypass"
    assert intent.summary["recognition_score"] == 85
    assert intent.summary["recognition_evidence"][0]["dedup_key"] == "career_wins_milestone:#A:1000"
    conn.close()


def test_policy_allows_different_players_inside_highlight_window(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import CommunicationPolicy

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(Detection(
        dedup_key="battle_trophy_push:#A:1", detection_type="battle_trophy_push",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T00:00:00Z",
        caused_by=["e"], payload={"trophy_delta": 110},
    ))
    world.save(Detection(
        dedup_key="badge_earned:#A:1", detection_type="badge_earned",
        detector="t", subject_tag="#A", occurred_at="2026-06-22T00:10:00Z",
        caused_by=["e"], payload={"badge_name": "Tenacious"},
    ))
    world.save(Detection(
        dedup_key="battle_trophy_push:#B:1", detection_type="battle_trophy_push",
        detector="t", subject_tag="#B", occurred_at="2026-06-22T00:00:00Z",
        caused_by=["e"], payload={"trophy_delta": 110},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 1

    a = world.repository.get(intent_id("intent:detection:badge_earned:#A:1"))
    b = world.repository.get(intent_id("intent:detection:battle_trophy_push:#B:1"))
    assert a.status == "raised"
    assert a.summary["recognition_score"] == 80
    assert b.status == "dropped"
    assert b.summary["recognition_score"] == 25
    conn.close()


def test_policy_does_not_recount_evidence_before_latest_public_highlight(world):
    from event_core import db
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import PLAYER_HIGHLIGHT_ACCRUING_REASON, CommunicationPolicy

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    world.save(CommunicationIntent(
        dedup_key="intent:detection:career_wins_milestone:#A:1000",
        intent_type="celebrate:career_wins_milestone",
        subject_tag="#A",
        scope="public",
        priority=1,
        caused_by=["e0"],
        summary={
            "detection_type": "career_wins_milestone",
            "milestone": 1000,
            "occurred_at": "2026-06-10T00:00:00Z",
        },
    ))
    world.save(Detection(
        dedup_key="battle_trophy_push:#A:1", detection_type="battle_trophy_push",
        detector="t", subject_tag="#A", occurred_at="2026-06-09T00:00:00Z",
        caused_by=["e1"], payload={"trophy_delta": 300},
    ))
    world.save(Detection(
        dedup_key="badge_earned:#A:1", detection_type="badge_earned",
        detector="t", subject_tag="#A", occurred_at="2026-06-11T00:00:00Z",
        caused_by=["e2"], payload={"badge_name": "Tenacious"},
    ))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 0

    current = world.repository.get(intent_id("intent:detection:badge_earned:#A:1"))
    assert current.status == "dropped"
    assert current.drop_reason == PLAYER_HIGHLIGHT_ACCRUING_REASON
    assert current.summary["recognition_score"] == 55
    assert [e["dedup_key"] for e in current.summary["recognition_evidence"]] == [
        "badge_earned:#A:1",
    ]
    conn.close()


def test_policy_maps_restored_coverage_detection_types(world):
    """v5 restored-coverage detections get the right intent_type prefix (which
    route_intent uses to pick the channel); non-public detections are filtered."""
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import CommunicationPolicy

    cases = {
        "member_joined:#J:t0": ("member_joined", "clan"),
        "member_left:#J:t0": ("member_left", "clan"),
        "member_promoted:#J:elder:t0": ("member_promoted", "clan"),
        "war_update:#CLAN:5:warDay": ("war_update", "war"),
        "war_complete:131:2": ("war_complete", "war"),
        "new_season:131": ("new_season", "war"),
        "cohort_wave:badge_earned:2026-06-21": ("cohort_wave", "cohort"),
        "wins:#J:1000": ("career_wins_milestone", "celebrate"),
        "collection:#J:1700": ("collection_level_milestone", "celebrate"),
        "pol_promo:#J:10": ("path_of_legend_promotion", "celebrate"),
        "uc:#J": ("ultimate_champion_reached", "celebrate"),
        "polrank:#J:1": ("path_of_legend_global_rank_attained", "celebrate"),
        "rankedpulse:#J:20260625": ("ranked_activity_pulse", "celebrate"),
        "clanbday:2026-06-21": ("clan_birthday", "clan"),
        "bday:#J:2026-06-21": ("member_birthday", "clan"),
        "anniv:#J:2026-06-21": ("join_anniversary", "clan"),
        "wkdon:2026W25": ("weekly_donation_leader", "clan"),
    }
    # Unique subject per case so per-player celebrate coalescing doesn't collapse
    # the celebrate detections — this test checks type->prefix mapping only.
    for i, (dedup, (dtype, _prefix)) in enumerate(cases.items()):
        world.save(Detection(
            dedup_key=dedup, detection_type=dtype, detector="t", subject_tag=f"#S{i}",
            occurred_at="2026-06-21T00:00:00Z", caused_by=["e"], payload={},
        ))
    # Detections that should NOT post:
    #  - inactive_member_risk drives recommendations, not Discord
    #  - new_champion_unlocked is a subset of new_card_unlocked (which DOES post);
    #    posting both double-posts every champion unlock
    world.save(Detection(
        dedup_key="inactive_member_risk:#Z", detection_type="inactive_member_risk",
        detector="t", subject_tag="#Z", occurred_at="2026-06-21T00:00:00Z",
        caused_by=["e"], payload={},
    ))
    world.save(Detection(
        dedup_key="new_champion_unlocked:#J:26000072", detection_type="new_champion_unlocked",
        detector="t", subject_tag="#J", occurred_at="2026-06-21T00:00:00Z",
        caused_by=["e"], payload={"rarity": "champion"},
    ))

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    expected_raised = sum(
        1
        for _dedup, (dtype, prefix) in cases.items()
        if prefix != "celebrate"
        or dtype in {
            "career_wins_milestone",
            "collection_level_milestone",
            "ultimate_champion_reached",
            "path_of_legend_global_rank_attained",
        }
    )
    assert pol.run() == expected_raised

    for dedup, (_dtype, prefix) in cases.items():
        intent = world.repository.get(intent_id(f"intent:detection:{dedup}"))
        assert intent.intent_type.split(":", 1)[0] == prefix
    conn.close()


def test_intent_context_includes_subject_history():
    """The compose prompt carries the subject player's recent detection stream
    (newest first), excludes the triggering detection, and scope-gates leadership
    rows out of a public post — the holistic-commentary enrichment."""
    import json

    from event_core import db
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.runtime import intent_context

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.execute(
        "CREATE TABLE detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, detector TEXT, "
        "subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO detections VALUES(?,?,?,?,?,?,?)",
        [
            # the triggering detection (must NOT reappear as history)
            ("best_trophies_peak:#A:6000", "best_trophies_peak", "t", "#A",
             "2026-06-21T03:00:00Z", "public", json.dumps({"peak": 6000})),
            ("card_level_milestone:#A:14", "card_level_milestone", "t", "#A",
             "2026-06-20T03:00:00Z", "public", json.dumps({"card": "Mega Knight", "level": 14})),
            ("player_level_up:#A:55", "player_level_up", "t", "#A",
             "2026-06-19T03:00:00Z", "public", json.dumps({"level": 55})),
            # retired/non-postable type: present in the projection but must NOT
            # pollute a public post's holistic context
            ("battle_hot_streak:#A:1", "battle_hot_streak", "t", "#A",
             "2026-06-18T12:00:00Z", "public", "{}"),
            # leadership-scoped: a PUBLIC post must not see this
            ("inactive_member_risk:#A", "inactive_member_risk", "t", "#A",
             "2026-06-18T03:00:00Z", "leadership", "{}"),
        ],
    )
    conn.commit()

    intent = CommunicationIntent(
        dedup_key="intent:detection:best_trophies_peak:#A:6000",
        intent_type="celebrate:best_trophies_peak", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={"detection_type": "best_trophies_peak", "peak": 6000},
    )
    prompt = intent_context(intent, conn)
    assert "recent_history" in prompt
    # parse the embedded JSON block to assert precisely
    block = prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    payload = json.loads(block)
    hist = payload["recent_history"]
    types = [h["type"] for h in hist]
    assert "best_trophies_peak" not in types  # triggering detection excluded
    assert "battle_hot_streak" not in types  # retired/non-postable type filtered out
    assert types == ["card_level_milestone", "player_level_up"]  # newest first, public only
    assert hist[0]["facts"]["card"] == "Mega Knight"  # payload carried through
    conn.close()


def test_looks_like_meta_rejects_agent_diagnostics():
    """compose_copy must not post the agent's meta/refusal notes; the guard catches
    the phrasings seen live without flagging real celebratory copy."""
    from event_core.live.runtime import _looks_like_meta

    assert _looks_like_meta("Signal data inconsistent with player profile; skipping post")
    assert _looks_like_meta("Signal is from Week 3 Battle Day 4; messaging would be stale.")
    assert not _looks_like_meta("**King Thing** just hit 8,200 trophies — clean ladder work!")
    assert not _looks_like_meta("We're holding rank 1 with 9,800 fame. Use your last decks!")


def test_intent_context_resolves_player_name_and_names_member():
    """card_level_milestone etc. must name the MEMBER, not lead with the card.
    intent_context resolves subject_tag -> member name and the prompt directs the
    agent to lead with the person (the 'Wizard just hit level 16' fix)."""
    from event_core import db
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.runtime import intent_context

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.execute("CREATE TABLE members (member_id INTEGER, player_tag TEXT, current_name TEXT, status TEXT)")
    conn.execute("INSERT INTO members VALUES (1, '#PR8YLQ2CV', 'The Joesma', 'active')")
    conn.execute(
        "CREATE TABLE detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, detector TEXT, "
        "subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
    )
    conn.commit()
    intent = CommunicationIntent(
        dedup_key="intent:detection:card_level_milestone:#PR8YLQ2CV:26000017",
        intent_type="celebrate:card_level_milestone", subject_tag="#PR8YLQ2CV", scope="public",
        priority=1, caused_by=["e"],
        summary={"detection_type": "card_level_milestone", "card_name": "Wizard", "milestone": 16},
    )
    prompt = intent_context(intent, conn)
    assert "The Joesma" in prompt and '"player_name": "The Joesma"' in prompt
    assert "never make a card the subject" in prompt
    conn.close()


def test_intent_context_no_history_is_clean():
    """A first-seen player (no prior detections) gets the original single-event
    prompt with no recent_history key."""

    from event_core import db
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.runtime import intent_context

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.execute(
        "CREATE TABLE detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, detector TEXT, "
        "subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
    )
    conn.commit()
    intent = CommunicationIntent(
        dedup_key="intent:detection:player_level_up:#NEW:10",
        intent_type="celebrate:player_level_up", subject_tag="#NEW", scope="public",
        priority=1, caused_by=["e"], summary={"detection_type": "player_level_up"},
    )
    prompt = intent_context(intent, conn)
    assert "recent_history" not in prompt
    conn.close()


def test_agent_tools_resolve_evidence_and_scope():
    from event_core import db
    from event_core.read import tools

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.execute(
        "CREATE TABLE battle_telemetry (player_tag TEXT, battle_time TEXT, battle_type TEXT, "
        "mode_group TEXT, outcome TEXT, crowns_for INT, crowns_against INT, opponent_tag TEXT, "
        "trophy_change INT, is_competitive INT)"
    )
    conn.executemany(
        "INSERT INTO battle_telemetry VALUES(?,?,?,?,?,?,?,?,?,1)",
        [
            ("#A", "20260621T100000.000Z", "PvP", "ladder", "W", 3, 1, "#OPP1", 30),
            ("#A", "20260621T110000.000Z", "PvP", "ladder", "W", 2, 0, "#OPP2", 30),
        ],
    )
    conn.execute(
        "CREATE TABLE detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, detector TEXT, "
        "subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO detections VALUES(?,?,?,?,?,?,?)",
        [
            ("d1", "battle_hot_streak", "x", "#A", "20260621T120000.000Z", "public", "{}"),
            ("d2", "inactive_member_risk", "x", "#A", "20260621T120000.000Z", "leadership", "{}"),
        ],
    )
    conn.commit()

    ev = tools.resolve_evidence(conn, {"subject_tag": "#A", "occurred_at": "20260621T120000.000Z"})
    assert len(ev) == 2 and ev[0]["opponent_tag"] in ("#OPP1", "#OPP2")

    # scope gating: public caller does not see the leadership detection
    assert len(tools.get_player_detections(conn, "#A", scope="public")) == 1
    assert len(tools.get_player_detections(conn, "#A", scope="leadership")) == 2
    conn.close()
