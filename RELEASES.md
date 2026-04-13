# Elixir Releases

This file tracks shipped features and capabilities in reverse chronological order.

---

## v4.4 — Omnipresent

**Date:** 2026-04-13

Elixir's horizon expanded from "our clan" to "any clan, any player, any tournament on the live Clash Royale API." A single unified `cr_api` tool bridges the LLM to external lookups by tag, existing local tools now expose the tags the LLM needs to chain into scouting, and the scheduled Clan Wars Intel Report was rewired through the normal LLM+tool plumbing instead of bespoke orchestration.

### Unified `cr_api` Tool

- New LLM tool with 8 aspects: `player`, `player_battles`, `player_chests`, `clan`, `clan_members`, `clan_war`, `clan_war_log`, `tournament`.
- Ask about any tag — "how strong is clan #QVJJL829", "scout player #P8JVG92U and show me their recent battles", "pull up top members of #G22GQVQR" — and Elixir fetches the answer live.
- Aspect chaining works: `player` → `player_battles` → `lookup_cards` produces a full scouting report with opponent decks identified by name and elixir cost.
- Strict tag validation (`_normalize_cr_tag`) rejects malformed tags with a clean envelope error instead of a 404 from the API.
- Our-clan tags on clan aspects are rejected with a pointer to the richer local tools (`get_clan_health`, `get_clan_roster`).

### Tag Exposure (LLM Chaining)

- `get_member_recent_form` now emits `player_tag` so follow-up scouts can chain.
- `get_member_war_status` now emits `player_tag`.
- `get_member_recent_losses` now emits an `opponent_tags` aggregate so "who's been beating me" can chain into `cr_api(aspect='player')` to scout the opponent.
- Before: the LLM knew *who* beat you but couldn't look them up. Now it can.

### Clan Wars Intel Report — LLM-Driven

- The scheduled Intel Report job (`#river-race`) no longer runs hardcoded orchestration. The LLM drives the fan-out across the four competing clans using `cr_api` and a new `get_clan_intel_report` tool that wraps the existing threat-scoring helpers.
- New `intel_report` workflow with a 15-round tool budget and a narrow toolset — the threat scoring logic was kept, the orchestration and narrative code around it was deleted.
- Same output quality, one consistent code path for conversational scouting and scheduled scouting.

### Guardrails

- Per-turn cap of 5 external lookups per LLM conversation (`EXTERNAL_LOOKUP_CAP`) prevents runaway chains.
- In-module TTL cache (60–600s per endpoint) keeps conversational scouting cheap on the CR API.
- External lookups are excluded from low-context workflows (`observe`, `channel_update`, `reception`, `roster_bios`) where they have no business firing.

### Tests & Dev

- 24 new tests covering tag normalization, cache TTL, dispatch guards, per-aspect whitelist filters, envelope budget, and cap constants.
- New unified eval harness (`scripts/eval_all_requests.py`) runs regular, deck, and cr_api-tag buckets through the real pipeline in a single command.
- Cleaned up the `scripts/` directory and added a README documenting every operational and eval utility.

---

## v4.3 — Deck Review

**Date:** 2026-04-12

Elixir gained a dedicated deck-review workflow that grounds advice in each player's own battle history rather than generic meta talk. It handles regular Trophy Road decks, the four-deck River Race / Clan Wars war pool (which the Clash Royale API doesn't expose directly), and a build-from-scratch suggest mode that's especially useful for clan members who haven't played war yet because they can't figure out how to assemble four non-overlapping decks.

### Personalized Deck Review (`#ask-elixir`)

- Asking "review my deck" / "improve my deck" / "what should I change" now routes to a specialized workflow instead of generic Q&A.
- Advice is grounded in the player's actual recent losses — Elixir cites specific opponent cards (e.g. "Mega Knight has been in 6 of your last 9 losses") instead of repeating meta knowledge.
- All suggestions are validated against the player's collection and card levels — no recommending a card they don't own at competitive level.

### War Deck Review (`review my war decks`)

- Reconstructs the player's four river-race war decks from battle history, since the Clash Royale API doesn't expose them directly.
- Duel battles reveal three decks per battle (one per round); river-race PvP battles reveal one each.
- Returns confidence (`high` / `medium` / `low`) and asks for confirmation when the reconstruction is uncertain.
- Enforces the no-overlap rule on every swap suggestion: a card moved into one deck must come out of wherever it currently lives across the other three.

### Build From Scratch (`build me a deck`, `build my war decks`)

