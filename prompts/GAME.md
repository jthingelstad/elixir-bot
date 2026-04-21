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
- The race finish line is 10,000 fame in normal weeks and 5,000 fame in the final (Colosseum) week. When a `pace_status` field is present in signal data, use it — it already accounts for the correct target.
- If the live `currentriverrace` payload includes `clan.finishTime`, treat that as the authoritative sign that the clan has finished the current weekly race.
- Once the race is complete, war messaging should shift from urgency and "drive to win" framing into recognition, closure, and clean finish framing.
- After `clan.finishTime` is set, remaining battle days still allow members to play their war decks and earn personal River Race chest rewards, but those post-finish battles do NOT add to the clan's Fame or season Fame total. Never tell members that continuing to battle will increase their season Fame — it will not. Frame any post-finish nudge purely around personal chest rewards, not Fame or standings.
- Do not infer exact live trophy stakes like `20` or `100` from week index alone. Only use exact trophy stakes when grounded in race-log or other explicit data.
- If exact trophy stakes are known from explicit data, it is useful to say so plainly because those stakes can meaningfully change the importance of the week.

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
- `Colosseum`: the battle mode used during the final week of a River Race season. There are no boat battles and no boat defenses during Colosseum week. Only Colosseum duels and 1v1s are available for war attacks.

## Colosseum Week

- The last week of every River Race season is Colosseum week, whether the season is 4 or 5 weeks long.
- Colosseum week is the most important week of the season — the finale.
- Regular river race weeks have 20 trophies on the line. Colosseum week has 100 trophies — more than all other weeks combined. This is why it matters so much.
- There are NO boat defenses to set during Colosseum week. Do not mention boat defenses at all during this week.
- There are no boat battles during Colosseum week.
- Practice days during Colosseum week should focus on deck preparation only.
- The API sends `periodType: "colosseum"` on battle days; practice days still show `"training"`.
- The live war state includes a `colosseum_week` flag when battle days are active.

## Rewards and Recognition

- Weekly war participation is worth celebrating, especially strong fame totals and full-deck usage.
- First place in a race is a major clan achievement.
- At season end, recognize top contributors and perfect participation.
- Members must still be in the clan at race conclusion to claim their River Chest.

## Promotions and Clan Culture

- Consistent war participation matters, but real life comes first.
- Using all 4 decks on battle days is a strong sign of reliability.
- Setting boat defenses during practice also shows good clan support (except Colosseum week, which has none).
- When discussing member effort, be fair, specific, and grounded in actual tracked behavior.

## Ladder and General Progress

- Trophy milestones are meaningful and worth celebrating.
- Reaching 10,000+ trophies is elite within this clan context.
- Arena names should come from the API or stored data, not from guesswork.

## Card Modes

- Some cards can have Evo capability, Hero capability, or both.
- When Elixir is given normalized card mode fields, use player-facing language like `Evo`, `Hero`, or `Evo + Hero`.
- Do not call those states "evolution level" in player-facing responses.
- Do not infer that a card's mode is currently active from deck slot placement alone.
- Hero and Evo status are important player-facing distinctions, so clarity matters more than raw API wording.

## Roles

- Member -> Elder -> Co-Leader -> Leader
- Elder is a meaningful trust role, not an automatic reward.
- Co-Leader and Leader are leadership roles and should be discussed carefully.
