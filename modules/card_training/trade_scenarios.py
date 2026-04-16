"""Curated positive/neutral/negative trade scenarios for the quiz module.

Each scenario is a real, canonical Clash Royale situation. Trade value is
computed deterministically from cost sums: ``opponent_total - your_total``.
A positive number means you won elixir; negative means you overspent.

The quiz generator picks one scenario at random, resolves card names to the
live catalog (so stat references match what's in-game), and builds the
multiple-choice question from the math. The scenario text is presented
verbatim — no LLM involved in scenario selection or math.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeCard:
    name: str
    cost: int


@dataclass(frozen=True)
class TradeScenario:
    your_cards: tuple[TradeCard, ...]
    opponent_cards: tuple[TradeCard, ...]
    scenario_text: str

    @property
    def your_total(self) -> int:
        return sum(c.cost for c in self.your_cards)

    @property
    def opponent_total(self) -> int:
        return sum(c.cost for c in self.opponent_cards)

    @property
    def trade_value(self) -> int:
        """Positive = you gained elixir, negative = you overspent, 0 = even."""
        return self.opponent_total - self.your_total


def _scenario(your: list[tuple[str, int]], opp: list[tuple[str, int]], text: str) -> TradeScenario:
    return TradeScenario(
        your_cards=tuple(TradeCard(name=n, cost=c) for n, c in your),
        opponent_cards=tuple(TradeCard(name=n, cost=c) for n, c in opp),
        scenario_text=text,
    )


# Seed list. Every entry has unambiguous math.
TRADE_SCENARIOS: tuple[TradeScenario, ...] = (
    # --- Fireball value (4 elixir spell hitting 5+ elixir targets) ---
    _scenario(
        [("Fireball", 4)], [("Musketeer", 4), ("Ice Spirit", 1)],
        "You Fireball a Musketeer and an Ice Spirit that dropped together.",
    ),
    _scenario(
        [("Fireball", 4)], [("Wizard", 5)],
        "You Fireball a lone Wizard behind the tank.",
    ),
    _scenario(
        [("Fireball", 4)], [("Witch", 5)],
        "You Fireball a Witch before she places her skeletons.",
    ),
    _scenario(
        [("Fireball", 4)], [("Three Musketeers", 9)],
        "You Fireball a split Three Musketeers and take out the two on one lane.",
    ),

    # --- Small-spell value (2-3 elixir) ---
    _scenario(
        [("Zap", 2)], [("Minions", 3)],
        "You Zap three Minions crossing the bridge.",
    ),
    _scenario(
        [("Zap", 2)], [("Goblin Gang", 3)],
        "You Zap a fresh Goblin Gang before they reach the tower.",
    ),
    _scenario(
        [("The Log", 2)], [("Goblin Gang", 3)],
        "You Log a Goblin Gang as it leaves the bridge.",
    ),
    _scenario(
        [("The Log", 2)], [("Princess", 3)],
        "You Log a Princess the moment she hits the board.",
    ),
    _scenario(
        [("Arrows", 3)], [("Minion Horde", 5)],
        "You Arrows a Minion Horde before it lands on your tower.",
    ),
    _scenario(
        [("Arrows", 3)], [("Skeleton Army", 3)],
        "You Arrows a Skeleton Army swarming your tank.",
    ),
    _scenario(
        [("Barbarian Barrel", 2)], [("Goblin Gang", 3)],
        "You Barbarian Barrel a Goblin Gang pushing your tower.",
    ),

    # --- Big-spell value (5-6 elixir hitting 8+ elixir stacks) ---
    _scenario(
        [("Rocket", 6)], [("Three Musketeers", 9)],
        "You Rocket three Musketeers clumped behind the tank.",
    ),
    _scenario(
        [("Rocket", 6)], [("Wizard", 5), ("Musketeer", 4)],
        "You Rocket a stacked Wizard + Musketeer combo.",
    ),
    _scenario(
        [("Lightning", 6)], [("Witch", 5), ("Musketeer", 4)],
        "You Lightning a Witch and Musketeer pushing together.",
    ),
    _scenario(
        [("Poison", 4)], [("Witch", 5)],
        "You Poison a Witch locked onto your tower.",
    ),
    _scenario(
        [("Earthquake", 3)], [("Inferno Tower", 5)],
        "You Earthquake an Inferno Tower to save your tank.",
    ),

    # --- Even trades (teach: not every spell needs to win elixir) ---
    _scenario(
        [("Fireball", 4)], [("Musketeer", 4)],
        "You Fireball a lone Musketeer to clear the counter.",
    ),

    # --- Negative trades (teach: when NOT to cast) ---
    _scenario(
        [("Fireball", 4)], [("Goblins", 2)],
        "You Fireball three Goblins at the bridge.",
    ),
    _scenario(
        [("Rocket", 6)], [("Princess", 3)],
        "You Rocket a Princess on the bridge.",
    ),
    _scenario(
        [("Poison", 4)], [("Princess", 3)],
        "You Poison a Princess as she places.",
    ),
)


__all__ = ["TRADE_SCENARIOS", "TradeCard", "TradeScenario"]
