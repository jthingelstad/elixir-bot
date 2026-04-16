"""Tests for the card training / quiz module."""

import json
from unittest.mock import patch

import pytest

import db
from modules.card_training import questions, storage
from storage.card_catalog import sync_card_catalog, lookup_cards, get_random_cards, _escape_like


@pytest.fixture(autouse=True)
def _stub_quiz_explanation(monkeypatch):
    """Stub out the LLM explanation call inside the questions module so
    scaffold tests don't hit the API.

    We only patch the ``explain_or_fallback`` reference *inside questions*,
    not the one exported from ``explanations``. TestExplanationLayer tests
    exercise the real helper.
    """
    def _no_llm(**kwargs):
        return kwargs["fallback"]

    monkeypatch.setattr(questions, "explain_or_fallback", _no_llm)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def quiz_db():
    """Yield an in-memory connection with full schema; auto-close after test."""
    conn = db.get_connection(":memory:")
    yield conn
    conn.close()


_SAMPLE_API_RESPONSE = {
    "items": [
        {"id": 26000000, "name": "Knight", "elixirCost": 3, "rarity": "Common", "maxLevel": 15,
         "maxEvolutionLevel": 1, "iconUrls": {"medium": "https://example.com/knight.png"}},
        {"id": 26000001, "name": "Archers", "elixirCost": 3, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/archers.png"}},
        {"id": 26000002, "name": "Giant", "elixirCost": 5, "rarity": "Rare", "maxLevel": 13,
         "iconUrls": {"medium": "https://example.com/giant.png"}},
        {"id": 26000003, "name": "P.E.K.K.A", "elixirCost": 7, "rarity": "Epic", "maxLevel": 11,
         "iconUrls": {"medium": "https://example.com/pekka.png"}},
        {"id": 26000004, "name": "Minions", "elixirCost": 3, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/minions.png"}},
        {"id": 26000005, "name": "Balloon", "elixirCost": 5, "rarity": "Epic", "maxLevel": 11,
         "iconUrls": {"medium": "https://example.com/balloon.png"}},
        {"id": 26000006, "name": "Witch", "elixirCost": 5, "rarity": "Epic", "maxLevel": 11,
         "maxEvolutionLevel": 1, "iconUrls": {"medium": "https://example.com/witch.png"}},
        {"id": 26000007, "name": "Barbarians", "elixirCost": 5, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/barbarians.png"}},
        {"id": 26000010, "name": "Goblin", "elixirCost": 2, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/goblin.png"}},
        {"id": 27000000, "name": "Cannon", "elixirCost": 3, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/cannon.png"}},
        {"id": 28000000, "name": "Fireball", "elixirCost": 4, "rarity": "Rare", "maxLevel": 13,
         "iconUrls": {"medium": "https://example.com/fireball.png"}},
        {"id": 28000001, "name": "Arrows", "elixirCost": 3, "rarity": "Common", "maxLevel": 15,
         "iconUrls": {"medium": "https://example.com/arrows.png"}},
        {"id": 26000085, "name": "Mighty Miner", "elixirCost": 4, "rarity": "Champion", "maxLevel": 11,
         "iconUrls": {"medium": "https://example.com/mightyminer.png"}},
    ],
}


def _seed_catalog(conn):
    """Seed the card catalog with test data."""
    sync_card_catalog(_SAMPLE_API_RESPONSE, conn=conn)


# ---------------------------------------------------------------------------
# Card catalog / LIKE escape tests
# ---------------------------------------------------------------------------

class TestLikeEscape:
    def test_escape_percent(self):
        assert _escape_like("50%") == "50\\%"

    def test_escape_underscore(self):
        assert _escape_like("P_E_K_K_A") == "P\\_E\\_K\\_K\\_A"

    def test_escape_backslash(self):
        assert _escape_like("a\\b") == "a\\\\b"

    def test_no_escape_needed(self):
        assert _escape_like("Knight") == "Knight"


class TestCardCatalogLookup:
    def test_lookup_by_name(self, quiz_db):
        _seed_catalog(quiz_db)
        results = lookup_cards(name="Knight", conn=quiz_db)
        assert any(r["name"] == "Knight" for r in results)

    def test_lookup_wildcards_in_name_do_not_expand(self, quiz_db):
        _seed_catalog(quiz_db)
        # "%"  should not match everything
        results = lookup_cards(name="%", conn=quiz_db)
        assert len(results) == 0

    def test_lookup_by_rarity(self, quiz_db):
        _seed_catalog(quiz_db)
        results = lookup_cards(rarity="epic", conn=quiz_db)
        assert all(r["rarity"] == "epic" for r in results)
        assert len(results) >= 2


# ---------------------------------------------------------------------------
# Question generation tests
# ---------------------------------------------------------------------------

class TestQuestionGeneration:
    def test_generate_elixir_cost_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_elixir_cost_question(conn=quiz_db)
        assert q is not None
        assert q["question_type"] == "elixir_cost"
        assert len(q["choices"]) == 4
        assert 0 <= q["correct_index"] <= 3
        assert q["card_ids"]
        # Every v4.7 generator uses explain_or_fallback — with the fixture
        # stub, we should get the deterministic fallback text.
        assert q["explanation"]

    def test_generate_cost_comparison_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_cost_comparison_question(conn=quiz_db)
        assert q is not None
        assert q["question_type"] == "cost_comparison"
        assert len(q["choices"]) == 4
        # All 4 options must be the same card_type — the upgraded filter
        # compares apples to apples.
        card_ids = q["card_ids"]
        rows = quiz_db.execute(
            f"SELECT card_type, elixir_cost FROM card_catalog WHERE card_id IN ({','.join('?' * len(card_ids))})",
            card_ids,
        ).fetchall()
        types = {row["card_type"] for row in rows}
        assert len(types) == 1, f"cost_comparison mixed card types: {types}"
        costs = [row["elixir_cost"] for row in rows]
        assert max(costs) - min(costs) <= 3, f"cost window too wide: {costs}"

    def test_generate_positive_trade_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_positive_trade_question(conn=quiz_db)
        assert q is not None
        assert q["question_type"] == "positive_trade"
        assert len(q["choices"]) == 4
        # Correct answer is one of +2 / +1 / Even / -1 / -2
        correct = q["choices"][q["correct_index"]]
        assert correct in {"+2", "+1", "Even", "-1", "-2"}

    def test_generate_cycle_total_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_cycle_total_question(conn=quiz_db)
        assert q is not None
        assert q["question_type"] == "cycle_total"
        assert len(q["choices"]) == 4
        # Correct answer = sum of card costs
        card_ids = q["card_ids"]
        rows = quiz_db.execute(
            f"SELECT elixir_cost FROM card_catalog WHERE card_id IN ({','.join('?' * len(card_ids))})",
            card_ids,
        ).fetchall()
        expected = sum(row["elixir_cost"] for row in rows)
        assert q["choices"][q["correct_index"]] == str(expected)

    def test_generate_cycle_back_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_cycle_back_question(conn=quiz_db)
        assert q is not None
        assert q["question_type"] == "cycle_back"
        assert len(q["choices"]) == 4
        # Correct answer = sum of 3 of the 4 cards (the non-key cards)
        card_ids = q["card_ids"]
        assert len(card_ids) == 4
        rows = quiz_db.execute(
            f"SELECT card_id, elixir_cost FROM card_catalog WHERE card_id IN ({','.join('?' * len(card_ids))})",
            card_ids,
        ).fetchall()
        costs = {row["card_id"]: row["elixir_cost"] for row in rows}
        total = sum(costs.values())
        correct_value = int(q["choices"][q["correct_index"]])
        # correct_value should equal total minus exactly one card's cost
        assert any(correct_value == total - cost for cost in costs.values()), (
            f"cycle_back answer {correct_value} doesn't match total {total} minus any single card"
        )

    def test_generate_random_question(self, quiz_db):
        _seed_catalog(quiz_db)
        q = questions.generate_random_question(conn=quiz_db)
        assert q is not None
        assert "question_text" in q
        assert "choices" in q

    def test_generate_quiz_set_returns_requested_count(self, quiz_db):
        _seed_catalog(quiz_db)
        qs = questions.generate_quiz_set(5, conn=quiz_db)
        assert len(qs) == 5

    def test_generate_quiz_set_empty_catalog_still_generates_trade_question(self, quiz_db):
        """positive_trade works without a card catalog — the seed list is self-contained."""
        qs = questions.generate_quiz_set(5, conn=quiz_db)
        # Every question should be positive_trade (the only type that doesn't
        # need the catalog).
        assert all(q["question_type"] == "positive_trade" for q in qs)

    def test_retired_generators_are_gone(self):
        """rarity / card_type / evolution / champion_id were retired in v4.7."""
        assert not hasattr(questions, "generate_rarity_question")
        assert not hasattr(questions, "generate_card_type_question")
        assert not hasattr(questions, "generate_evolution_question")
        assert not hasattr(questions, "generate_champion_identification_question")

    def test_elixir_cost_distractors_never_crash(self, quiz_db):
        """Even with a very constrained cost range, distractor generation should not crash."""
        sync_card_catalog({"items": [
            {"id": 26000099, "name": "TestCard", "elixirCost": 5, "rarity": "Common",
             "maxLevel": 15, "iconUrls": {"medium": "https://example.com/test.png"}},
        ]}, conn=quiz_db)
        q = questions.generate_elixir_cost_question(conn=quiz_db)
        assert q is not None
        assert len(q["choices"]) >= 2


# ---------------------------------------------------------------------------
# Explanation layer
# ---------------------------------------------------------------------------

class TestExplanationLayer:
    def test_fallback_returned_when_no_explainer(self, monkeypatch):
        """If elixir_agent lacks explain_quiz_answer, we return the fallback."""
        from modules.card_training import explanations

        class _FakeAgent:
            pass

        monkeypatch.setitem(__import__('sys').modules, 'elixir_agent', _FakeAgent())
        result = explanations.explain_or_fallback(
            question_text="Q",
            correct_answer="A",
            context="ctx",
            fallback="FALLBACK",
        )
        assert result == "FALLBACK"

    def test_fallback_returned_when_explainer_raises(self, monkeypatch):
        from modules.card_training import explanations

        class _FakeAgent:
            def explain_quiz_answer(self, **_):
                raise RuntimeError("boom")

        monkeypatch.setitem(__import__('sys').modules, 'elixir_agent', _FakeAgent())
        result = explanations.explain_or_fallback(
            question_text="Q",
            correct_answer="A",
            context="ctx",
            fallback="FALLBACK",
        )
        assert result == "FALLBACK"

    def test_llm_text_returned_when_available(self, monkeypatch):
        from modules.card_training import explanations

        class _FakeAgent:
            def explain_quiz_answer(self, **kwargs):
                return "Tactical insight about " + kwargs["correct_answer"]

        monkeypatch.setitem(__import__('sys').modules, 'elixir_agent', _FakeAgent())
        result = explanations.explain_or_fallback(
            question_text="Q",
            correct_answer="Fireball",
            context="ctx",
            fallback="FALLBACK",
        )
        assert "Tactical insight about Fireball" == result


# ---------------------------------------------------------------------------
# Storage: sessions and responses
# ---------------------------------------------------------------------------

class TestQuizStorage:
    def test_create_and_complete_session(self, quiz_db):
        sid = storage.create_session("user1", "interactive", 5, conn=quiz_db)
        assert sid > 0

        storage.record_response(
            sid, 0, "elixir_cost", "What costs?", "3", "3", True, conn=quiz_db,
        )
        storage.complete_session(sid, 1, conn=quiz_db)

        row = quiz_db.execute(
            "SELECT * FROM quiz_sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        assert row["correct_count"] == 1
        assert row["completed_at"] is not None

    def test_daily_session_with_message_id(self, quiz_db):
        sid = storage.create_session(
            "_system_daily_", "daily", 1,
            channel_id="123", question_json='{"q": 1}', conn=quiz_db,
        )
        storage.update_session_message_id(sid, "msg456", conn=quiz_db)

        session = storage.get_active_daily_session(conn=quiz_db)
        assert session is not None
        assert session["message_id"] == "msg456"


# ---------------------------------------------------------------------------
# Streak logic
# ---------------------------------------------------------------------------

class TestStreakLogic:
    def test_first_correct_answer_starts_streak(self, quiz_db):
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 1
        assert streak["longest_streak"] == 1
        assert streak["total_daily_correct"] == 1
        assert streak["total_daily_answered"] == 1

    def test_consecutive_days_build_streak(self, quiz_db):
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-02", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-03", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 3
        assert streak["longest_streak"] == 3

    def test_gap_resets_streak(self, quiz_db):
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-02", conn=quiz_db)
        # Skip a day
        storage.update_daily_streak("user1", True, "2025-04-04", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 1
        assert streak["longest_streak"] == 2

    def test_wrong_answer_breaks_streak(self, quiz_db):
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-02", conn=quiz_db)
        storage.update_daily_streak("user1", False, "2025-04-03", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 0
        assert streak["longest_streak"] == 2

    def test_wrong_answer_same_day_breaks_streak(self, quiz_db):
        """Issue #2: wrong answer on same day as correct should still break streak."""
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-02", conn=quiz_db)
        # Wrong answer on same day — streak should break
        storage.update_daily_streak("user1", False, "2025-04-02", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 0

    def test_first_wrong_answer_zero_streak(self, quiz_db):
        storage.update_daily_streak("user1", False, "2025-04-01", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 0
        assert streak["longest_streak"] == 0
        assert streak["total_daily_correct"] == 0
        assert streak["total_daily_answered"] == 1

    def test_correct_same_day_no_double_count_streak(self, quiz_db):
        """Answering correctly twice on same day should not increment streak."""
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        streak = storage.get_daily_streak("user1", conn=quiz_db)
        assert streak["current_streak"] == 1
        # total_daily_correct counts each call
        assert streak["total_daily_correct"] == 2


# ---------------------------------------------------------------------------
# Atomic daily response
# ---------------------------------------------------------------------------

class TestAtomicDailyResponse:
    def _make_question(self):
        return {
            "question_text": "What is the elixir cost of Knight?",
            "choices": ["2", "3", "4", "5"],
            "correct_index": 1,
            "question_type": "elixir_cost",
            "card_ids": [26000000],
            "explanation": "Knight costs 3 elixir.",
        }

    def test_records_correct_answer(self, quiz_db):
        sid = storage.create_session("_system_daily_", "daily", 1, conn=quiz_db)
        question = self._make_question()
        result = storage.record_daily_response_atomic(
            sid, "user1", question, 1, "2025-04-01", conn=quiz_db,
        )
        assert result["already_answered"] is False
        assert result["is_correct"] is True
        assert result["streak_info"] is not None
        assert result["streak_info"]["current_streak"] == 1

    def test_records_wrong_answer(self, quiz_db):
        sid = storage.create_session("_system_daily_", "daily", 1, conn=quiz_db)
        question = self._make_question()
        result = storage.record_daily_response_atomic(
            sid, "user1", question, 0, "2025-04-01", conn=quiz_db,
        )
        assert result["already_answered"] is False
        assert result["is_correct"] is False
        assert result["streak_info"]["current_streak"] == 0

    def test_prevents_duplicate_answer(self, quiz_db):
        sid = storage.create_session("_system_daily_", "daily", 1, conn=quiz_db)
        question = self._make_question()

        result1 = storage.record_daily_response_atomic(
            sid, "user1", question, 1, "2025-04-01", conn=quiz_db,
        )
        assert result1["already_answered"] is False

        # Second attempt on same day should be blocked
        result2 = storage.record_daily_response_atomic(
            sid, "user1", question, 0, "2025-04-01", conn=quiz_db,
        )
        assert result2["already_answered"] is True

    def test_different_users_can_answer_same_day(self, quiz_db):
        sid = storage.create_session("_system_daily_", "daily", 1, conn=quiz_db)
        question = self._make_question()

        r1 = storage.record_daily_response_atomic(
            sid, "user1", question, 1, "2025-04-01", conn=quiz_db,
        )
        r2 = storage.record_daily_response_atomic(
            sid, "user2", question, 0, "2025-04-01", conn=quiz_db,
        )
        assert r1["already_answered"] is False
        assert r2["already_answered"] is False


# ---------------------------------------------------------------------------
# Leaderboard and stats
# ---------------------------------------------------------------------------

class TestLeaderboardAndStats:
    def test_leaderboard_ordering(self, quiz_db):
        storage.update_daily_streak("user1", True, "2025-04-01", conn=quiz_db)
        storage.update_daily_streak("user1", True, "2025-04-02", conn=quiz_db)
        storage.update_daily_streak("user2", True, "2025-04-01", conn=quiz_db)

        board = storage.get_quiz_leaderboard(10, conn=quiz_db)
        assert len(board) == 2
        assert board[0]["discord_user_id"] == "user1"
        assert board[0]["current_streak"] == 2

    def test_member_quiz_stats(self, quiz_db):
        sid = storage.create_session("user1", "interactive", 2, conn=quiz_db)
        storage.record_response(sid, 0, "rarity", "Q1", "Rare", "Rare", True, conn=quiz_db)
        storage.record_response(sid, 1, "rarity", "Q2", "Epic", "Common", False, conn=quiz_db)
        storage.complete_session(sid, 1, conn=quiz_db)

        stats = storage.get_member_quiz_stats("user1", conn=quiz_db)
        assert stats["total_sessions"] == 1
        assert stats["total_correct"] == 1
        assert stats["total_questions"] == 2


# ---------------------------------------------------------------------------
# _is_consecutive_day
# ---------------------------------------------------------------------------

class TestIsConsecutiveDay:
    def test_consecutive(self):
        assert storage._is_consecutive_day("2025-04-01", "2025-04-02") is True

    def test_same_day(self):
        assert storage._is_consecutive_day("2025-04-01", "2025-04-01") is False

    def test_gap(self):
        assert storage._is_consecutive_day("2025-04-01", "2025-04-03") is False

    def test_month_boundary(self):
        assert storage._is_consecutive_day("2025-03-31", "2025-04-01") is True

    def test_none_prev(self):
        assert storage._is_consecutive_day(None, "2025-04-01") is False

    def test_invalid_format(self):
        assert storage._is_consecutive_day("not-a-date", "2025-04-01") is False
