# Clash Royale Game Knowledge

This file is for practical game knowledge Elixir can use when talking to the clan.
Prioritize clear guidance, not wiki-style completeness.

## River Race / Clan Wars

- Treat the Clash Royale API as the source of truth for the current war state.
- A war season contains multiple weekly races.
- Humans sometimes refer to war status as `SEASON-WEEK` shorthand, for example `130-1`.
- `season_id` is the season number.
- `section_index` is the current race week within that season, starting at 0.
- When speaking to humans, refer to the week as `section_index + 1`.

### War Phases

- There are two main live war phases: `practice` and `battle`.
- If the API says practice is active, talk about preparation.
- If the API says battle is active, talk about using war decks and winning the race.
- Avoid relying on local calendar or timezone assumptions when describing the current phase.
- In normal Clash Royale war rhythm, practice is typically Monday-Wednesday and battle is typically Thursday-Sunday, but live API state takes priority over that convention.

### Practice Phase Priorities

- Practice days are the setup window before battle days.
- The main clan priority during practice is setting boat defenses.
- Boat defenses are a one-time setup during practice days, not something members redo every day.
- A member may have some boat defenses set without having finished every available defense slot yet.
- Strong practice-day messaging should remind members to build or update boat defenses early.
- Intact boat defenses at the end of a day award bonus movement points, so early setup matters.
- On the final practice day, emphasize last-chance setup: finish boat defenses and get ready for battle days.

### Battle Phase Priorities

- Battle days are when the clan actively races and boat progress matters.
- Each player gets 4 war decks per battle day.
- The main clan priority during battle is using all 4 war decks.
- Thank members who already used all 4.
- Nudge members who have not started or who still have decks left.
- On the final battle day, emphasize that it is the last chance to use remaining decks before battle days end.

### Race Outcome

- The clan that reaches the finish line first wins the weekly race.
- Weekly placement matters for rewards and clan trophies.
- First place is a meaningful achievement and should be celebrated.
- In the final week of a season, the race can work differently from a normal week, so be careful and ground claims in current data.

## War Decks

- A player builds 4 war decks using 32 unique cards total.
- A card cannot appear in more than one of that player's war decks.
- Once a deck is used, it cannot be reused until the next war day reset.
- If helpful, mention that the in-game Magic wand can help auto-build decks from unused cards.

## Boat Defenses

- Boat defenses are separate from active war deck usage.
- Cards used on defense towers can overlap with war deck cards.
- A clan boat can hold multiple defenses, depending on league level.
- The live River Race API does not expose which member has placed boat defenses or how many defense slots each member has filled.
- The live River Race API does expose clan-level period-log defense metrics such as remaining intact defenses and progress earned from defenses after logged war days.
- Treat those period-log metrics as clan-level defense performance, not as proof that a specific member placed or finished defenses.
- Do not claim that a specific member finished or added boat defenses unless that came from another source besides the live API payload.
- Damaged boat defenses stay damaged between attacks, so teamwork and cleanup attacks matter.
- Elixir Collector, Mirror, and Clone cannot be placed on defense towers.

## Battle Modes

- `1v1 Battle`: one war deck, standard match.
- `Duel`: best-of-3 using up to 3 war decks; often a strong value play.
- `Rotating Game Mode`: special limited-time battle mode for war.
- `Boat Battle`: attack enemy boat defenses to slow another clan.
- `Colosseum`: special final-week mode; be careful and rely on live context before describing it in detail.

## Rewards and Recognition

- Weekly war participation is worth celebrating, especially strong fame totals and full-deck usage.
- First place in a race is a major clan achievement.
- At season end, recognize top contributors and perfect participation.
- Members must still be in the clan at race conclusion to claim their River Chest.

## Promotions and Clan Culture

- Consistent war participation matters, but real life comes first.
- Using all 4 decks on battle days is a strong sign of reliability.
- Setting boat defenses during practice also shows good clan support.
- When discussing member effort, be fair, specific, and grounded in actual tracked behavior.

## Ladder and General Progress

- Trophy milestones are meaningful and worth celebrating.
- Reaching 10,000+ trophies is elite within this clan context.
- Arena names should come from the API or stored data, not from guesswork.

## Roles

- Member -> Elder -> Co-Leader -> Leader
- Elder is a meaningful trust role, not an automatic reward.
- Co-Leader and Leader are leadership roles and should be discussed carefully.
