import json
from datetime import datetime, timedelta, timezone

from engine.editor import record_copy_edit_pair
from runtime.awareness.policy import apply_editorial_admission
from runtime.awareness.read import _editorial_guidance
from runtime.awareness.store import record_awareness_post
from scripts.eval_post_quality import _repetition_findings, _sample


def _now(offset_hours=0):
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_active_sampler_reads_awareness_and_assistant_messages(engine_conn):
    plan = {
        "posts": [{
            "channel": "elixir",
            "leads_with": "war",
            "content": "Colosseum has no finish line; every battle counts.",
            "covers_signal_keys": ["war:1"],
        }]
    }
    read = {"time": {"is_colosseum_week": True}}
    engine_conn.execute(
        "INSERT INTO awareness_thoughts "
        "(thought_id, loop_number, at, read_json, plan_json, chose_silence, post_count) "
        "VALUES ('t1', 1, ?, ?, ?, 0, 1)",
        (_now(-1), json.dumps(read), json.dumps(plan)),
    )
    engine_conn.execute(
        "INSERT INTO awareness_posts "
        "(lane, content_preview, covers_json, loop_number, posted_at, discord_message_id) "
        "VALUES ('elixir', ?, '[\"war:1\"]', 1, ?, '101')",
        (plan["posts"][0]["content"], _now(-1)),
    )
    engine_conn.execute(
        "INSERT INTO conversation_threads "
        "(scope_type, scope_key, created_at, last_active_at) "
        "VALUES ('channel', 'ask', ?, ?)",
        (_now(-1), _now(-1)),
    )
    thread_id = engine_conn.execute(
        "SELECT thread_id FROM conversation_threads WHERE scope_key = 'ask'"
    ).fetchone()[0]
    engine_conn.execute(
        "INSERT INTO messages "
        "(discord_message_id, thread_id, author_type, workflow, event_type, content, created_at) "
        "VALUES ('202', ?, 'assistant', 'interactive', 'channel_response', 'A grounded answer.', ?)",
        (thread_id, _now(-1)),
    )

    posts = _sample(engine_conn, days=1, lane=None)

    assert {post["source"] for post in posts} == {"awareness", "messages"}
    awareness = next(post for post in posts if post["source"] == "awareness")
    assert awareness["facts"]["is_colosseum_week"] is True
    assert awareness["message_id"] == "101"


def test_active_sampler_recovers_pre_link_awareness_evidence(engine_conn):
    post = {
        "channel": "elixir",
        "leads_with": "war",
        "content": "Colosseum has no finish line; every battle counts.",
        "covers_signal_keys": ["war:old"],
    }
    engine_conn.execute(
        "INSERT INTO awareness_posts "
        "(lane, content_preview, covers_json, posted_at, discord_message_id) "
        "VALUES ('elixir', ?, '[\"war:old\"]', ?, 'old-101')",
        (post["content"], _now(-1)),
    )
    engine_conn.execute(
        "INSERT INTO awareness_thoughts "
        "(thought_id, loop_number, at, read_json, plan_json, chose_silence, post_count) "
        "VALUES ('old-thought', 9, ?, ?, ?, 0, 1)",
        (
            _now(-1),
            json.dumps({"time": {"is_colosseum_week": True}}),
            json.dumps({"posts": [post]}),
        ),
    )

    sampled = _sample(engine_conn, days=1, lane="elixir")
    recovered = next(item for item in sampled if item["message_id"] == "old-101")

    assert recovered["intent_type"] == "awareness:war"
    assert recovered["facts"]["is_colosseum_week"] is True


