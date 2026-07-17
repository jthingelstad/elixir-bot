"""Context-builder truth tests (audit 2026-07-04): the numbers Elixir's chat
contexts assert must survive independent recomputation. Regressions covered:
departed riverrace participants counted as "haven't started"; roster-total
trophy deltas narrated as trophies pushed; war-scoped memory filters silently
ignored by the rebuilt store."""

import json

import db
from memory_store import create_memory, list_memories
from storage import war_status
from storage.trends import build_clan_trend_summary_context

HOME = "#J2RGCRVG"


def _seed_race_state(conn, participants):
    conn.execute(
        """INSERT INTO state_baselines (entity_kind, entity_tag, aspect,
               payload_json, payload_hash, observed_at)
           VALUES ('riverrace', ?, 'race', ?, 'h-race', '2026-07-04T12:00:00Z')""",
        (
            HOME,
            json.dumps(
                {
                    "season_id": 133,
                    "section_index": 4,
                    "period_index": 33,
                    "period_type": "colosseum",
                    "our_tag": HOME,
                    "our_fame": 27850,
                    "clans": {HOME: {"name": "POAP KINGS", "fame": 27850}},
                    "participants": participants,
                }
            ),
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES (?, 'POAP KINGS', '2026-02-04', '2026-07-04', 1)",
        (HOME,),
    )


def _seed_member(conn, tag, name, *, departed=False):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES (?, 'POAP KINGS', '2026-02-04', '2026-07-04', 1)",
        (HOME,),
    )
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '2026-06-01T00:00:00Z', '2026-07-04T00:00:00Z')",
        (tag, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, left_at, join_source) "
        "VALUES (?, ?, '2026-06-01T00:00:00Z', ?, 'test')",
        (tag, HOME, "2026-07-01T00:00:00Z" if departed else None),
    )


def test_war_day_state_excludes_departed_from_deck_buckets():
    conn = db.get_connection()
    try:
        _seed_member(conn, "#AAA", "Stayer")
        _seed_member(conn, "#BBB", "AlsoHere")
        _seed_member(conn, "#GONE", "LeftMidweek", departed=True)
        _seed_race_state(
            conn,
            {
                "#AAA": {
                    "name": "Stayer",
                    "fame": 900,
                    "decks_used": 8,
                    "decks_used_today": 0,
                },
                "#BBB": {
                    "name": "AlsoHere",
                    "fame": 1200,
                    "decks_used": 12,
                    "decks_used_today": 4,
                },
                "#GONE": {
                    "name": "LeftMidweek",
                    "fame": 400,
                    "decks_used": 4,
                    "decks_used_today": 0,
                },
            },
        )
        conn.commit()
    finally:
        conn.close()

    state = war_status.get_current_war_day_state()
    assert state is not None
    # Departed participant is visible but never counted as "hasn't started".
    assert state["departed_participant_count"] == 1
    assert state["all_participant_count"] == 3
    assert state["total_participants"] == 2
    assert state["untouched_count"] == 1  # Stayer only — not LeftMidweek
    untouched_tags = {p["tag"] for p in state["used_none"]}
    assert "#GONE" not in untouched_tags
    # Fame lists still honor the departed player's real contribution.
    all_tags = {p["tag"] for p in state["participants"]}
    assert "#GONE" in all_tags


def test_clan_trend_context_labels_roster_delta_and_battle_delta():
    conn = db.get_connection()
    try:
        # A joiner mid-window inflates the roster total by their whole count;
        # the context must label that delta as roster movement, not pushing.
        for _i, (date, members, total) in enumerate(
            [
                ("2026-06-28", 43, 450000),
                ("2026-06-30", 44, 462000),
                ("2026-07-02", 46, 488000),
                ("2026-07-04", 47, 513000),
            ]
        ):
            conn.execute(
                """INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name,
                       member_count, total_member_trophies, clan_score, observed_at)
                   VALUES (?, ?, 'POAP KINGS', ?, ?, 60000, ?)""",
                (date, HOME, members, total, f"{date}T23:00:00Z"),
            )
        conn.execute(
            """INSERT INTO clan_daily_battle_rollups (battle_date, clan_tag, clan_name,
                   mode_group, battles, wins, losses, draws, trophy_change_total, last_aggregated_at)
               VALUES ('2026-07-03', ?, 'POAP KINGS', 'ladder', 40, 22, 18, 0, 180, '2026-07-04T00:00:00Z')""",
            (HOME,),
        )
        conn.commit()
    finally:
        conn.close()

    text = build_clan_trend_summary_context(days=30, window_days=7)
    assert "roster_total_trophies_change" in text
    assert "NOT trophies pushed" in text
    assert "battle_trophy_delta" in text
    assert "total_member_trophies 6" not in text  # old unlabeled framing gone


def test_memory_war_filters_actually_filter():
    mem_id = create_memory(
        title="Week recap 133:4",
        body="Colosseum week four recap for testing.",
        source_type="system",
        scope="public",
        is_inference=False,
        confidence=0.95,
        war_season_id="133",
        war_week_id="133:4",
        created_by="test",
    )
    mem_id = mem_id["memory_id"]  # create_memory returns the full row dict
    assert mem_id
    hit = list_memories(viewer_scope="public", filters={"war_week_id": "133:4"}, limit=10)
    miss = list_memories(viewer_scope="public", filters={"war_week_id": "999:9"}, limit=10)
    season_hit = list_memories(viewer_scope="public", filters={"war_season_id": "133"}, limit=10)
    assert any(m["memory_id"] == mem_id for m in hit)
    assert not miss
    assert any(m["memory_id"] == mem_id for m in season_hit)
