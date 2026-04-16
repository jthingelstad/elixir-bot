"""Quiz question generation from card catalog data.

All question types produce a standard dict:
{
    "question_text": str,
    "image_url": str | None,
    "choices": [str, str, str, str],
    "correct_index": int,
    "explanation": str,
    "question_type": str,
    "card_ids": [int],
}

v4.7: retired rarity / card_type / evo-mode / champion-ID questions (low
tactical value). Kept elixir_cost + upgraded cost_comparison. Added three
tactically meaningful generators: positive_trade (curated scenarios),
cycle_total (4-card rotation cost), cycle_back (elixir to cycle).
Explanations are rewritten through a small LLM call in Elixir's voice,
with a deterministic fallback so the quiz never breaks.
"""

import logging
import random

from modules.card_training.explanations import explain_or_fallback
from modules.card_training.trade_scenarios import TRADE_SCENARIOS, TradeScenario
from storage.card_catalog import get_random_cards, lookup_cards

log = logging.getLogger("elixir.card_training.questions")

# Tower troops are not playable battle cards — exclude from all quiz questions.
_PLAYABLE_TYPES = ("troop", "building", "spell")


def _random_playable_cards(count: int, *, rarity=None, exclude_ids=None, conn=None) -> list[dict]:
    """Get random cards excluding tower troops."""
    cards = get_random_cards(count + 5, rarity=rarity, exclude_ids=exclude_ids, conn=conn)
    cards = [c for c in cards if c["card_type"] in _PLAYABLE_TYPES]
    return cards[:count]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shuffle_choices(correct: str, distractors: list[str]) -> tuple[list[str], int]:
    """Return shuffled choices and the index of the correct answer."""
    choices = [correct] + distractors[:3]
    combined = list(enumerate(choices))
    random.shuffle(combined)
    shuffled = [c for _, c in combined]
    correct_index = next(i for i, (orig_i, _) in enumerate(combined) if orig_i == 0)
    return shuffled, correct_index


def _type_display(card_type: str) -> str:
    return {
        "troop": "troop",
        "building": "building",
        "spell": "spell",
    }.get(card_type, card_type.replace("_", " ").lower())


def _format_trade_delta(value: int) -> str:
    """Render a trade value as '+2', '+1', 'Even', '-1', '-2'."""
    if value == 0:
        return "Even"
    sign = "+" if value > 0 else "-"
    return f"{sign}{abs(value)}"


# ---------------------------------------------------------------------------
# Question generators
# ---------------------------------------------------------------------------

def generate_elixir_cost_question(conn=None) -> dict | None:
    """What is the elixir cost of this card? (show image)

    Tactical value: cost literacy is the foundation of every other decision.
    """
    cards = _random_playable_cards(5, conn=conn)
    if not cards:
        return None

    card = cards[0]
    correct = str(card["elixir_cost"])
    cost = card["elixir_cost"]

    possible = [str(c) for c in range(max(1, cost - 2), cost + 3) if c != cost and 1 <= c <= 10]
    random.shuffle(possible)
    distractors = possible[:3]
    while len(distractors) < 3:
        available = [str(c) for c in range(1, 11) if str(c) != correct and str(c) not in distractors]
        if not available:
            break
        distractors.append(random.choice(available))

    choices, correct_index = _shuffle_choices(correct, distractors)

    fallback = (
        f"{card['name']} costs {cost} elixir — remember this when you're deciding "
        f"whether to answer it at the bridge or let it in for a defensive counter."
    )
    explanation = explain_or_fallback(
        question_text=f"What is the elixir cost of {card['name']}?",
        correct_answer=f"{cost} elixir",
        context=(
            f"Card: {card['name']} ({_type_display(card['card_type'])}, "
            f"{card['rarity']}). Cost: {cost} elixir."
        ),
        fallback=fallback,
    )

    return {
        "question_text": f"What is the elixir cost of **{card['name']}**?",
        "image_url": card.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": explanation,
        "question_type": "elixir_cost",
        "card_ids": [card["card_id"]],
    }


