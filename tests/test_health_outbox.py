"""Health coverage for the durable awareness delivery boundary."""

from runtime.health import check_awareness_outbox


def test_stale_awareness_outbox_states_are_reported(engine_conn):
    engine_conn.execute(
        """INSERT INTO awareness_delivery_intents
               (intent_key, lane, content, covers_json, post_json, status,
                created_at, updated_at)
           VALUES ('sending', 'elixir', 'one', '[]', '{}', 'sending',
                   datetime('now', '-1 hour'), datetime('now', '-1 hour')),
                  ('pending', 'elixir', 'two', '[]', '{}', 'pending',
                   datetime('now', '-3 hours'), datetime('now', '-3 hours'))"""
    )

    problems = check_awareness_outbox(engine_conn)
    assert len(problems) == 2
    assert "stuck sending" in problems[0]
    assert "pending" in problems[1]


def test_recent_pending_intent_is_not_reported(engine_conn):
    engine_conn.execute(
        """INSERT INTO awareness_delivery_intents
               (intent_key, lane, content, covers_json, post_json, status,
                created_at, updated_at)
           VALUES ('fresh', 'elixir', 'one', '[]', '{}', 'pending',
                   datetime('now'), datetime('now'))"""
    )

    assert check_awareness_outbox(engine_conn) == []
