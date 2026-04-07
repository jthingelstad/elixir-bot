"""Quiz session, response, and streak persistence."""

import json
from datetime import datetime, timezone

from db import get_connection


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(
    discord_user_id: str,
    session_type: str,
    question_count: int,
    *,
    member_id: int | None = None,
    channel_id: str | None = None,
    message_id: str | None = None,
    question_json: str | None = None,
    conn=None,
) -> int:
    """Create a new quiz session and return its session_id."""
    close = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO quiz_sessions
                   (discord_user_id, member_id, session_type, question_count,
                    started_at, channel_id, message_id, question_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (discord_user_id, member_id, session_type, question_count,
             _utcnow(), channel_id, message_id, question_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if close:
            conn.close()


def update_session_message_id(session_id: int, message_id: str, conn=None):
    """Set the Discord message_id for a session (used for daily quiz persistence)."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "UPDATE quiz_sessions SET message_id = ? WHERE session_id = ?",
            (message_id, session_id),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def get_active_daily_session(conn=None) -> dict | None:
    """Return the most recent daily session that has a message_id and question_json."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM quiz_sessions
               WHERE session_type = 'daily' AND message_id IS NOT NULL AND question_json IS NOT NULL
               ORDER BY started_at DESC LIMIT 1""",
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def record_response(
    session_id: int,
    question_index: int,
    question_type: str,
    question_text: str,
    correct_answer: str,
    user_answer: str | None,
    is_correct: bool | None,
    card_ids: list[int] | None = None,
    conn=None,
):
    """Record a single quiz response."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """INSERT INTO quiz_responses
                   (session_id, question_index, question_type, question_text,
                    correct_answer, user_answer, is_correct, answered_at, card_ids_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, question_index, question_type, question_text,
                correct_answer, user_answer,
                1 if is_correct else (0 if is_correct is not None else None),
                _utcnow() if user_answer is not None else None,
                json.dumps(card_ids) if card_ids else None,
            ),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def complete_session(session_id: int, correct_count: int, conn=None):
    """Mark a quiz session as completed."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "UPDATE quiz_sessions SET correct_count = ?, completed_at = ? WHERE session_id = ?",
            (correct_count, _utcnow(), session_id),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Daily streaks
# ---------------------------------------------------------------------------

def update_daily_streak(discord_user_id: str, is_correct: bool, date_str: str, conn=None):
    """Update the daily streak for a user.

    date_str should be YYYY-MM-DD in Chicago time.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM quiz_daily_streaks WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()

        if row is None:
            conn.execute(
                """INSERT INTO quiz_daily_streaks
                       (discord_user_id, current_streak, longest_streak,
                        last_correct_date, total_daily_correct, total_daily_answered)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    discord_user_id,
                    1 if is_correct else 0,
                    1 if is_correct else 0,
                    date_str if is_correct else None,
                    1 if is_correct else 0,
                    1,
                ),
            )
        else:
            last_date = row["last_correct_date"]
            current_streak = row["current_streak"]
            longest_streak = row["longest_streak"]
            total_correct = row["total_daily_correct"]
            total_answered = row["total_daily_answered"]

            if is_correct:
                if last_date == date_str:
                    # Already answered correctly today — no streak change
                    pass
                elif _is_consecutive_day(last_date, date_str):
                    current_streak += 1
                else:
                    current_streak = 1
                longest_streak = max(longest_streak, current_streak)
                total_correct += 1
                last_date = date_str
            else:
                if last_date != date_str:
                    # Wrong answer on a new day — streak breaks
                    current_streak = 0

            total_answered += 1

            conn.execute(
                """UPDATE quiz_daily_streaks
                   SET current_streak = ?, longest_streak = ?,
                       last_correct_date = ?, total_daily_correct = ?,
                       total_daily_answered = ?
                   WHERE discord_user_id = ?""",
                (current_streak, longest_streak, last_date,
                 total_correct, total_answered, discord_user_id),
            )

        conn.commit()
    finally:
        if close:
            conn.close()


def _is_consecutive_day(prev_date_str: str | None, cur_date_str: str) -> bool:
    """Check if cur_date is exactly one day after prev_date (YYYY-MM-DD strings)."""
    if not prev_date_str:
        return False
    try:
        prev = datetime.strptime(prev_date_str, "%Y-%m-%d")
        cur = datetime.strptime(cur_date_str, "%Y-%m-%d")
        return (cur - prev).days == 1
    except (ValueError, TypeError):
        return False


def get_daily_streak(discord_user_id: str, conn=None) -> dict | None:
    """Return streak info for a user, or None if they've never answered."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM quiz_daily_streaks WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def has_answered_daily_today(discord_user_id: str, date_str: str, conn=None) -> bool:
    """Check if user has already answered a daily question today."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            """SELECT 1 FROM quiz_sessions s
               JOIN quiz_responses r ON r.session_id = s.session_id
               WHERE s.discord_user_id = ? AND s.session_type = 'daily'
                 AND r.answered_at LIKE ?
               LIMIT 1""",
            (discord_user_id, f"{date_str}%"),
        ).fetchone()
        return row is not None
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------------------
# Stats and leaderboard
# ---------------------------------------------------------------------------

def get_quiz_leaderboard(limit: int = 10, conn=None) -> list[dict]:
    """Return top daily streak holders."""
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """SELECT discord_user_id, current_streak, longest_streak,
                      total_daily_correct, total_daily_answered
               FROM quiz_daily_streaks
               ORDER BY current_streak DESC, longest_streak DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_member_quiz_stats(discord_user_id: str, conn=None) -> dict:
    """Return personal quiz stats summary."""
    close = conn is None
    conn = conn or get_connection()
    try:
        # Session stats
        session_row = conn.execute(
            """SELECT COUNT(*) AS total_sessions,
                      SUM(correct_count) AS total_correct,
                      SUM(question_count) AS total_questions
               FROM quiz_sessions
               WHERE discord_user_id = ? AND completed_at IS NOT NULL""",
            (discord_user_id,),
        ).fetchone()

        # Streak stats
        streak = get_daily_streak(discord_user_id, conn=conn)

        return {
            "total_sessions": (session_row["total_sessions"] or 0) if session_row else 0,
            "total_correct": (session_row["total_correct"] or 0) if session_row else 0,
            "total_questions": (session_row["total_questions"] or 0) if session_row else 0,
            "daily_streak": streak,
        }
    finally:
        if close:
            conn.close()