def generate_cost_comparison_question(conn=None) -> dict | None:
    """Which of these costs the most/least elixir?

    v4.7: filters to 4 cards of the same type within a 3-elixir cost band so
    choices are discriminating instead of trivially obvious. Compares apples
    to apples (e.g. four spells in the 2–5 range).
    """
    pool_size = 40
    cards = _random_playable_cards(pool_size, conn=conn)
    if len(cards) < 4:
        return None

    # Group by card_type, pick a type that has >=4 distinct-cost cards in some
    # 3-elixir window.
    by_type: dict[str, list[dict]] = {}
    for c in cards:
        by_type.setdefault(c["card_type"], []).append(c)

    candidates: list[tuple[str, list[dict]]] = []
    for card_type, group in by_type.items():
        if len(group) < 4:
            continue
        costs = sorted({c["elixir_cost"] for c in group})
        # Find a 3-elixir window with >=4 distinct costs (4 real choices)
        for window_start in costs:
            window_costs = [c for c in costs if window_start <= c <= window_start + 3]
            if len(window_costs) >= 4:
                cards_in_window = [c for c in group if c["elixir_cost"] in window_costs]
                if len({c["elixir_cost"] for c in cards_in_window}) >= 4:
                    candidates.append((card_type, cards_in_window))
                    break

    if not candidates:
        return None

    card_type, pool = random.choice(candidates)

    by_cost: dict[int, list[dict]] = {}
    for c in pool:
        by_cost.setdefault(c["elixir_cost"], []).append(c)

    selected: list[dict] = []
    for cost in sorted(by_cost.keys()):
        if len(selected) >= 4:
            break
        selected.append(random.choice(by_cost[cost]))

    if len(selected) < 4:
        return None
    selected = selected[:4]

    ask_most = random.choice([True, False])
    target = max(selected, key=lambda c: c["elixir_cost"]) if ask_most else min(selected, key=lambda c: c["elixir_cost"])
    word = "most" if ask_most else "least"

    choices = [c["name"] for c in selected]
    random.shuffle(choices)
    correct_index = choices.index(target["name"])

    type_plural = {"troop": "troops", "building": "buildings", "spell": "spells"}.get(card_type, card_type + "s")
    fallback = (
        f"{target['name']} costs {target['elixir_cost']} elixir — the {word} "
        f"of the four. Cost discipline between {type_plural} shapes your cycle "
        f"and your ability to answer pressure cheaply."
    )
    details = ", ".join(f"{c['name']} ({c['elixir_cost']})" for c in selected)
    explanation = explain_or_fallback(
        question_text=f"Which of these {type_plural} costs the {word} elixir?",
        correct_answer=f"{target['name']} at {target['elixir_cost']} elixir",
        context=f"Choices: {details}. Target: {target['name']} ({target['elixir_cost']}).",
        fallback=fallback,
    )

    return {
        "question_text": f"Which of these **{type_plural}** costs the **{word}** elixir?",
        "image_url": None,
        "result_image_url": target.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": explanation,
        "question_type": "cost_comparison",
        "card_ids": [c["card_id"] for c in selected],
    }


