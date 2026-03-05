# Prompt Externalization Design

## File Structure

```
prompts/
  PURPOSE.md    — Elixir's identity, voice, personality
  CLAN.md       — Clan identity, rules, history, traditions, thresholds
  GAME.md       — Clash Royale mechanics (game-generic)
  DISCORD.md    — Discord server structure, channel behaviors, config IDs
```

## What Lives in Prompt Files

- **PURPOSE.md**: Who Elixir is. Personality, tone, signing conventions. Portable across any clan.
- **GAME.md**: Clash Royale knowledge — war schedule, roles, arenas. Game-generic, rarely changes.
- **CLAN.md**: Clan-specific identity, rules, history, and configurable thresholds:
  - Trophy milestone intervals (currently hardcoded as every 1,000 in cr_knowledge.py)
  - Inactivity threshold (currently hardcoded as 3 days in heartbeat.py)
  - Promotion criteria and clan composition targets
  - Donation highlight thresholds
  - Clan history and lore (human-authored, updated occasionally)
- **DISCORD.md**: Discord server layout, per-channel behavior rules.

## What Stays in Code

- **Channel routing** — `if channel == X` dispatch logic (elixir.py)
- **Response format contracts** — JSON schemas the bot parses (event_type, content, share_content, etc.)
- **Tool definitions + execution** — OpenAI function-calling tools and dispatch
- **Signal detection logic** — heartbeat.py detectors (reads thresholds from CLAN.md)
- **Conversation memory** — SQLite read/write for leader Q&A history
- **Scheduling** — heartbeat interval, active hours, editorial cron
- **Nickname matching** — roster lookup and role-granting flow
- **LLM parameters** — model, temperature, max tokens, tool rounds

Principle: **Prompts define what Elixir says and why. Code defines when, where, and how.**

## No Templates — All LLM

Every message Elixir sends is LLM-generated. No hardcoded message templates anywhere.
Events that currently use f-string templates become LLM calls with context:

- **on_member_join** (Discord event) — pass event context to LLM, let it craft the welcome
- **member_join signal** (heartbeat) — already LLM-driven, no change
- **member_leave signal** (heartbeat) — currently a hardcoded string, becomes LLM-driven
- **nickname_match_success** — pass match result to LLM, it writes the confirmation
- **nickname_match_failure** — pass the failed name to LLM, it writes the guidance
- **role_grant_failure** — pass the error context to LLM, it writes the fallback

The LLM gets its voice from PURPOSE.md and knows channel context from DISCORD.md.
This means every message is consistent with Elixir's personality and adapts naturally
to changes in the prompt files. A casual clan's Elixir would welcome people differently
than a competitive clan's — without anyone touching code.

## Future Work

### Share-to-broadcast as a general tool
Currently `leader_share` is a special event_type that posts from #leader-lounge to #elixir.
This should become a general-purpose tool available in any interactive channel — "post this
to the broadcast channel." The tool definition would reference DISCORD.md to know which
channel has the `broadcast` role.

### Editorial publishing
The daily editorial + journal commit to poapkings.com is POAP KINGS-specific. Needs its own
design pass to make it optional/configurable rather than hardwired. Address separately.

### Configurable thresholds from CLAN.md
Signal detectors in heartbeat.py currently hardcode values:
- Trophy milestones: `range(1000, 15001, 1000)` in cr_knowledge.py
- Inactivity: 3 days in heartbeat.py:detect_inactivity
- Donation highlight hour: 8pm in heartbeat.py:tick

These should be read from CLAN.md (or a structured section of it) so a different clan
can set their own thresholds without editing Python.

## Portability Goal

A new clan forks elixir-bot and only rewrites:
- **CLAN.md** — their clan name, tag, rules, history, thresholds
- **DISCORD.md** — their Discord server layout, channel behaviors, and config IDs

PURPOSE.md and GAME.md stay mostly the same.
