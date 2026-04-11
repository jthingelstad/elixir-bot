"""Discord Views and Buttons for the card training quiz."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

import discord
import pytz

from modules.card_training import questions, storage

log = logging.getLogger("elixir.card_training")

CARD_TRAINING_CHANNEL_ID = int(os.getenv("CARD_TRAINING_CHANNEL_ID", "0"))
CHICAGO = pytz.timezone("America/Chicago")

CHOICE_LABELS = ["A", "B", "C", "D"]
CHOICE_STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
    discord.ButtonStyle.primary,
]


def _today_chicago() -> str:
    return datetime.now(CHICAGO).strftime("%Y-%m-%d")


def _build_question_embed(
    question: dict,
    *,
    index: int | None = None,
    total: int | None = None,
    daily: bool = False,
) -> discord.Embed:
    """Build a Discord embed for a quiz question."""
    title = "Elixir University"
    if daily:
        title += " | Daily Question"

    embed = discord.Embed(
        title=title,
        description=question["question_text"],
        color=discord.Color.purple(),
    )

    if question.get("image_url"):
        embed.set_image(url=question["image_url"])

    if index is not None and total is not None:
        embed.set_footer(text=f"Question {index + 1}/{total}")
    elif daily:
        today = _today_chicago()
        embed.set_footer(text=f"Daily Question | {today}")

    return embed


def _build_result_embed(question: dict, user_choice: int, correct: bool) -> discord.Embed:
    """Build a result embed showing correct/incorrect with explanation."""
    if correct:
        embed = discord.Embed(
            title="Correct!",
            description=question["explanation"],
            color=discord.Color.green(),
        )
    else:
        correct_answer = question["choices"][question["correct_index"]]
        embed = discord.Embed(
            title="Not quite!",
            description=f"The answer was **{correct_answer}**.\n\n{question['explanation']}",
            color=discord.Color.red(),
        )

    # Show the relevant card image in the result
    result_image = question.get("result_image_url") or question.get("image_url")
    if result_image:
        embed.set_thumbnail(url=result_image)

    return embed


# ---------------------------------------------------------------------------
# Interactive quiz session (ephemeral, multi-question)
# ---------------------------------------------------------------------------

class QuizQuestionView(discord.ui.View):
    """A single quiz question with 4 multiple-choice buttons."""

    def __init__(self, question: dict, *, session: "QuizSession"):
        super().__init__(timeout=900)  # 15 minutes per question
        self.question = question
        self.session = session
        self.answered = False

        for i, choice in enumerate(question["choices"]):
            button = QuizChoiceButton(
                label=f"{CHOICE_LABELS[i]}. {choice}",
                choice_index=i,
                custom_id=f"quiz_{session.session_id}_{session.current_index}_{i}",
            )
            self.add_item(button)


class QuizChoiceButton(discord.ui.Button):
    def __init__(self, label: str, choice_index: int, custom_id: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=custom_id,
        )
        self.choice_index = choice_index

    async def callback(self, interaction: discord.Interaction):
        view: QuizQuestionView = self.view
        if view.answered:
            await interaction.response.send_message(
                "You've already answered this question.", ephemeral=True
            )
            return

        view.answered = True
        for item in view.children:
            item.disabled = True

        question = view.question
        is_correct = self.choice_index == question["correct_index"]

        # Record the response
        try:
            await asyncio.to_thread(
                storage.record_response,
                view.session.session_id,
                view.session.current_index,
                question["question_type"],
                question["question_text"],
                question["choices"][question["correct_index"]],
                question["choices"][self.choice_index],
                is_correct,
                card_ids=question.get("card_ids"),
            )
        except Exception:
            log.exception("Failed to record quiz response for session %s", view.session.session_id)

        if is_correct:
            view.session.correct_count += 1

        result_embed = _build_result_embed(question, self.choice_index, is_correct)

        # Check if there are more questions
        view.session.current_index += 1
        if view.session.current_index < len(view.session.questions):
            # Send result, then next question
            next_q = view.session.questions[view.session.current_index]
            next_embed = _build_question_embed(
                next_q,
                index=view.session.current_index,
                total=len(view.session.questions),
            )
            next_view = QuizQuestionView(next_q, session=view.session)

            await interaction.response.edit_message(embed=result_embed, view=None)
            await interaction.followup.send(embed=next_embed, view=next_view, ephemeral=True)
        else:
            # Quiz complete
            try:
                await asyncio.to_thread(
                    storage.complete_session,
                    view.session.session_id,
                    view.session.correct_count,
                )
            except Exception:
                log.exception("Failed to complete quiz session %s", view.session.session_id)

            score = view.session.correct_count
            total = len(view.session.questions)
            pct = round(100 * score / total) if total > 0 else 0

            summary_embed = discord.Embed(
                title="Quiz Complete!",
                description=(
                    f"**Score: {score}/{total}** ({pct}%)\n\n"
                    f"{_score_message(pct)}"
                ),
                color=discord.Color.gold(),
            )

            await interaction.response.edit_message(embed=result_embed, view=None)
            await interaction.followup.send(embed=summary_embed, ephemeral=True)

        view.stop()


class QuizSession:
    """Tracks state for a multi-question interactive quiz."""

    def __init__(self, session_id: int, questions_list: list[dict]):
        self.session_id = session_id
        self.questions = questions_list
        self.current_index = 0
        self.correct_count = 0


def _score_message(pct: int) -> str:
    if pct == 100:
        return "Perfect score! You really know your cards."
    elif pct >= 80:
        return "Impressive card knowledge!"
    elif pct >= 60:
        return "Solid work. Keep training!"
    elif pct >= 40:
        return "Not bad. There's room to grow."
    else:
        return "Keep at it. Every quiz makes you sharper."


# ---------------------------------------------------------------------------
# Daily quiz question (persistent, public message with ephemeral answers)
# ---------------------------------------------------------------------------

class DailyQuestionView(discord.ui.View):
    """Persistent view for the daily quiz question."""

    def __init__(self, question: dict, daily_session_id: int):
        # No timeout — persists until bot restart (re-registered on startup)
        super().__init__(timeout=None)
        self.question = question
        self.daily_session_id = daily_session_id

        for i, choice in enumerate(question["choices"]):
            button = DailyChoiceButton(
                label=f"{CHOICE_LABELS[i]}. {choice}",
                choice_index=i,
                custom_id=f"daily_quiz_{daily_session_id}_{i}",
            )
            self.add_item(button)


class DailyChoiceButton(discord.ui.Button):
    def __init__(self, label: str, choice_index: int, custom_id: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=custom_id,
        )
        self.choice_index = choice_index

    async def callback(self, interaction: discord.Interaction):
        view: DailyQuestionView = self.view
        question = view.question
        user_id = str(interaction.user.id)
        today = _today_chicago()

        try:
            result = await asyncio.to_thread(
                storage.record_daily_response_atomic,
                view.daily_session_id,
                user_id,
                question,
                self.choice_index,
                today,
            )
        except Exception:
            log.exception("Daily quiz response failed for user %s", user_id)
            await interaction.response.send_message(
                "Something went wrong recording your answer. Please try again.",
                ephemeral=True,
            )
            return

        if result["already_answered"]:
            await interaction.response.send_message(
                "You've already answered today's question. Come back tomorrow!",
                ephemeral=True,
            )
            return

        is_correct = result["is_correct"]
        result_embed = _build_result_embed(question, self.choice_index, is_correct)

        # Add streak info to the result
        streak_info = result["streak_info"]
        if streak_info:
            streak = streak_info["current_streak"] or 0
            if is_correct and streak > 1:
                result_embed.add_field(
                    name="Streak",
                    value=f"{streak} days in a row!",
                    inline=True,
                )
            elif is_correct and streak == 1:
                result_embed.add_field(
                    name="Streak",
                    value="Streak started! Keep it going tomorrow.",
                    inline=True,
                )

        await interaction.response.send_message(embed=result_embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Helpers for slash commands
# ---------------------------------------------------------------------------

async def start_interactive_quiz(
    interaction: discord.Interaction,
    question_count: int,
):
    """Start an interactive quiz session for the user."""
    quiz_questions = await asyncio.to_thread(
        questions.generate_quiz_set, question_count,
    )

    if not quiz_questions:
        await interaction.response.send_message(
            "The card catalog is empty. The quiz needs card data to generate questions. "
            "An admin can trigger a card catalog sync.",
            ephemeral=True,
        )
        return

    if len(quiz_questions) < question_count:
        log.warning(
            "Quiz for user %s: requested %d questions, got %d",
            interaction.user.id, question_count, len(quiz_questions),
        )

    session_id = await asyncio.to_thread(
        storage.create_session,
        str(interaction.user.id),
        "interactive",
        len(quiz_questions),
        channel_id=str(interaction.channel_id),
    )

    session = QuizSession(session_id, quiz_questions)
    first_q = quiz_questions[0]

    embed = _build_question_embed(first_q, index=0, total=len(quiz_questions))
    view = QuizQuestionView(first_q, session=session)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def post_daily_question(channel: discord.TextChannel):
    """Post the daily quiz question to the given channel."""
    question = await asyncio.to_thread(questions.generate_random_question)
    if not question:
        log.warning("Daily quiz: no question generated (card catalog may be empty)")
        return None

    question_json = json.dumps(question)
    session_id = await asyncio.to_thread(
        storage.create_session,
        "_system_daily_",
        "daily",
        1,
        channel_id=str(channel.id),
        question_json=question_json,
    )

    embed = _build_question_embed(question, daily=True)
    view = DailyQuestionView(question, session_id)

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        log.exception("Failed to send daily quiz to channel %s", channel.id)
        return None

    # Store message_id so the view can be re-registered after restart
    await asyncio.to_thread(
        storage.update_session_message_id, session_id, str(message.id),
    )

    return message


async def restore_daily_view(bot: discord.Client):
    """Re-register the persistent daily quiz view after a bot restart.

    Looks up the most recent daily session with a stored message_id and
    question, then registers the view so buttons keep working.
    """
    session = await asyncio.to_thread(storage.get_active_daily_session)
    if not session:
        return

    question_json = session.get("question_json")
    message_id = session.get("message_id")
    if not question_json or not message_id:
        return

    try:
        question = json.loads(question_json)
    except (json.JSONDecodeError, TypeError):
        log.warning("Daily quiz restore: invalid question_json for session %s", session["session_id"])
        return

    view = DailyQuestionView(question, session["session_id"])
    bot.add_view(view, message_id=int(message_id))
    log.info("Restored daily quiz view for session %s (message %s)", session["session_id"], message_id)
