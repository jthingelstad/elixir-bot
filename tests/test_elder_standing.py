"""Elder Standing report — the table-free render and the grounding guard that
keeps the LLM from naming a member who isn't in the facts."""

from __future__ import annotations

from runtime.elder_standing import (
    facts_for_model,
    facts_member_names,
    output_is_grounded,
    render_elder_standing,
)

STANDING = {
    "composition": {"elders": 3, "members": 5},
    "holding": [{"name": "Atternam", "why": "98% war decks, 8 ranked battles"}],
    "rising": [{"name": "Fullboat", "why": "100% war decks, 98 ranked battles"}],
    "stepping_down": [
        {"name": "OllieTurtle", "why": "3% war decks", "reason": "outranked"}
    ],
}


def test_render_has_no_tables_and_names_everyone():
    text = render_elder_standing(STANDING, date="Tuesday, July 14")
    assert "|" not in text  # Discord renders no markdown tables
    assert "**Holding strong.**" in text and "**Stepping-down watch.**" in text
    for name in ("Atternam", "Fullboat", "OllieTurtle"):
        assert name in text


def test_grounding_accepts_real_names_and_ignores_bolded_stats():
    good = (
        "**Holding strong.**\n"
        "- **Atternam** — a huge **98%** war decks and **8** ranked battles.\n"
        "**On the rise.**\n"
        "- **Fullboat** — a perfect **100%** war decks."
    )
    assert output_is_grounded(good, STANDING) is True


def test_grounding_rejects_a_fabricated_member():
    bad = "- **Atternam** — solid.\n- **GhostPlayer** — invented out of thin air."
    assert output_is_grounded(bad, STANDING) is False


def test_facts_brief_lists_every_member():
    facts = facts_for_model(STANDING, date="Tuesday")
    for name in ("Atternam", "Fullboat", "OllieTurtle"):
        assert name in facts
    assert facts_member_names(STANDING) == {"Atternam", "Fullboat", "OllieTurtle"}


def test_empty_groups_render_gracefully():
    empty = {
        "composition": {"elders": 0, "members": 0},
        "holding": [],
        "rising": [],
        "stepping_down": [],
    }
    text = render_elder_standing(empty)
    assert "|" not in text
    assert "No one's slipping" in text
