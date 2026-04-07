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
"""

import random

from storage.card_catalog import get_random_cards, lookup_cards

# Tower troops are not playable battle cards — exclude from all quiz questions.
_PLAYABLE_TYPES = ("troop", "building", "spell")


def _random_playable_cards(count: int, *, rarity=None, exclude_ids=None, conn=None) -> list[dict]:
    """Get random cards excluding tower troops."""
    # Fetch extra to account for possible tower troop filtering
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


def _rarity_display(rarity: str) -> str:
    return (rarity or "unknown").capitalize()


def _type_display(card_type: str) -> str:
    return {
        "troop": "Troop",
        "building": "Building",
        "spell": "Spell",
        "tower_troop": "Tower Troop",
    }.get(card_type, card_type.replace("_", " ").title())


def _mode_display(mode_label: str | None) -> str:
    return mode_label or "Neither"


# ---------------------------------------------------------------------------
# Question generators
# ---------------------------------------------------------------------------

def generate_elixir_cost_question(conn=None) -> dict | None:
    """What is the elixir cost of this card? (show image)"""
    cards = _random_playable_cards(5, conn=conn)
    if not cards:
        return None

    card = cards[0]
    correct = str(card["elixir_cost"])

    # Build plausible distractors from nearby costs
    cost = card["elixir_cost"]
    possible = [str(c) for c in range(max(1, cost - 2), cost + 3) if c != cost and 1 <= c <= 10]
    random.shuffle(possible)
    distractors = possible[:3]
    # Pad if needed
    while len(distractors) < 3:
        distractors.append(str(random.choice([c for c in range(1, 11) if str(c) != correct and str(c) not in distractors])))

    choices, correct_index = _shuffle_choices(correct, distractors)

    return {
        "question_text": f"What is the elixir cost of **{card['name']}**?",
        "image_url": card.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{card['name']} costs {cost} elixir. It's a {_rarity_display(card['rarity'])} {_type_display(card['card_type'])}.",
        "question_type": "elixir_cost",
        "card_ids": [card["card_id"]],
    }


def generate_cost_comparison_question(conn=None) -> dict | None:
    """Which of these cards costs the most (or least) elixir?"""
    cards = _random_playable_cards(20, conn=conn)
    if len(cards) < 4:
        return None

    # Pick 4 cards with different costs when possible
    by_cost = {}
    for c in cards:
        by_cost.setdefault(c["elixir_cost"], []).append(c)

    selected = []
    costs_used = set()
    for cost in sorted(by_cost.keys()):
        if len(selected) >= 4:
            break
        card = random.choice(by_cost[cost])
        selected.append(card)
        costs_used.add(cost)

    # Fill remaining from leftovers if needed
    if len(selected) < 4:
        remaining = [c for c in cards if c["card_id"] not in {s["card_id"] for s in selected}]
        random.shuffle(remaining)
        selected.extend(remaining[: 4 - len(selected)])

    if len(selected) < 4:
        return None

    selected = selected[:4]

    # Decide most or least
    ask_most = random.choice([True, False])
    if ask_most:
        target = max(selected, key=lambda c: c["elixir_cost"])
        word = "most"
    else:
        target = min(selected, key=lambda c: c["elixir_cost"])
        word = "least"

    choices = [c["name"] for c in selected]
    correct_name = target["name"]
    random.shuffle(choices)
    correct_index = choices.index(correct_name)

    return {
        "question_text": f"Which of these cards costs the **{word}** elixir?",
        "image_url": None,
        "result_image_url": target.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{target['name']} costs {target['elixir_cost']} elixir — the {word} of these four.",
        "question_type": "cost_comparison",
        "card_ids": [c["card_id"] for c in selected],
    }


def generate_rarity_question(conn=None) -> dict | None:
    """What rarity is this card? (show image)"""
    cards = _random_playable_cards(1, conn=conn)
    if not cards:
        return None

    card = cards[0]
    correct = _rarity_display(card["rarity"])

    all_rarities = ["Common", "Rare", "Epic", "Legendary", "Champion"]
    distractors = [r for r in all_rarities if r != correct]
    random.shuffle(distractors)
    distractors = distractors[:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    return {
        "question_text": f"What rarity is **{card['name']}**?",
        "image_url": card.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{card['name']} is a {correct} {_type_display(card['card_type'])}.",
        "question_type": "rarity",
        "card_ids": [card["card_id"]],
    }


def generate_card_type_question(conn=None) -> dict | None:
    """Is this card a troop, spell, or building? (show image)"""
    cards = _random_playable_cards(5, conn=conn)
    if not cards:
        return None

    card = cards[0]
    correct = _type_display(card["card_type"])

    all_types = ["Troop", "Building", "Spell"]
    distractors = [t for t in all_types if t != correct]
    random.shuffle(distractors)
    distractors = distractors[:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    return {
        "question_text": f"What type of card is **{card['name']}**?",
        "image_url": card.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{card['name']} is a {_rarity_display(card['rarity'])} {correct}.",
        "question_type": "card_type",
        "card_ids": [card["card_id"]],
    }


def generate_evolution_question(conn=None) -> dict | None:
    """Does this card support Evo, Hero, both, or neither?"""
    cards = _random_playable_cards(5, conn=conn)
    if not cards:
        return None

    card = cards[0]
    correct = _mode_display(card.get("mode_label"))

    all_modes = ["Evo", "Hero", "Evo + Hero", "Neither"]
    distractors = [m for m in all_modes if m != correct]
    random.shuffle(distractors)
    distractors = distractors[:3]

    choices, correct_index = _shuffle_choices(correct, distractors)

    return {
        "question_text": f"Does **{card['name']}** support Evo, Hero, both, or neither?",
        "image_url": card.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{card['name']} supports: {correct}." if correct != "Neither" else f"{card['name']} does not have Evo or Hero capability.",
        "question_type": "evolution_mode",
        "card_ids": [card["card_id"]],
    }


def generate_champion_identification_question(conn=None) -> dict | None:
    """Which of these is a Champion?"""
    champions = lookup_cards(rarity="champion", limit=10, conn=conn)
    non_champions = _random_playable_cards(10, conn=conn)
    non_champions = [c for c in non_champions if c["rarity"] != "champion"]

    if not champions or len(non_champions) < 3:
        return None

    champion = random.choice(champions)
    random.shuffle(non_champions)
    others = non_champions[:3]

    choices = [champion["name"]] + [c["name"] for c in others]
    random.shuffle(choices)
    correct_index = choices.index(champion["name"])

    return {
        "question_text": "Which of these cards is a **Champion**?",
        "image_url": None,
        "result_image_url": champion.get("icon_url"),
        "choices": choices,
        "correct_index": correct_index,
        "explanation": f"{champion['name']} is a Champion — Champions are the rarest cards and have unique abilities that activate during battle.",
        "question_type": "champion_id",
        "card_ids": [champion["card_id"]] + [c["card_id"] for c in others],
    }


# ---------------------------------------------------------------------------
# Composite generators
# ---------------------------------------------------------------------------

_GENERATORS = [
    generate_elixir_cost_question,
    generate_cost_comparison_question,
    generate_rarity_question,
    generate_card_type_question,
    generate_evolution_question,
    generate_champion_identification_question,
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
        # Prefer question types not yet used
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
                # Mild dedup: skip if the exact same primary card was already used
                primary_card = q["card_ids"][0] if q["card_ids"] else None
                if primary_card and primary_card in used_card_ids and len(questions) < count:
                    # Try once more with the same generator
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

    return questions
