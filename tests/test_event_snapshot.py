"""Current event membership survives rollover, failures and payload retention."""

import pytest

import db
from db.schema import CURRENT_SCHEMA_FINGERPRINT, apply_schema_migrations, schema_fingerprint
from storage import game_mode_contexts as contexts


def _tags(conn, *, current=True):
    return {
        r["event_tag"]
        for r in contexts.list_game_mode_contexts("event", conn=conn, current_only=current)
    }


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        {"items": None},
        [None],
        [{"eventTag": "#NEW"}, {"title": 1}],
        [{"description": "no identity"}],
    ],
)
def test_invalid_snapshot_preserves_current_membership(engine_conn, invalid):
    contexts.upsert_game_mode_contexts_from_events(
        [{"eventTag": "#OLD", "title": "Old"}], conn=engine_conn
    )
    contexts.upsert_game_mode_contexts_from_events(invalid, conn=engine_conn)
    assert _tags(engine_conn) == {"#OLD"}
    assert _tags(engine_conn, current=False) == {"#OLD"}


def test_rollover_and_empty_snapshot_keep_historical_labels(engine_conn):
    contexts.upsert_game_mode_contexts_from_events(
        [{"eventTag": "#OLD", "title": "Retired"}], conn=engine_conn
    )
    current = {"items": [{"eventTag": "#NEW", "title": "Current"}]}
    contexts.upsert_game_mode_contexts_from_events(current, conn=engine_conn)
    contexts.upsert_game_mode_contexts_from_events(current, conn=engine_conn)
    assert _tags(engine_conn) == {"#NEW"}
    contexts.upsert_game_mode_contexts_from_events([], conn=engine_conn)
    assert _tags(engine_conn) == set()
    assert _tags(engine_conn, current=False) == {"#OLD", "#NEW"}
    assert (
        engine_conn.execute(
            "SELECT display_name FROM game_mode_contexts WHERE event_tag='#OLD'"
        ).fetchone()[0]
        == "Retired"
    )


def test_failed_projection_rolls_back_entire_snapshot(engine_conn, monkeypatch):
    contexts.upsert_game_mode_contexts_from_events([{"eventTag": "#OLD"}], conn=engine_conn)
    real_upsert = contexts._upsert_context

    def fail_second(conn, **kwargs):
        if kwargs["event_tag"] == "#FAIL":
            raise RuntimeError("simulated persistence failure")
        real_upsert(conn, **kwargs)

    monkeypatch.setattr(contexts, "_upsert_context", fail_second)
    with pytest.raises(RuntimeError, match="simulated persistence"):
        contexts.upsert_game_mode_contexts_from_events(
            [{"eventTag": "#NEW"}, {"eventTag": "#FAIL"}], conn=engine_conn
        )
    assert _tags(engine_conn) == {"#OLD"}
    assert _tags(engine_conn, current=False) == {"#OLD"}


@pytest.mark.parametrize(
    "last_payload, expected",
    [
        ([{"eventTag": "#A", "title": "A"}], {"#A"}),
        ([], set()),
        ({"items": None}, {"#B"}),
    ],
)
def test_v40_seeds_latest_receipt_not_latest_unique_content(engine_conn, last_payload, expected):
    for payload in (
        [{"eventTag": "#A", "title": "A"}],
        [{"eventTag": "#B", "title": "B"}],
        last_payload,
    ):
        db._store_raw_payload(engine_conn, "events", "global", payload)
        contexts.upsert_game_mode_contexts_from_events(payload, conn=engine_conn)
    engine_conn.execute("ALTER TABLE game_mode_contexts DROP COLUMN is_current")
    engine_conn.execute("PRAGMA user_version=39")
    engine_conn.commit()
    apply_schema_migrations(engine_conn)
    assert _tags(engine_conn) == expected
    assert _tags(engine_conn, current=False) == {"#A", "#B"}
    assert schema_fingerprint(engine_conn) == CURRENT_SCHEMA_FINGERPRINT
    # The projection, including a valid empty snapshot, outlives the raw buffer.
    engine_conn.execute("DELETE FROM api_observation_receipts")
    engine_conn.execute("DELETE FROM raw_api_payloads")
    apply_schema_migrations(engine_conn)
    assert _tags(engine_conn) == expected


def test_v40_missing_latest_payload_does_not_revive_older_membership(engine_conn):
    for tag in ("#OLD", "#LATEST"):
        payload = [{"eventTag": tag}]
        db._store_raw_payload(engine_conn, "events", "global", payload)
        contexts.upsert_game_mode_contexts_from_events(payload, conn=engine_conn)
    engine_conn.execute(
        "UPDATE api_observation_receipts SET payload_id=NULL WHERE receipt_id=(SELECT MAX(receipt_id) FROM api_observation_receipts)"
    )
    engine_conn.execute("ALTER TABLE game_mode_contexts DROP COLUMN is_current")
    engine_conn.execute("PRAGMA user_version=39")
    engine_conn.commit()
    apply_schema_migrations(engine_conn)
    assert _tags(engine_conn) == set()
