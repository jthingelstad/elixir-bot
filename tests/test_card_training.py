"""Tests for the card training / quiz module."""

import json

import db
from modules.card_training import questions, storage
from storage.card_catalog import sync_card_catalog, lookup_cards, get_random_cards, _escape_like


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quiz_db():
    """Return an in-memory connection with full schema (including card_catalog + quiz tables)."""
    return db.get_connection(":memory:")


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
    def test_lookup_by_name(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            results = lookup_cards(name="Knight", conn=conn)
            assert any(r["name"] == "Knight" for r in results)
        finally:
            conn.close()

    def test_lookup_wildcards_in_name_do_not_expand(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            # "%"  should not match everything
            results = lookup_cards(name="%", conn=conn)
            assert len(results) == 0
        finally:
            conn.close()

    def test_lookup_by_rarity(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            results = lookup_cards(rarity="epic", conn=conn)
            assert all(r["rarity"] == "epic" for r in results)
            assert len(results) >= 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Question generation tests
# ---------------------------------------------------------------------------

class TestQuestionGeneration:
    def test_generate_elixir_cost_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_elixir_cost_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "elixir_cost"
            assert len(q["choices"]) == 4
            assert 0 <= q["correct_index"] <= 3
            assert q["card_ids"]
        finally:
            conn.close()

    def test_generate_rarity_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_rarity_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "rarity"
            assert len(q["choices"]) == 4
        finally:
            conn.close()

    def test_generate_card_type_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_card_type_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "card_type"
        finally:
            conn.close()

    def test_generate_cost_comparison_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_cost_comparison_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "cost_comparison"
            assert len(q["choices"]) == 4
        finally:
            conn.close()

    def test_generate_evolution_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_evolution_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "evolution_mode"
        finally:
            conn.close()

    def test_generate_champion_identification_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_champion_identification_question(conn=conn)
            assert q is not None
            assert q["question_type"] == "champion_id"
        finally:
            conn.close()

    def test_generate_random_question(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            q = questions.generate_random_question(conn=conn)
            assert q is not None
            assert "question_text" in q
            assert "choices" in q
        finally:
            conn.close()

    def test_generate_quiz_set_returns_requested_count(self):
        conn = _quiz_db()
        try:
            _seed_catalog(conn)
            qs = questions.generate_quiz_set(5, conn=conn)
            assert len(qs) == 5
        finally:
            conn.close()

    def test_generate_quiz_set_empty_catalog_returns_empty(self):
        conn = _quiz_db()
        try:
            qs = questions.generate_quiz_set(5, conn=conn)
            assert qs == []
        finally:
            conn.close()

    def test_elixir_cost_distractors_never_crash(self):
        """Even with a very constrained cost range, distractor generation should not crash."""
        conn = _quiz_db()
        try:
            # Seed a single card
            sync_card_catalog({"items": [
                {"id": 26000099, "name": "TestCard", "elixirCost": 5, "rarity": "Common",
                 "maxLevel": 15, "iconUrls": {"medium": "https://example.com/test.png"}},
            ]}, conn=conn)
            q = questions.generate_elixir_cost_question(conn=conn)
            assert q is not None
            # Should have at most 4 choices, at least 2 (correct + some distractors)
            assert len(q["choices"]) >= 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Storage: sessions and responses
# ---------------------------------------------------------------------------

class TestQuizStorage:
    def test_create_and_complete_session(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("user1", "interactive", 5, conn=conn)
            assert sid > 0

            storage.record_response(
                sid, 0, "elixir_cost", "What costs?", "3", "3", True, conn=conn,
            )
            storage.complete_session(sid, 1, conn=conn)

            row = conn.execute(
                "SELECT * FROM quiz_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            assert row["correct_count"] == 1
            assert row["completed_at"] is not None
        finally:
            conn.close()

    def test_daily_session_with_message_id(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session(
                "_system_daily_", "daily", 1,
                channel_id="123", question_json='{"q": 1}', conn=conn,
            )
            storage.update_session_message_id(sid, "msg456", conn=conn)

            session = storage.get_active_daily_session(conn=conn)
            assert session is not None
            assert session["message_id"] == "msg456"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Streak logic
# ---------------------------------------------------------------------------

class TestStreakLogic:
    def test_first_correct_answer_starts_streak(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 1
            assert streak["longest_streak"] == 1
            assert streak["total_daily_correct"] == 1
            assert streak["total_daily_answered"] == 1
        finally:
            conn.close()

    def test_consecutive_days_build_streak(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-02", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-03", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 3
            assert streak["longest_streak"] == 3
        finally:
            conn.close()

    def test_gap_resets_streak(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-02", conn=conn)
            # Skip a day
            storage.update_daily_streak("user1", True, "2025-04-04", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 1
            assert streak["longest_streak"] == 2
        finally:
            conn.close()

    def test_wrong_answer_breaks_streak(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-02", conn=conn)
            storage.update_daily_streak("user1", False, "2025-04-03", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 0
            assert streak["longest_streak"] == 2
        finally:
            conn.close()

    def test_wrong_answer_same_day_breaks_streak(self):
        """Issue #2: wrong answer on same day as correct should still break streak."""
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-02", conn=conn)
            # Wrong answer on same day — streak should break
            storage.update_daily_streak("user1", False, "2025-04-02", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 0
        finally:
            conn.close()

    def test_first_wrong_answer_zero_streak(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", False, "2025-04-01", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 0
            assert streak["longest_streak"] == 0
            assert streak["total_daily_correct"] == 0
            assert streak["total_daily_answered"] == 1
        finally:
            conn.close()

    def test_correct_same_day_no_double_count_streak(self):
        """Answering correctly twice on same day should not increment streak."""
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            streak = storage.get_daily_streak("user1", conn=conn)
            assert streak["current_streak"] == 1
            # total_daily_correct counts each call
            assert streak["total_daily_correct"] == 2
        finally:
            conn.close()


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

    def test_records_correct_answer(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("_system_daily_", "daily", 1, conn=conn)
            question = self._make_question()
            result = storage.record_daily_response_atomic(
                sid, "user1", question, 1, "2025-04-01", conn=conn,
            )
            assert result["already_answered"] is False
            assert result["is_correct"] is True
            assert result["streak_info"] is not None
            assert result["streak_info"]["current_streak"] == 1
        finally:
            conn.close()

    def test_records_wrong_answer(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("_system_daily_", "daily", 1, conn=conn)
            question = self._make_question()
            result = storage.record_daily_response_atomic(
                sid, "user1", question, 0, "2025-04-01", conn=conn,
            )
            assert result["already_answered"] is False
            assert result["is_correct"] is False
            assert result["streak_info"]["current_streak"] == 0
        finally:
            conn.close()

    def test_prevents_duplicate_answer(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("_system_daily_", "daily", 1, conn=conn)
            question = self._make_question()

            result1 = storage.record_daily_response_atomic(
                sid, "user1", question, 1, "2025-04-01", conn=conn,
            )
            assert result1["already_answered"] is False

            # Second attempt on same day should be blocked
            result2 = storage.record_daily_response_atomic(
                sid, "user1", question, 0, "2025-04-01", conn=conn,
            )
            assert result2["already_answered"] is True
        finally:
            conn.close()

    def test_different_users_can_answer_same_day(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("_system_daily_", "daily", 1, conn=conn)
            question = self._make_question()

            r1 = storage.record_daily_response_atomic(
                sid, "user1", question, 1, "2025-04-01", conn=conn,
            )
            r2 = storage.record_daily_response_atomic(
                sid, "user2", question, 0, "2025-04-01", conn=conn,
            )
            assert r1["already_answered"] is False
            assert r2["already_answered"] is False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Leaderboard and stats
# ---------------------------------------------------------------------------

class TestLeaderboardAndStats:
    def test_leaderboard_ordering(self):
        conn = _quiz_db()
        try:
            storage.update_daily_streak("user1", True, "2025-04-01", conn=conn)
            storage.update_daily_streak("user1", True, "2025-04-02", conn=conn)
            storage.update_daily_streak("user2", True, "2025-04-01", conn=conn)

            board = storage.get_quiz_leaderboard(10, conn=conn)
            assert len(board) == 2
            assert board[0]["discord_user_id"] == "user1"
            assert board[0]["current_streak"] == 2
        finally:
            conn.close()

    def test_member_quiz_stats(self):
        conn = _quiz_db()
        try:
            sid = storage.create_session("user1", "interactive", 2, conn=conn)
            storage.record_response(sid, 0, "rarity", "Q1", "Rare", "Rare", True, conn=conn)
            storage.record_response(sid, 1, "rarity", "Q2", "Epic", "Common", False, conn=conn)
            storage.complete_session(sid, 1, conn=conn)

            stats = storage.get_member_quiz_stats("user1", conn=conn)
            assert stats["total_sessions"] == 1
            assert stats["total_correct"] == 1
            assert stats["total_questions"] == 2
        finally:
            conn.close()


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
