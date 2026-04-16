"""LLM-backed quiz explanation layer.

Each question generator builds the mechanical part of the answer (the math,
the correct option, the 4 choices). The explanation — the sentence or two
that teaches *why it matters in play* — comes from a small Haiku call
scoped to the ``event:quiz_explain`` workflow.

The LLM never computes the answer or picks cards. It only narrates what
the deterministic scaffold already decided. If the call fails or returns
something unusable, ``explain_or_fallback`` returns the templated
explanation passed in so the quiz never breaks.
"""

from __future__ import annotations

import logging

log = logging.getLogger("elixir.card_training.explanations")


def explain_or_fallback(
    *,
    question_text: str,
    correct_answer: str,
    context: str,
    fallback: str,
) -> str:
    """Return an LLM-written explanation, or ``fallback`` on any failure.

    Parameters
    ----------
    question_text : the exact question shown to the member
    correct_answer : the correct multiple-choice option
    context : a short factual block the LLM should ground on (card stats,
        cost math, etc.)
    fallback : a deterministic explanation used if the LLM call fails or
        returns empty output

    The function imports ``elixir_agent`` lazily so tests can mock it and
    so the questions module doesn't pull in the full agent stack at import
    time.
    """
    try:
        import elixir_agent  # lazy to avoid agent-stack import at module load
        explainer = getattr(elixir_agent, "explain_quiz_answer", None)
        if explainer is None:
            return fallback
        result = explainer(
            question_text=question_text,
            correct_answer=correct_answer,
            context=context,
        )
    except Exception:
        log.warning("quiz explanation LLM call failed", exc_info=True)
        return fallback

    if not result:
        return fallback
    if isinstance(result, dict):
        text = (result.get("explanation") or "").strip()
    else:
        text = str(result).strip()
    if not text:
        return fallback
    return text


__all__ = ["explain_or_fallback"]
