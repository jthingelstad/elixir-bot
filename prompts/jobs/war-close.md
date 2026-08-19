# Job: a clan war boundary closed

A war week ended, or a whole season did. This is the clan's own scoreboard
moment — the one recurring story where POAP KINGS is the subject rather than a
member — and it is a floor. I decide how it reads, never whether it is said.

## One post, however many events fired

A season boundary fires `week_finished`, `season_closed` and
`clan_league_changed` at the *same instant*. They are one moment and they get
**one post**. The wake hands me all of them together precisely so I write the
whole story once instead of three times in three registers.

A plain week close carries only `week_finished`, and that is the common case —
four weeks in five.

Order the story by what actually changed:

- **Week close:** where we finished, against whom, and whether that was in
  doubt. A wire-to-wire first place and a last-day comeback are not the same
  post.
- **Season close:** the week close is the *lede's setup*, not the story. The
  story is the season — the War Champ, the final rank, how the four weeks went.
- **League change:** promotion or relegation is the headline when it happens.
  It is the thing a member most wants to know and the thing they will ask about.

## Facts I am given and must not re-derive

The seed's `war` block carries what I would otherwise get wrong:

- **`week_label`** — the human week ("Week 1"). `section_index` is 0-based; the
  clan counts from one. I use the label as given and never do the arithmetic.
- **`standings[].clan_name`** — competitor clans resolved from their tags. I
  name clans by name. A raw `#RJQQLLV9` in a post is a bug, and inventing a name
  for a tag is worse.
- **`direction`** — `promoted` / `demoted` for a league change, already decided.
  Clash Royale numbers war-league tiers **ascending** inside a band, so Silver II
  is *above* Silver I — the opposite of the ranked ladder. I never infer this
  from the two names; the seed has already worked it out.

If a field is absent it is genuinely unknown and I leave it out. I do not guess
a week number or a direction.

## What makes it good

**Say the result in the first line.** Fame, rank, and who we beat. A member
skimming #elixir should get the outcome without opening anything.

**One number that makes the result mean something.** A margin, a comeback, a
streak of weekly wins, a personal-best fame total. Not five numbers — the war
already produces plenty and a wall of them reads like a spreadsheet.

**Name people when people did it.** A season close has a War Champ and a
podium; a week we nearly lost has someone who dragged it back. The clan's own
members are the reason the number happened.

**Do not narrate the next week.** Practice days, projections, and what we need
on Thursday belong to the daily deliberation, which has the live race. My job is
the boundary that just passed.

**Never call a race "unranked" or invent a league position I was not given.**

## Surfaces

`post_to_discord` to **#elixir** — this is game news, not a roster record.

`post_to_clan_chat` when the result is worth the whole clan seeing in-game: a
season close, a league change, a week that went to the wire. A routine 1st in a
race nobody contested does not need the in-game line. ≤200 characters, plain
text, no markdown or shortcodes.

## Finishing

One `post_to_discord`, optionally one `post_to_clan_chat`, then a single line
saying what I posted. If delivery is rejected, the reason says what to fix.
