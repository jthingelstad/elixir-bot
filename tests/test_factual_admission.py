from agent.factual_admission import admit_structured_response

FACTS = {
    "is_colosseum_week": True,
    "finish_line": None,
    "every_battle_counts_for_standings": True,
}


def _response(content):
    return {"event_type": "channel_response", "summary": "war", "content": content}


def test_clean_response_passes_without_repair():
    called = []
    result, trace = admit_structured_response(
        _response("No finish line in Colosseum; every battle counts."),
        FACTS,
        repair_fn=lambda *_: called.append(True),
    )
    assert trace["decision"] == "pass"
    assert called == []
    assert "every battle" in result["content"]


def test_bad_response_gets_one_constrained_repair():
    original = _response("We crossed the 5,000 finish line.")

    def repair(response, findings, facts):
        assert findings and facts["finish_line"] is None
        return {
            **response,
            "content": "Colosseum has no finish line; every battle counts.",
        }

    result, trace = admit_structured_response(original, FACTS, repair_fn=repair)
    assert trace["decision"] == "repaired"
    assert result["event_type"] == original["event_type"]


def test_persistent_contradiction_falls_back_deterministically():
    original = _response("The remaining decks do not count; chest rewards only.")
    result, trace = admit_structured_response(
        original,
        FACTS,
        repair_fn=lambda *_: {**original, "content": "Past the finish line."},
    )
    assert trace["decision"] == "fallback"
    assert "no finish line" in result["content"].lower()
    assert "every battle" in result["content"].lower()