def test_active_repetition_writes_editorial_memory(engine_conn):
    prior = {
        "channel": "elixir",
        "leads_with": "milestone",
        "summary": "Gem trophy peak",
        "member_tags": ["#GEM"],
        "member_names": ["Gem"],
        "content": "Gem reached another trophy peak.",
    }
    engine_conn.execute(
        "INSERT INTO awareness_thoughts "
        "(thought_id, loop_number, at, plan_json, chose_silence, post_count) "
        "VALUES ('prior', 1, ?, ?, 0, 1)",
        (_now(-1), json.dumps({"posts": [prior]})),
    )
    current = {**prior, "summary": "Gem leveled a card", "content": "Gem leveled another card."}

    record_awareness_post(
        lane="elixir",
        content=current["content"],
        message_id="303",
        post=current,
        evidence={"notable_moment": False},
        conn=engine_conn,
    )

    memory = engine_conn.execute(
        "SELECT title, body, created_by FROM memories "
        "WHERE source_event_key = 'active_post_quality:303'"
    ).fetchone()
    assert memory is not None
    assert memory["created_by"] == "editor-active-quality"
    assert "inside 48 hours" in memory["body"]


def test_editorial_memories_are_retrieved_as_actionable_guidance(engine_conn):
    record_copy_edit_pair(
        engine_conn,
        action_id=77,
        before="Generic job everyone.",
        after="King Levy closed the exact 180-point gap.",
    )

    guidance = _editorial_guidance(engine_conn)

    assert guidance
    assert any("AFTER" in item["guidance"] for item in guidance)
    assert all("editorial" in item["tags"] for item in guidance)


def test_repetition_eval_compares_active_copy_within_lane():
    posts = [
        {"message_id": "1", "lane": "elixir", "at": "2026-07-15T01:00:00Z",
         "copy": "Gem pushed another trophy peak today with the same deck and the same result."},
        {"message_id": "2", "lane": "elixir", "at": "2026-07-15T02:00:00Z",
         "copy": "Gem pushed another trophy peak today with the same deck and the same result!"},
    ]

    findings = _repetition_findings(posts)

    assert findings["2"]["message_id"] == "1"
    assert findings["2"]["similarity"] > 0.9


def test_editorial_admission_suppresses_only_routine_solo_repeat():
    signal = {
        "signal_key": "card_level_milestone:#GEM:16",
        "event_type": "card_level_milestone",
        "subject_tag": "#GEM",
    }
    read = {
        "signals_by_lane": {"milestone": [signal]},
        "hard_post_signals": [],
        "recent_member_spotlights": [{
            "member_tag": "#GEM", "member_ref": "Gem", "solo": True,
            "at": _now(-4), "summary": "Gem trophy peak",
        }],
    }
    plan = {"posts": [{
        "channel": "elixir", "leads_with": "milestone",
        "member_tags": ["#GEM"], "member_names": ["Gem"],
        "summary": "Gem card level", "content": "Gem reached card level 16.",
        "covers_signal_keys": [signal["signal_key"]],
    }]}

    admitted, suppressed = apply_editorial_admission(read, plan)

    assert admitted["posts"] == []
    assert suppressed[0]["member_tag"] == "#GEM"
    assert "48-hour" in admitted["skipped_reason"]


def test_editorial_admission_keeps_notable_repeat_and_roundup():
    notable = {
        "signal_key": "badge_earned:#GEM:legendary",
        "event_type": "badge_earned",
        "subject_tag": "#GEM",
        "badge_tier": "legendary",
    }
    read = {
        "signals_by_lane": {"milestone": [notable]},
        "hard_post_signals": [],
        "recent_member_spotlights": [{
            "member_tag": "#GEM", "member_ref": "Gem", "solo": True,
        }],
    }
    notable_post = {
        "channel": "elixir", "leads_with": "milestone",
        "member_tags": ["#GEM"], "member_names": ["Gem"],
        "content": "Gem earned a Legendary badge.",
        "covers_signal_keys": [notable["signal_key"]],
    }
    roundup = {
        "channel": "elixir", "leads_with": "milestone",
        "member_tags": ["#GEM", "#STONE"], "member_names": ["Gem", "Stone"],
        "content": "Gem and Stone had a week.",
        "covers_signal_keys": ["one", "two"],
    }

    admitted, suppressed = apply_editorial_admission(
        read, {"posts": [notable_post, roundup]}
    )

    assert admitted["posts"] == [notable_post, roundup]
    assert suppressed == []
