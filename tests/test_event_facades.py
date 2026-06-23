"""Tests for the v5-native game_event_stream read facades (event_core.read.event_facades)."""

from event_core import config
from event_core import db as ec_db
from event_core.ingest.battles import BATTLE_TELEMETRY_DDL
from event_core.read import event_facades

NOW = "20260623T120000.000Z"

_DETECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS detections (
    dedup_key      TEXT PRIMARY KEY,
    detection_type TEXT,
    detector       TEXT,
    subject_tag    TEXT,
    occurred_at    TEXT,
    scope          TEXT,
    payload_json   TEXT
)
"""

_MEMBERS_DDL = "CREATE TABLE IF NOT EXISTS members (player_tag TEXT UNIQUE, current_name TEXT)"


def _seed(detections=(), battles=(), profiles=()):
    conn = ec_db.connect(config.PROJECTIONS_DB)
    try:
        conn.execute(_DETECTIONS_DDL)
        conn.execute(BATTLE_TELEMETRY_DDL)
        conn.execute(_MEMBERS_DDL)
        for d in detections:
            conn.execute(
                "INSERT INTO detections(dedup_key,detection_type,detector,subject_tag,occurred_at,scope,payload_json) "
                "VALUES(?,?,?,?,?,?,?)",
                d,
            )
        for b in battles:
            # (player_tag, battle_time, battle_type, opponent_tag, crowns_for, crowns_against, mode_group, outcome)
            conn.execute(
                "INSERT INTO battle_telemetry(player_tag,battle_time,battle_type,opponent_tag,"
                "crowns_for,crowns_against,mode_group,outcome,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                b,
            )
        for p in profiles:
            conn.execute("INSERT INTO members(player_tag,current_name) VALUES(?,?)", p)
        conn.commit()
    finally:
        conn.close()


def test_summarize_event_windows_counts_by_type_and_scope():
    _seed(detections=[
        ("d1", "battle_trophy_push", "push", "#AAA", "20260620T120000.000Z", "public", "{}"),
        ("d2", "battle_trophy_push", "push", "#BBB", "20260619T120000.000Z", "public", "{}"),
        ("d3", "inactive_members", "scan", None, "20260618T120000.000Z", "leadership", "{}"),
        # 30 days back — outside the 7d/28d windows, inside 56d/90d
        ("d4", "war_update", "war", None, "20260524T120000.000Z", "public", "{}"),
    ])
    out = event_facades.summarize_event_windows(now=NOW)

    assert out["7d"]["total"] == 3
    assert out["7d"]["by_type"]["battle_trophy_push"] == 2
    assert out["7d"]["by_type"]["inactive_members"] == 1
    assert out["7d"]["by_scope"] == {"public": 2, "leadership": 1}
    assert out["28d"]["total"] == 3  # d4 still outside 28d
    assert out["56d"]["total"] == 4  # d4 now included


def test_summarize_event_windows_scope_and_subject_filters():
    _seed(detections=[
        ("d1", "battle_trophy_push", "push", "#AAA", "20260620T120000.000Z", "public", "{}"),
        ("d2", "inactive_members", "scan", None, "20260620T120000.000Z", "leadership", "{}"),
    ])
    public = event_facades.summarize_event_windows(now=NOW, scope="public")
    assert public["7d"]["total"] == 1
    assert public["7d"]["by_scope"] == {"public": 1}

    by_subject = event_facades.summarize_event_windows(now=NOW, subject_key="aaa")
    assert by_subject["7d"]["total"] == 1  # subject_tag canonicalized to #AAA


def test_list_recent_events_shape_and_order():
    _seed(detections=[
        ("d1", "battle_trophy_push", "push", "#AAA", "20260620T120000.000Z", "public", '{"k": 1}'),
        ("d2", "war_update", "war", None, "20260622T120000.000Z", "public", "{}"),
    ])
    events = event_facades.list_recent_events(now=NOW, days=7, limit=10)

    assert [e["event_type"] for e in events] == ["war_update", "battle_trophy_push"]  # newest first
    first = events[0]
    assert first["event_key"] == "d2"
    assert first["occurred_at"] == "20260622T120000.000Z"
    assert events[1]["payload_json"] == {"k": 1}  # parsed dict
    assert events[1]["subject_type"] == "member" and events[1]["subject_key"] == "#AAA"


def test_list_recent_events_filters_and_limit():
    _seed(detections=[
        ("d1", "battle_trophy_push", "push", "#AAA", "20260620T120000.000Z", "public", "{}"),
        ("d2", "battle_trophy_push", "push", "#BBB", "20260619T120000.000Z", "public", "{}"),
        ("d3", "inactive_members", "scan", None, "20260618T120000.000Z", "leadership", "{}"),
    ])
    assert len(event_facades.list_recent_events(now=NOW, days=7, scope="public")) == 2
    assert len(event_facades.list_recent_events(now=NOW, days=7, event_type="inactive_members")) == 1
    assert len(event_facades.list_recent_events(now=NOW, days=7, limit=1)) == 1


def test_summarize_battle_modes_pulse_with_names():
    _seed(
        battles=[
            ("#AAA", "20260622T120000.000Z", "PvP", "#X", 3, 1, "ladder", "W", NOW),
            ("#AAA", "20260621T120000.000Z", "PvP", "#Y", 0, 2, "ladder", "L", NOW),
            ("#AAA", "20260620T120000.000Z", "PvP", "#Z", 2, 1, "ladder", "W", NOW),
            ("#BBB", "20260622T130000.000Z", "PvP", "#Q", 1, 0, "ladder", "W", NOW),
            # too few in two_v_two to clear min_battles=3
            ("#BBB", "20260622T140000.000Z", "2v2", "#R", 1, 0, "two_v_two", "W", NOW),
        ],
        profiles=[("#AAA", "Alice"), ("#BBB", "Bob")],
    )
    out = event_facades.summarize_battle_modes(now=NOW, windows=(7,), min_battles=3)

    modes = out["7d"]["modes"]
    assert "ladder" in modes
    assert "two_v_two" not in modes  # below min_battles
    ladder = modes["ladder"]
    assert ladder["battles"] == 4
    assert ladder["wins"] == 3 and ladder["losses"] == 1
    assert ladder["win_rate"] == 0.75
    names = {m["name"] for m in ladder["top_members"]}
    assert names == {"Alice", "Bob"}  # names sourced from player_current_profile


def test_facades_return_empty_when_tables_absent():
    # No seeding: the projection tables do not exist yet.
    assert event_facades.summarize_event_windows(now=NOW)["7d"]["total"] == 0
    assert event_facades.list_recent_events(now=NOW) == []
    assert event_facades.summarize_battle_modes(now=NOW)["7d"]["modes"] == {}
