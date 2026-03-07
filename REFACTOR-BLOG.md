# From Snapshots to Signals: Elixir's V2 Data Model

A week ago, I rebuilt Elixir's entire database schema. Not to add features. Not to fix bugs. To make the bot's *thinking* fundamentally clearer.

The problem: Elixir was storing sparse snapshots and forcing the LLM to reconstruct facts indirectly. "What's King Levy's win rate?" meant the agent had to load 50 battle records, compute the ratio, format it, and hope the LLM didn't mess up the math.

The solution: a normalized V2 schema that separates raw ingest from normalized state from derived analytics from Discord metadata and memory. Now Elixir asks the database directly. No reconstruction. No LLM guessing.

Here's how I did it—and why the architecture matters.

## The Old Way: Snapshots as Source of Truth

Before V2, Elixir stored the Clash Royale API responses as blobs and kept a few materialized views:

```
clan_roster = fetch_clan_api()
player_profile = fetch_player_api(tag)
battle_log = fetch_battles_api(tag)

→ store these as semi-structured snapshots
→ when asked "what deck is King Levy running?" load the snapshot and search inside
→ when asked "who's improving?" load all player profiles and compare trophy history manually
→ when asked "who used all 4 war decks today?" scan the war participation rows and count...
```

This worked for simple questions. But complex ones became expensive:

- "List members at risk of demotion" → load all profiles, compute recent form, deduce trend
- "Who has the highest win rate in wars?" → load all battle facts, filter by war, compute ratio
- "What cards is the clan overleveled in?" → load all card collections, find the mode, compare to meta
- "Who just upgraded a card to level 15?" → load current and yesterday's snapshot, diff them

The LLM had to do all this reasoning. And if the reasoning was wrong, there was no audit trail—just guesses baked into Discord messages.

## The V2 Solution: Layered Schema Design

I split the schema into five distinct layers:

### Layer 1: Raw Ingest (No Changes, Just Storage)

```sql
raw_api_payloads(endpoint, entity_key, fetched_at, payload_hash, payload_json)
```

Every API response gets logged as-is. Never modified. This is your audit trail and your escape hatch if normalization breaks.

### Layer 2: Current State (Fast Queries)

```sql
members(member_id, player_tag, current_name, status, first_seen_at, last_seen_at)
member_current_state(member_id, role, exp_level, trophies, donations_week, ...)
clan_memberships(member_id, joined_at, left_at, join_source)
player_profile_snapshots(member_id, fetched_at, exp_level, current_deck_json, cards_json, ...)
```

One row per member. Current facts only. No history. Indexed heavily. Fast.

When you ask "list all active members," you hit `member_current_state` and get answers in milliseconds, not by diffing snapshots.

### Layer 3: Historical Facts (Event Stream)

```sql
member_daily_metrics(member_id, metric_date, exp_level, trophies, donations_week, ...)
member_battle_facts(member_id, battle_time, battle_type, deck_json, outcome, trophy_change, ...)
war_participation(war_race_id, member_id, fame, repair_points, decks_used, ...)
clan_memberships(member_id, joined_at, left_at)  -- tracks join/leave cycles
```

Every event becomes a row. Battles, days, season participation. Immutable.

Now "who improved most this week?" is a SQL query: `SELECT member_id, MAX(trophies) - MIN(trophies) FROM member_daily_metrics WHERE metric_date BETWEEN ... GROUP BY member_id ORDER BY delta DESC`.

### Layer 4: Derived Analytics (Precomputed Intelligence)

```sql
member_recent_form(member_id, scope, wins, losses, current_streak, win_rate, form_label, ...)
member_card_usage_snapshots(member_id, fetched_at, cards_json)  -- top 5 signature cards
member_deck_snapshots(member_id, fetched_at, mode_scope, deck_json, sample_size)
```

I precompute the stuff LLMs would guess at:

- **Recent form**: 10-game, 25-game, ladder, war, ranked scopes
- **Card signatures**: "what does this player actually use?"
- **Deck profiles**: "ladder deck vs. war deck vs. event deck"
- **Form labels**: `hot`, `strong`, `mixed`, `slumping`, `cold`, `inactive`

When Elixir answers "is King Levy hot right now?", it queries one row instead of reconstructing from 50 battles.

### Layer 5: Discord Identity & Memory (First-Class Citizenship)

```sql
discord_users(discord_user_id, username, global_name, first_seen_at, last_seen_at)
discord_links(discord_user_id, member_id, confidence, source, is_primary)
conversation_threads(scope_type, scope_key, channel_id, discord_user_id, member_id, created_at)
messages(discord_message_id, thread_id, author_type, workflow, content, summary, created_at)
memory_facts(subject_type, subject_key, fact_type, fact_value, confidence, expires_at)
memory_episodes(subject_type, subject_key, episode_type, summary, importance, source_message_ids_json)
channel_state(channel_id, last_elixir_post_at, last_topics_json, last_summary)
```

Discord is no longer a routing layer. It's data.

Elixir now stores:
- Who is King Levy on Discord? (and with how much confidence)
- What has Elixir told each user before?
- What did we discuss in `#reception` last week?
- Did someone just join? When?
- Are we repeating ourselves?

This kills the "generic greeting every time" problem. Elixir reads the room.

## The Key Insight: Separate Concerns

The schema doesn't mix these things:

