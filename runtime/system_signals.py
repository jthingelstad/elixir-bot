from __future__ import annotations

import logging

import db
from storage.contextual_memory import upsert_race_streak_memory

log = logging.getLogger(__name__)


STARTUP_SYSTEM_SIGNALS = [
    {
        "signal_key": "capability_anthropic_claude_migration_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: New Brain",
            "message": (
                "Elixir has migrated from OpenAI GPT to Anthropic Claude. "
                "This is a full intelligence upgrade — new models, native prompt caching, and a faster signal pipeline."
            ),
            "discord_content": (
                "**Achievement Unlocked: New Brain** :elixir:\n\n"
                "Elixir just got a brain transplant. Starting today, every conversation, every signal, "
                "and every piece of content Elixir produces is powered by **Anthropic Claude** instead of OpenAI GPT.\n\n"
                "**What changed:**\n"
                "- Chat, content, and promotion workflows now run on **Claude Sonnet** — "
                "stronger at staying in character, following instructions, and writing within guardrails.\n"
                "- Signal detection (the heartbeat that watches the clan 24/7) now runs on **Claude Haiku** — "
                "faster and more efficient for high-volume classification.\n"
                "- Native **prompt caching** means Elixir's large system prompts and 47 tool definitions "
                "are cached across calls, cutting latency and cost on every request.\n\n"
                "**What you'll notice:**\n"
                "- Elixir's voice may feel slightly different as the new models settle in. Same soul, new neurons.\n"
                "- Responses should be more consistent with Elixir's personality across channels.\n"
                "- Structured answers (war status, member lookups, roster data) should have fewer formatting hiccups.\n\n"
                "This is **Elixir v4.0 \"New Brain\"** — same Elixir, sharper mind."
            ),
            "details": [
                "Full migration from OpenAI GPT to Anthropic Claude models.",
                "Claude Sonnet powers chat, content, and promotion workflows.",
                "Claude Haiku powers observation and signal detection for speed and efficiency.",
                "Native prompt caching on system prompts and tool definitions reduces latency and cost.",
                "Tool definitions and message handling rewritten for Anthropic's native API format.",
                "Telemetry system generalized to provider-neutral naming.",
            ],
            "audience": "clan",
            "capability_area": "intelligence_migration",
        },
    },
    {
        "signal_key": "capability_deck_review_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Deck Review",
            "message": (
                "Elixir now has a dedicated deck-review workflow that grounds advice in your actual battle history "
                "instead of generic meta talk. It handles regular Trophy Road decks, your four river-race war decks "
                "(reconstructed from battle data since the Clash Royale API does not expose them), and a build-from-scratch "
                "mode for clan members who want a starter kit."
            ),
            "discord_content": (
                "**Achievement Unlocked: Deck Review** :elixir:\n\n"
                "Asking Elixir for deck help just got a lot more personal. "
                "Instead of generic meta talk, advice is now grounded in **your own battle history** — "
                "the cards that have actually been beating you, the cards you actually own at competitive level, "
                "and the four war decks Elixir reconstructs from your river-race battles.\n\n"
                "**In #ask-elixir:**\n"
                "- `review my deck` — Elixir cites the specific cards in your recent losses "
                "(e.g. \"Mega Knight has been in 6 of your last 9 losses\") and proposes swaps you can actually run.\n"
                "- `review my war decks` — reconstructs your four war decks from battle history "
                "(the Clash Royale API does not show them directly), then reviews each one with the "
                "no-overlap rule enforced on every swap suggestion.\n"
                "- `build me a deck` — proposes a deck built entirely from cards you own, with reasoning per slot.\n"
                "- `build my war decks` — builds all **four** war decks for you (32 unique cards, no overlaps), "
                "with distinct roles per deck.\n\n"
                "**For clan members new to war:**\n"
                "- If you have not played river race yet and ask Elixir to review your war decks, "
                "you will get a warm offer instead of a brick wall: Elixir will offer to build you four starter "
                "decks from your collection so you can stop staring at the deck-builder screen.\n"
                "- Building four non-overlapping decks is the most common blocker keeping members out of war. "
                "Elixir can hand you a kit. Reply `build my war decks` and you are in.\n\n"
                "This is **Elixir v4.3 \"Deck Review\"** — your decks, your data, your call."
            ),
            "details": [
                "Deck advice is now grounded in real opponent cards from your recent losses, not generic meta knowledge.",
                "War deck reconstruction infers your four river-race decks from battle history since the Clash Royale API does not expose them.",
                "Build-from-scratch mode can propose a single deck or all four war decks (32 unique cards) from your collection.",
                "New-war-player flow: asking to review war decks with no war activity triggers an offer to build you a starter kit.",
                "Every swap suggestion is validated against the no-overlap rule and your card collection, so Elixir never recommends a card you do not own.",
            ],
            "audience": "clan",
            "capability_area": "deck_review",
        },
    },
    {
        "signal_key": "capability_race_command_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Race Command",
            "message": (
                "Elixir's River Race coverage just got a major upgrade. "
                "I now track the full competitive field — not just POAP KINGS — and carry the weight of our unbroken 1st-place streak in how I talk about the race."
            ),
            "discord_content": (
                "**Achievement Unlocked: Race Command** :elixir:\n\n"
                "Elixir's River Race coverage just leveled up. "
                "I'm not just watching POAP KINGS anymore — I'm watching the entire field.\n\n"
                "**What's new in #river-race:**\n"
                "- I now call out competing clans by name. "
                "Who's closest, who's making a move, and who's barely showing up — it all makes the updates now.\n"
                "- The fame gap sets the tone. "
                "A 200-fame lead feels different than a 5,000-fame lead, and the posts will reflect that.\n"
                "- Day transitions are now one clean message instead of two back-to-back posts. "
                "When a battle day ends and a new one starts, you get one cohesive update covering both.\n\n"
                "**The streak:**\n"
                "- POAP KINGS has finished 1st in every single river race since our very first one. "
                "That streak is now part of how I frame the race — "
                "not as a stat I announce every post, but as the floor I stand on.\n\n"
                "**Under the hood:**\n"
                "- Elixir's tool layer was consolidated from 51 tools into domain-aligned tools — "
                "cleaner, faster, and easier to reason about.\n\n"
                "This is **Elixir v4.2 \"Race Command\"** — sharper eyes on the river."
            ),
            "details": [
                "River Race updates now reference competing clans by name with fame-gap framing.",
                "Day transition signals (end of day + start of day) are merged into one cohesive post.",
                "The clan's unbroken 1st-place streak is now tracked as a durable identity memory.",
                "LLM tool layer consolidated from 51 into domain-aligned tools.",
            ],
            "audience": "clan",
            "capability_area": "race_command",
        },
    },
    {
        "signal_key": "capability_omnipresent_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Omnipresent",
            "message": (
                "Elixir's horizon just expanded from our clan to every clan, every player, and every tournament on the live Clash Royale API. "
                "Drop a tag in chat and I can scout it — roster, river race standing, recent battles, opponent decks, and threat level."
            ),
            "discord_content": (
                "**Achievement Unlocked: Omnipresent** :elixir:\n\n"
                "I used to only see our clan. Now I see every arena.\n\n"
                "**Ask me about any tag:**\n"
                "- `how strong is clan #QVJJL829` — full clan profile, trophy average, donations, top members.\n"
                "- `scout #P8JVG92U and show me their recent battles` — player profile plus the decks they have been running, with each card named and costed.\n"
                "- `what is clan #XYZ's current river race standing` — live fame, participants, war day state.\n"
                "- `pull up top members of #XYZ` — ranked roster with roles, trophies, and last-seen.\n\n"
                "**Chaining works:**\n"
                "- When you ask about a clanmate's recent losses, I can now chain straight into scouting the opponents who beat them — "
                "their deck, their level, their clan. The data was always close, but the bridge between \"who beat me\" and \"who are they\" was missing. It is there now.\n\n"
                "**Clan Wars Intel Report:**\n"
                "- The scheduled intel report in #river-race is fully rewired. Same threat ratings and roster analysis, but now driven by the same tool plumbing I use for conversational scouting — one brain, two surfaces.\n\n"
                "This is **Elixir v4.4 \"Omnipresent\"** — wherever the tag is, I can be there too."
            ),
            "details": [
                "New unified cr_api tool reaches any player, clan, or tournament on the live Clash Royale API by tag.",
                "Aspect chaining lets Elixir scout opponents end-to-end: player profile → recent battles → opponent decks named and costed.",
                "Local tools now expose player and opponent tags so conversational follow-ups can chain into external scouting without re-pasting the tag.",
                "Clan Wars Intel Report is rewired onto the normal LLM+tool plumbing — same threat analysis, one consistent code path.",
                "Guardrails: per-turn cap of 5 external lookups, in-module TTL cache, and external lookups excluded from low-context workflows.",
            ],
            "audience": "clan",
            "capability_area": "omnipresent",
        },
    },
    {
        "signal_key": "capability_coherent_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Coherent",
            "message": (
                "Elixir's proactive posting flipped from one LLM call per signal to one agent turn per heartbeat. "
                "I now see the full situation each tick — what's happened, where in the war week we are, what each channel has heard from me — and I decide what (if anything) is worth saying."
            ),
            "discord_content": (
                "**Achievement Unlocked: Coherent** :elixir:\n\n"
                "I used to react to one signal at a time. Now I see the whole tick at once and decide what's worth saying.\n\n"
                "**What's different:**\n"
                "- **I investigate before I post.** When someone is on a streak, I pull their recent battles to see who they were beating before I write the callout. The post leads with what was actually faced, not just the streak count.\n"
                "- **I collapse related signals.** When a battle day ends, the week rolls over, and the next practice phase starts in the same tick, that's now one post that recaps and pivots — not five posts racing each other into the channel.\n"
                "- **I'm allowed to be silent.** When the data has already gone stale (a hot-streak signal whose live battle log shows the streak broke), I skip and log why. Silence is a real choice now.\n"
                "- **I always know what time it is.** Hours-remaining, day index, war phase, and colosseum status attach to every post — no waiting for a checkpoint to fire to talk about the clock.\n\n"
                "**Member highlights are consolidated**\n"
                "- Volatile non-war battle activity and durable progression now land together in **#player-highlights**.\n"
                "- I still distinguish a live push from a permanent milestone; the difference is in the framing, not another channel to watch.\n\n"
                "This is **Elixir v4.5 \"Coherent\"** — one mind per tick, watching the whole situation, deciding what's worth your attention."
            ),
            "details": [
                "New per-tick awareness loop replaces per-signal LLM calls — one agent turn sees all signals together and emits a structured post plan.",
                "Agent investigates before posting via cr_api (streak opponents, rival clans) so posts cite specific evidence instead of restating signal dicts.",
                "Coherent timing: related signals (war cascade, mixed milestone+roster batches) collapse into one sequenced post per channel instead of N independent posts.",
                "Genuine silence is allowed — stale signals get caught and skipped with a logged reason; quiet ticks fast-path skip the LLM call entirely.",
                "Hard-post-floor fallback guarantees coverage for member_join, war_battle_rank_change, capability_unlock, and week/season completion signals.",
                "#player-highlights consolidates volatile battle-mode activity and durable player milestones with different framing rules.",
                "Time/phase/standing context now attaches to every channel post, not just war checkpoints.",
            ],
            "audience": "clan",
            "capability_area": "coherent",
        },
    },
    {
        "signal_key": "capability_clan_keep_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Clan Keep",
            "message": (
                "Elixir can now act on what it observes, schedule its own follow-ups, "
                "and synthesize clan memory at the end of each week. The awareness loop "
                "gained write tools, a revisit scheduler, and a weekly memory-synthesis job."
            ),
            "discord_content": (
                "**Achievement Unlocked: Clan Keep** :elixir:\n\n"
                "I used to observe and report. Now I can remember, flag, and follow up on my own.\n\n"
                "**What's new:**\n"
                "- **I keep watch.** When I notice a member going quiet, sliding in trophies, or missing war days, "
                "I can flag them for leadership — a durable note that persists across ticks so the pattern doesn't get lost in the scroll.\n"
                "- **I queue follow-ups.** When the data suggests a leadership action (promotion review, kick conversation, "
                "deck check), I write a concrete recommendation to #leaders instead of hoping someone notices.\n"
                "- **I schedule revisits.** Mid-arc situations — a win streak during battle day, a silent member before Friday — "
                "I can tell my future self to check back. The revisit surfaces in a later tick and I decide then whether to act.\n"
                "- **I synthesize the week.** Every Sunday night I read back through the week's signals, posts, and memories, "
                "write canonical arc memories (\"Week 5 colosseum: the Gareth push\"), retire entries that no longer match "
                "reality, and flag contradictions for leadership.\n\n"
                "All writes are leadership-scoped and budget-capped at 3 per tick. The weekly synthesis digest lands in "
                "#leaders.\n\n"
                "This is **Elixir v4.6 \"Clan Keep\"** — the keep at the center of the castle, holding what matters."
            ),
            "details": [
                "Awareness write surface: save_clan_memory, flag_member_watch, record_leadership_followup — all leadership-scoped, capped at 3 per tick.",
                "Self-scheduled revisits: schedule_revisit(signal_key, at, rationale) persists to a revisits table and surfaces in future Situations.",
                "Weekly memory synthesis: Sunday job writes elixir_synthesis arc memories, expires stale entries, flags contradictions, posts digest to #leaders.",
                "Emoji fix: agent prompts enumerate real custom emoji; hallucinated shortcodes stripped before posting.",
                "Feature flag cleanup: ELIXIR_AWARENESS_LOOP retired; awareness loop is the only path.",
                "Signal dedup fix: arena_change and war_rollover signals no longer re-fire across ticks.",
            ],
            "audience": "clan",
            "capability_area": "clan_keep",
        },
    },
    {
        "signal_key": "capability_trophy_hall_v1",
        "signal_type": "capability_unlock",
        "payload": {
            "title": "Achievement Unlocked: Trophy Hall",
            "message": (
                "Awards are now first-class. War Champ, Iron King, Donation Champ, and four more are persisted per season, "
                "announced as they land, and surfaced on every member's POAP KINGS profile."
            ),
            "discord_content": (
                "**Achievement Unlocked: Trophy Hall**\n\n"
                "Until now, War Champ and friends lived only in the moment — recomputed at announcement time, remembered only by me. "
                "That's over. Every award is now a row in the book, a line on your profile, and a callout the moment you earn it.\n\n"
                "**Seven awards, two scopes:**\n"
                "- **War Champ** — top-3 fame for the season (gold / silver / bronze).\n"
                "- **Iron King** — 4/4 decks every battle day of every battle week. No misses.\n"
                "- **Donation Champ** — top-3 total donations for the season.\n"
                "- **Donation Champ Weekly** — top-3 each CR week, carried over from the Monday recap.\n"
                "- **War Participant** — fire off fame in any race and it lands on your card.\n"
                "- **Perfect Week** — 4/4 decks every battle day of one week. Earnable four times a season.\n"
                "- **Rookie MVP** — top-3 fame among members who joined mid-season.\n\n"
                "**What's new on poapkings.com:**\n"
                "- Every member card now shows a **trophy case** — inline, newest first.\n"
                "- A new **`/members/trophy/<season>`** page collects every award from every season, with tabs.\n\n"
                "**In Discord:** each grant fires a fresh `award_earned` signal into #clan-events. Live, durable, and something I'll remember forever.\n\n"
                "This is **Elixir v4.8 \"Trophy Hall\"** — earn it once, it's yours for good."
            ),
            "details": [
                "New awards table (migration 30) with UNIQUE(award_type, season_id, section_index, member_id) for idempotent grants.",
                "Seven award types across season and weekly scopes; podium awards store rank 1/2/3, pass/fail awards always rank 1.",
                "Iron King / Perfect Week use war_participant_snapshots; Donation Champ sums weekly MAX across the season; Rookie MVP filters by clan_memberships.joined_at.",
                "Season close detected by the presence of a newer season_id in war_races — grants back-fill automatically with no timing-sensitive trigger.",
                "award_earned signal routed to #clan-events and stored as a public clan_memories row so the trophy history is durable across conversations.",
                "POAP KINGS site: per-member trophy_case on roster/members + new elixirAwards.json for the /members/trophy page.",
            ],
            "audience": "clan",
            "capability_area": "trophy_hall",
        },
    },
]


def _seed_race_streak_memory(*, conn=None) -> None:
    """Seed the race win streak identity memory on first deploy."""
    from memory_store import list_memories

    existing = list_memories(
        viewer_scope="system_internal",
        include_system_internal=True,
        filters={"event_type": "clan_identity", "event_id": "race_win_streak"},
        limit=1,
        conn=conn,
    )
    if existing:
        return  # Already seeded
    try:
        upsert_race_streak_memory(season_id=0, week=0, race_rank=1, conn=conn)
        log.info("Seeded race win streak identity memory")
    except Exception:
        log.warning("Failed to seed race streak memory", exc_info=True)


def queue_startup_system_signals(*, conn=None) -> None:
    for signal in STARTUP_SYSTEM_SIGNALS:
        db.queue_system_signal(
            signal["signal_key"],
            signal["signal_type"],
            signal["payload"],
            conn=conn,
        )
    _seed_race_streak_memory(conn=conn)


__all__ = [
    "STARTUP_SYSTEM_SIGNALS",
    "queue_startup_system_signals",
]