def generate_positive_trade_question(conn=None) -> dict | None:
    """Given a curated trade scenario, is it +2 / +1 / Even / -1 / -2?

    The seed list lives in ``trade_scenarios.py``. Math is deterministic:
    opponent elixir minus your elixir. The LLM only writes the explanation.
    """
    scenario: TradeScenario = random.choice(TRADE_SCENARIOS)
    trade_value = scenario.trade_value  # int in range roughly -3..+3
    # Clamp display to ±2 for a clean 5-option world; larger trades collapse.
    display_value = max(-2, min(2, trade_value))

    all_options = ["+2", "+1", "Even", "-1", "-2"]
    correct = _format_trade_delta(display_value)
    distractors = [o for o in all_options if o != correct][:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    your_desc = " + ".join(f"{c.name} ({c.cost})" for c in scenario.your_cards)
    opp_desc = " + ".join(f"{c.name} ({c.cost})" for c in scenario.opponent_cards)
    your_total = scenario.your_total
    opp_total = scenario.opponent_total
    raw_value = scenario.trade_value

    if raw_value > 0:
        fallback_reason = f"You spent {your_total} to answer {opp_total}, banking {raw_value} elixir"
    elif raw_value < 0:
        fallback_reason = f"You spent {your_total} for only {opp_total} of value, losing {abs(raw_value)} elixir"
    else:
        fallback_reason = f"You spent {your_total} to answer {opp_total} — a clean even trade"

    fallback = (
        f"{fallback_reason}. Good trade habits compound over a match — small "
        f"elixir wins are what let you open a counter-push."
    )

    question_text = (
        f"**Trade math.** {scenario.scenario_text} Your cost: {your_total}. "
        f"Opponent's cost: {opp_total}. Is this trade…"
    )

    explanation = explain_or_fallback(
        question_text=question_text,
        correct_answer=correct,
        context=(
            f"You: {your_desc} totaling {your_total}. "
            f"Opponent: {opp_desc} totaling {opp_total}. "
            f"Raw trade value: {raw_value:+d} (clamped to {display_value:+d} for the answer)."
        ),
        fallback=fallback,
    )

    return {
        "question_text": question_text,
        "image_url": None,
        "choices": choices,
        "correct_index": correct_index,
        "explanation": explanation,
        "question_type": "positive_trade",
        "card_ids": [],
    }


def generate_cycle_total_question(conn=None) -> dict | None:
    """Cost total for a 4-card rotation.

    Tactical value: knowing your deck's average cycle cost tells you
    whether you can match opponent pressure without bleeding towers.
    """
    cards = _random_playable_cards(10, conn=conn)
    if len(cards) < 4:
        return None

    rotation = random.sample(cards, 4)
    total = sum(c["elixir_cost"] for c in rotation)
    correct = str(total)

    distractors_pool = {total - 2, total - 1, total + 1, total + 2}
    distractors_pool.discard(total)
    distractors = [str(d) for d in distractors_pool if d > 0]
    random.shuffle(distractors)
    distractors = distractors[:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    rotation_desc = ", ".join(f"{c['name']} ({c['elixir_cost']})" for c in rotation)
    fallback = (
        f"{' + '.join(str(c['elixir_cost']) for c in rotation)} = {total}. "
        f"A rotation in the low-mid teens keeps you flexible; much more "
        f"and you'll lose cycle races against cheaper decks."
    )
    explanation = explain_or_fallback(
        question_text=f"Total elixir to play all four: {rotation_desc}?",
        correct_answer=f"{total} elixir",
        context=f"Cards: {rotation_desc}. Sum: {total}.",
        fallback=fallback,
    )

    return {
        "question_text": (
            f"**Rotation cost.** You play all four of these in sequence: "
            f"{rotation_desc}. What is the total elixir spent?"
        ),
        "image_url": None,
        "choices": choices,
        "correct_index": correct_index,
        "explanation": explanation,
        "question_type": "cycle_total",
        "card_ids": [c["card_id"] for c in rotation],
    }


def generate_cycle_back_question(conn=None) -> dict | None:
    """Cycle math: given a 4-card rotation A-B-C-D, how much elixir to
    cycle back to A? (Answer: B + C + D.)

    Tactical value: this is the exact math every player does before
    committing to a big win-condition push.
    """
    cards = _random_playable_cards(10, conn=conn)
    if len(cards) < 4:
        return None

    # Randomize which card is the "key" (the one you want to cycle back to)
    rotation = random.sample(cards, 4)
    key_index = random.randrange(4)
    others = [c for i, c in enumerate(rotation) if i != key_index]
    cycle_cost = sum(c["elixir_cost"] for c in others)
    correct = str(cycle_cost)

    distractors_pool = {
        cycle_cost - 2,
        cycle_cost - 1,
        cycle_cost + 1,
        cycle_cost + 2,
        sum(c["elixir_cost"] for c in rotation),  # includes the key itself — tempting wrong
    }
    distractors_pool.discard(cycle_cost)
    distractors = [str(d) for d in distractors_pool if d > 0]
    random.shuffle(distractors)
    distractors = distractors[:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    key_card = rotation[key_index]
    deck_desc = ", ".join(f"{c['name']} ({c['elixir_cost']})" for c in rotation)
    others_desc = " + ".join(str(c["elixir_cost"]) for c in others)
    fallback = (
        f"Cycling back to {key_card['name']} means playing the other three: "
        f"{others_desc} = {cycle_cost}. Knowing this lets you time your "
        f"win-condition pushes around the opponent's answers."
    )
    explanation = explain_or_fallback(
        question_text=f"Cycle cost back to {key_card['name']} in {deck_desc}?",
        correct_answer=f"{cycle_cost} elixir",
        context=(
            f"Deck rotation: {deck_desc}. Key card: {key_card['name']} "
            f"(cost {key_card['elixir_cost']}). "
            f"To cycle back, play the other three: {others_desc} = {cycle_cost}."
        ),
        fallback=fallback,
    )

    return {
        "question_text": (
            f"**Cycle math.** Your rotation is {deck_desc}. "
            f"You just played **{key_card['name']}**. How much elixir do you "
            f"need to spend to cycle back to it?"
        ),
        "image_url": None,
        "choices": choices,
        "correct_index": correct_index,
        "explanation": explanation,
        "question_type": "cycle_back",
        "card_ids": [c["card_id"] for c in rotation],
    }


# ---------------------------------------------------------------------------
# Composite generators
# ---------------------------------------------------------------------------

_GENERATORS = [
    generate_elixir_cost_question,
    generate_cost_comparison_question,
    generate_positive_trade_question,
    generate_cycle_total_question,
    generate_cycle_back_question,
]


def generate_random_question(conn=None) -> dict | None:
    """Generate one random question from any type."""
    generators = list(_GENERATORS)
    random.shuffle(generators)
    for gen in generators:
        q = gen(conn=conn)
        if q:
            return q
    return None


def generate_quiz_set(count: int, conn=None) -> list[dict]:
    """Generate a diverse set of questions, avoiding type repetition when possible."""
    questions = []
    used_types = []
    used_card_ids = set()

    for _ in range(count):
        generators = list(_GENERATORS)
        unused_types = [g for g in generators if g.__name__ not in used_types]
        if unused_types:
            random.shuffle(unused_types)
            order = unused_types + [g for g in generators if g not in unused_types]
        else:
            random.shuffle(generators)
            order = generators

        for gen in order:
            q = gen(conn=conn)
            if q:
                primary_card = q["card_ids"][0] if q["card_ids"] else None
                if primary_card and primary_card in used_card_ids and len(questions) < count:
                    q2 = gen(conn=conn)
                    if q2:
                        primary2 = q2["card_ids"][0] if q2["card_ids"] else None
                        if primary2 and primary2 not in used_card_ids:
                            q = q2
                questions.append(q)
                used_types.append(gen.__name__)
                for cid in q.get("card_ids", []):
                    used_card_ids.add(cid)
                break

    if len(questions) < count:
        log.warning("Quiz generation: requested %d questions but only generated %d", count, len(questions))
    return questions