1. **Raw facts** (from Clash Royale API) stay raw
2. **Normalized state** is fast-pathed and indexed
3. **Historical records** are immutable event stream
4. **Derived analytics** are precomputed, not reconstructed
5. **Discord context** is explicit, not inferred

Before: database → agent → LLM → guess → Discord  
After: database query → formatted answer → Discord

The LLM now works with *facts*, not reconstructions.

## What This Enables

### Before V2

Agent: "What cards is King Levy using?"  
Database: *(loads entire profile JSON)*  
Agent: *(searches inside JSON)*  
LLM: "Probably Valkyrie and Skeletons?" ← guessing

### After V2

Tool: `get_member_signature_cards(member_tag, scope='overall')`  
Database: `SELECT cards_json FROM member_card_usage_snapshots WHERE member_id = ? ORDER BY fetched_at DESC LIMIT 1`  
Result: `[{"name": "Valkyrie", "usage_pct": 70}, {"name": "Skeleton Barrel", "usage_pct": 60}]`  
LLM: "King Levy's top cards are Valkyrie (70%) and Skeleton Barrel (60%)." ← deterministic

### More Examples

**"Is King Levy improving?"**
- Before: load profiles from 3 different days, compute trophy delta, hope the math is right
- After: `SELECT wins, losses, form_label FROM member_recent_form WHERE member_id = ? AND scope = 'overall_10'` → `{wins: 7, losses: 3, form_label: 'hot'}`

**"Who used all 4 war decks today?"**
- Before: scan war participation rows, count deck usage per member, check if == 4
- After: `SELECT member_id FROM war_day_status WHERE battle_date = TODAY AND decks_used_today = 4`

**"List members who might be ready for elder."**
- Before: load profiles, compare thresholds in the LLM, uncertain
- After: 
```sql
SELECT m.current_name, mcs.trophies, mrf.win_rate
FROM member_current_state mcs
JOIN members m USING(member_id)
JOIN member_recent_form mrf USING(member_id)
WHERE mcs.trophies > 5000 AND mrf.win_rate > 0.6 AND mrf.scope = 'overall_10'
```

**"Did King Levy just level up?"**
- Before: compare today's profile to yesterday's, hope you fetched at the right times
- After: `SELECT member_id FROM member_daily_metrics WHERE member_id = ? AND exp_level > YESTERDAY.exp_level`

## The Schema at a Glance

| Layer | Purpose | Mutability | Query Pattern |
|-------|---------|-----------|---------------|
| Raw Ingest | Audit trail | Append-only | Rare; debugging |
| Current State | Fast facts | Upsert | Primary; indexed |
| Historical Facts | Event stream | Append-only | Analytics; trends |
| Derived Analytics | Precomputed intelligence | Materialized | Fast answers |
| Discord Memory | Context & identity | Append-only facts | Prevent repetition; link users |

## What Stayed the Same

- **Public APIs**: Tools still look the same to the agent
- **Database file**: Still `elixir.db`; still SQLite
- **Discord functionality**: Channels, member linking, heartbeat
- **Prompts and personalities**: No change

What changed is *underneath*. The database now *models the domain* instead of just storing blobs.

## Why This Matters

### For Maintenance

When something's broken ("Elixir said King Levy's deck was Mega Knight, but it's actually P.E.K.K.A."), you can:

1. Check `raw_api_payloads` for the original CR API response
2. Check `member_deck_snapshots` for what we normalized
3. Check `messages` for what Elixir said to Discord
4. Audit the tool that formatted the answer

There's a chain of custody. No more "the LLM probably hallucinated."

### For Features

Adding "detect someone's power level" now means:
1. Decide what that means: trophies + win rate + card levels + war participation
2. Write a SQL query that combines those facts
3. Create a tool that runs that query
4. Elixir uses it

No training, no prompt engineering, no luck.

### For Confidence

Before V2, Elixir's answers were as good as the LLM's reasoning that day. Today, Elixir answers are as good as the database is clean. Much better odds.

## The Trade-Off

Normalization costs compute on the write side:

- Fetch clan roster → normalize into `members` and `member_current_state` and `clan_memberships`
- Fetch player profile → normalize into `player_profile_snapshots`, extract `current_deck`, compute `member_recent_form`
- Ingest war log → normalize into `war_races`, `war_participation`, compute war champ standings

This is *good*. Expensive work happens once, at ingest time. Queries are cheap.

The old way was backwards: cheap writes, expensive queries.

## Open Questions V2 Answered

- "Why do I have to ask the LLM the same question twice to get consistent answers?" → Because the LLM was reconstructing facts from snapshots. V2 precomputes.
- "How do I audit what Elixir told someone?" → `messages` table + `memory_facts` table.
- "Why does Elixir forget context between messages?" → No durable memory. V2 stores conversations.
- "How do I add a new query tool?" → Write the SQL. Wire it in. No LLM prompt tuning needed.

## What's Next

V2 isn't "done" in the sense of being frozen. It's *stable* and *extensible*. New signals are just:

1. New materialized view (e.g., `member_donation_streaks`)
2. Precompute it at ingest time
3. Create a tool that queries it
4. Done

The schema scales because it separates concerns. Adding "deck power level" doesn't require rewriting war participation logic.

---

This refactor took a few hours and broke nothing. The database schema changed completely, but the bot still works. All tests pass. The clan doesn't notice.

But behind the scenes, Elixir's thinking is now grounded in *facts*, not *guesses*.

And that's worth the refactor.
