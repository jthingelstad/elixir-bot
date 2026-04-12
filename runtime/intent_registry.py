"""Single source of truth for routable user intents.

Both the LLM intent router (`agent.intent_router`) and the dynamic help report
(`runtime.helpers._reports._build_help_report`) draw from this registry, so a new
capability is added in exactly one place.

A route entry has:
    key                Stable identifier used by the router and dispatch table.
    label              Short human-readable name.
    router_description Sentence(s) the router LLM uses to pick this route.
    help_summary       Sentence(s) shown to users in the help report.
    examples           Phrasings the router should treat as canonical for this route.
    workflows          Which channel workflows the route is available in.
                       Use {"interactive", "clanops"} for both.
    requires_mention   True if the route should only fire when the bot is mentioned
                       (or the channel allows open replies). Routes that read or
                       write state generally require this.
    mode_choices       Optional sub-mode values the router may attach (e.g. for decks).
"""

from __future__ import annotations

from typing import Iterable


ROUTES: list[dict] = [
    {
        "key": "help",
        "label": "Help / capabilities",
        "router_description": (
            "User is asking what Elixir can do, how to use it, what commands "
            "exist, or for general guidance about the bot's capabilities. "
            "Examples include 'help', 'what can you do', 'how can you help me', "
            "'what do you help with', 'help me out'."
        ),
        "help_summary": "Show what I can do and how to ask.",
        "examples": [
            "help",
            "what can you do",
            "how can you help me?",
            "what do you help with",
            "can you help me",
            "help me out",
            "how do you help",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": False,
    },
    {
        "key": "deck_display",
        "label": "Show a member's current deck(s)",
        "router_description": (
            "User wants to see the cards in a deck — no advice, just the "
            "list. Use mode='regular' for the current ladder/trophy-road "
            "deck (default). Use mode='war' when the user asks for their "
            "war decks, river-race decks, or wants to see all four war "
            "decks from the battle log."
        ),
        "help_summary": "Show the cards in your current deck or your four war decks.",
        "examples": [
            "what cards are in my deck",
            "show me my deck",
            "what's in jamie's deck",
            "show jamie's deck",
            "current deck",
            "show me my war decks",
            "pull up my four war decks from the battle log",
            "what are my river race decks",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": False,
        "mode_choices": ["regular", "war"],
    },
    {
        "key": "deck_review",
        "label": "Review / critique an existing deck",
        "router_description": (
            "User wants feedback on a deck they already have — critique, "
            "improvements, swaps, or tuning. Use mode='war' if the request "
            "mentions war decks, river race decks, or clan war decks; otherwise "
            "mode='regular'."
        ),
        "help_summary": "Critique a deck and suggest swaps or improvements.",
        "examples": [
            "review my deck",
            "review my war decks",
            "what should I swap in my deck",
            "any tips on my war decks",
            "how do I improve my deck",
            "tune my war decks",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": True,
        "mode_choices": ["regular", "war"],
    },
    {
        "key": "deck_suggest",
        "label": "Build a new deck from scratch",
        "router_description": (
            "User wants Elixir to build, recommend, or suggest a brand new "
            "deck (or set of war decks) from their collection — they don't "
            "have one yet, or want a fresh start. Use mode='war' for war / "
            "river race / clan war decks; otherwise mode='regular'. Quantifiers "
            "like 'four new war decks' or 'two regular decks' belong here."
        ),
        "help_summary": "Build new decks for you from your card collection.",
        "examples": [
            "build me a deck",
            "suggest a new deck",
            "recommend four new war decks for me",
            "build my war decks",
            "make me four war decks",
            "what deck should I play",
            "help me start playing war",
            "I don't know what deck to use",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": True,
        "mode_choices": ["regular", "war"],
    },
    {
        "key": "kick_risk",
        "label": "Kick-risk report",
        "router_description": (
            "User (a clan operator) wants the kick-risk report listing members "
            "who are at risk of being removed from the clan."
        ),
        "help_summary": "Show members at risk of being kicked.",
        "examples": [
            "who is at risk of being kicked",
            "kick risk report",
            "show kick candidates",
            "who should we kick",
        ],
        "workflows": {"clanops"},
        "requires_mention": False,
    },
    {
        "key": "top_war_contributors",
        "label": "Top war contributors this season",
        "router_description": (
            "User wants the leaderboard of top contributors to clan wars for "
            "the current season."
        ),
        "help_summary": "List the top war contributors this season.",
        "examples": [
            "who are the top war contributors this season",
            "top war contributors this season",
            "who are the top 5 contributors to clan wars this season?",
        ],
        "workflows": {"clanops"},
        "requires_mention": False,
    },
    {
        "key": "roster_join_dates",
        "label": "Roster join dates",
        "router_description": (
            "User wants the roster sorted by when each member joined the clan."
        ),
        "help_summary": "Show the roster ordered by join date.",
        "examples": [
            "show join dates",
            "when did everyone join",
            "roster by join date",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": False,
    },
    {
        "key": "clan_status",
        "label": "Full clan status report",
        "router_description": (
            "User wants the full clan status snapshot — roster, war state, "
            "promotions, at-risk members, season summary. Pick this over "
            "status_report when the user explicitly says 'clan status'."
        ),
        "help_summary": "Show the full clan status snapshot.",
        "examples": [
            "clan status",
            "full clan status",
            "give me the clan status",
        ],
        "workflows": {"clanops"},
        "requires_mention": False,
        "mode_choices": ["full", "short"],
    },
    {
        "key": "status_report",
        "label": "System / bot status",
        "router_description": (
            "User wants the bot's own runtime status — uptime, recent errors, "
            "data freshness. Not the clan's status."
        ),
        "help_summary": "Show Elixir's runtime status (uptime, errors, freshness).",
        "examples": [
            "status",
            "system status",
            "are you healthy",
            "elixir status",
        ],
        "workflows": {"clanops"},
        "requires_mention": False,
    },
    {
        "key": "schedule_report",
        "label": "Scheduled jobs report",
        "router_description": (
            "User wants to see the scheduled jobs / cron / recurring tasks the "
            "bot is configured to run."
        ),
        "help_summary": "Show what's on the schedule and when it runs next.",
        "examples": [
            "schedule",
            "what's on the schedule",
            "show me the cron",
            "scheduled jobs",
        ],
        "workflows": {"clanops"},
        "requires_mention": False,
    },
    {
        "key": "llm_chat",
        "label": "Open-ended chat / Q&A",
        "router_description": (
            "Anything else that isn't covered by a specific route — open-ended "
            "questions about clan members, war participation, donations, recent "
            "form, trophies, league/arena, conversational follow-ups, or any "
            "request that needs the full LLM chat workflow with tools. This is "
            "the default when nothing else fits."
        ),
        "help_summary": (
            "Open Q&A on members, war, donations, trophies, recent form, and more."
        ),
        "examples": [
            "what's jamie's war participation rate",
            "who has the most donations this week",
            "what arena is foo in",
            "tell me about jamie",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": True,
    },
    {
        "key": "not_for_bot",
        "label": "Not addressed to the bot",
        "router_description": (
            "Message is conversation between humans, not directed at Elixir. "
            "Pick this when there's no question, no command, no mention, and "
            "nothing the bot should respond to."
        ),
        "help_summary": "",  # not shown in help
        "examples": [
            "lol",
            "thanks!",
            "yeah I agree",
            "see you tomorrow",
        ],
        "workflows": {"interactive", "clanops"},
        "requires_mention": False,
    },
]


ROUTE_KEYS: list[str] = [r["key"] for r in ROUTES]


def get_route(key: str) -> dict | None:
    for route in ROUTES:
        if route["key"] == key:
            return route
    return None


def routes_for_workflow(workflow: str) -> list[dict]:
    return [r for r in ROUTES if workflow in r["workflows"]]


def help_routes_for_workflow(workflow: str) -> list[dict]:
    """Routes worth showing in a user-facing help report for this workflow."""
    excluded = {"not_for_bot", "llm_chat"}
    return [
        r for r in ROUTES
        if workflow in r["workflows"] and r["key"] not in excluded and r.get("help_summary")
    ]


def router_route_summaries(workflows: Iterable[str] | None = None) -> str:
    """Render the route table as markdown for the intent-router system prompt."""
    workflows = set(workflows) if workflows else None
    lines = []
    for r in ROUTES:
        if workflows and not (r["workflows"] & workflows):
            continue
        modes = r.get("mode_choices")
        mode_note = f" (modes: {', '.join(modes)})" if modes else ""
        mention_note = " — requires bot to be mentioned" if r.get("requires_mention") else ""
        examples = "; ".join(f'"{e}"' for e in r["examples"][:4])
        lines.append(
            f"- **{r['key']}** — {r['label']}{mode_note}{mention_note}\n"
            f"  {r['router_description']}\n"
            f"  Examples: {examples}"
        )
    return "\n".join(lines)
