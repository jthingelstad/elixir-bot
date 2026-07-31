def test_source_freshness_lists_only_members_needing_attention():
    """Per-member freshness telemetry was the largest field in the entire awareness
    read (~3,700 tokens/tick, 30% of it). Across 20 sampled production ticks, 907 of
    907 rows said status=ready with no reasons — the whole block told the brain that
    nothing was wrong, once per member. Ready members are counted; anything not
    plain-ready must survive IN FULL, because that is the part that changes a verdict.
    """
    from runtime.awareness.read import _compact_source_freshness

    ready = {
        "battlelog": {"age_minutes": 50.0, "fresh": True},
        "profile": {"age_minutes": 382.0, "fresh": True},
        "reasons": [],
        "status": "ready",
    }
    members = {f"#T{i}": dict(ready) for i in range(46)}
    members["#HELD"] = {"status": "held", "reasons": ["battlelog stale"]}
    members["#FLAGGED"] = {"status": "ready", "reasons": ["profile missing"]}
    out = _compact_source_freshness({"as_of": "x", "clan": {"fresh": True}, "members": members})

    assert out["members"]["ready_count"] == 46
    assert set(out["members"]["not_ready"]) == {"#HELD", "#FLAGGED"}
    assert out["members"]["not_ready"]["#HELD"]["reasons"] == ["battlelog stale"]
    assert out["as_of"] == "x" and out["clan"] == {"fresh": True}
    # the point of the exercise
    import json

    assert len(json.dumps(out)) < len(json.dumps({"as_of": "x", "members": members})) / 10


def test_source_freshness_compaction_is_safe_on_odd_shapes():
    from runtime.awareness.read import _compact_source_freshness

    assert _compact_source_freshness({}) == {}
    assert _compact_source_freshness({"members": {}}) == {"members": {}}
    assert _compact_source_freshness({"members": "nope"}) == {"members": "nope"}
    out = _compact_source_freshness({"members": {"#A": "not-a-dict"}})
    assert "#A" in out["members"]["not_ready"]