- "Build me a deck" → suggests 1–2 candidate decks with per-card reasoning, drawn from the player's collection and shaped by what's been beating them.
- "Build my war decks" / "I want to start playing war" → builds four full war decks (32 unique cards) with distinct roles per deck. A post-response validator confirms the no-overlap and ownership constraints, asking the LLM to revise (up to 2 attempts) on violations.

### New War Player Onboarding

- Asking "review my war decks" with no war activity yet triggers a warm offer: Elixir acknowledges the player hasn't played war, explains that building four non-overlapping decks is the most common blocker, and offers to put together a starter kit.
- The reply prompt routes seamlessly into the four-deck builder.

### Data Foundation

- New `opponent_deck_json` column on `member_battle_facts` captures opponent decks on every battle ingest going forward, plus a one-time backfill of all 11K+ historical battles from raw API payloads.
- New `losses` include on `get_member` and new `war_decks` aspect on `get_member_war_detail` cleanly extend the existing tool surface (no new top-level tools added).

### Structural

- New `deck_review` LLM workflow registered alongside `interactive` / `clanops` / `observation`, with a higher 10-round budget for the longer war-mode chains.
- New deck-request classifier separates "show my deck" (fast static report, unchanged) from "review my deck" (LLM workflow), eliminating a long-standing routing bug where review intent silently fell through to the display report.
- 12 new tests covering opponent capture, losses aggregation, war-deck reconstruction status logic, no-overlap regression, and the war-suggest validator.

---

## v4.2 — Race Command

**Date:** 2026-04-11

Elixir's River Race intelligence and internal architecture both got sharper in this release. The LLM tool layer was consolidated from 51 tools down to 15 domain-aligned tools, and the #river-race channel now carries real situational awareness of the competitive field and the clan's historic win streak.

### Tool Layer Consolidation

- Collapsed 51 single-purpose LLM tools into 15 domain-aligned tools with aspect-based routing (e.g. `get_war_season(aspect="standings")` instead of separate `get_war_champ_standings`).
- Reduces prompt overhead and gives the LLM cleaner, more predictable tool interfaces.

### River Race — Competing Clan Awareness

- The #river-race subagent now references competing clans by name with fame-gap framing — who's closest, who's falling behind, and snarky commentary when a rival barely shows up.
- Race standings data was already passed to the LLM but previously ignored; the prompt now actively instructs Elixir to use it.

### River Race — Win Streak Memory

- Introduced unscoped "clan identity" durable memories that load for the river-race subagent regardless of which war week is active.
- A race win streak memory is auto-updated on each `war_week_complete` signal by counting consecutive 1st-place finishes in the `war_races` table.
- POAP KINGS' unbroken 1st-place streak (Season 129 Week 2 to present) is now part of Elixir's River Race voice.

### River Race — Day Transition Consolidation

- When a battle day ends and a new one starts simultaneously, the two signals (`*_complete` + `*_started`) are now merged into a single batch, producing one cohesive message instead of two back-to-back posts.
- Applies to all day transition types: battle-to-battle, practice-to-battle, practice-to-practice.

### Structural

- Split large modules and tightened exception handling across the codebase.
- Added API retry logic for transient Clash Royale API failures.
- Fixed 3 pre-existing test failures from stale patch targets.
- Tightened promotion content: non-ASCII escaping in JSON output, more concise copy.

---

## v4.1 — Card Quiz

**Date:** 2026-04-07

Elixir gained a card catalog foundation and a new interactive quiz experience in `#card-quiz`. Members can now test and sharpen their Clash Royale card knowledge directly inside Discord.

### Card Catalog

- Added a synced card catalog from the Clash Royale `/cards` API (121 standard cards, daily refresh at 4 AM CT).
- New `lookup_cards` LLM tool gives Elixir accurate card data (elixir cost, rarity, type, Evo/Hero capability) so it stops guessing when members ask about card stats or tradeoffs in `#ask-elixir`.
- Card catalog syncs at startup and daily via the activity scheduler.

### Card Quiz (`#card-quiz`)

- `/elixir quiz start` — start an interactive quiz session (1-10 questions, ephemeral to the member).
- `/elixir quiz stats` — view personal accuracy and daily streak.
- `/elixir quiz leaderboard` — daily streak rankings.
- A daily quiz question is posted automatically each morning at 10 AM CT.

**Question types (v1):**
- What is the elixir cost of this card?
- Which of these cards costs the most/least elixir?
- What rarity is this card?
- Is this card a troop, spell, or building?
- Does this card support Evo, Hero, both, or neither?
- Which of these cards is a Champion?

All questions are generated from real card catalog data with card images. Daily questions track streaks for consecutive correct answers.

### Structural

- Renamed `integrations/` to `modules/` — both `poap_kings` and `card_training` now live under a unified feature module directory.
