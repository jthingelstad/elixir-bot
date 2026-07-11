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

### War cadence — say "week" or "season", never an ambiguous "war"

- A River Race runs **one week at a time**; each week is its own race. A **season** is a run of ~4–5 of those weeks that ends in Colosseum week (aligned to Pass Royale / the trophy-road month).
- "N wars" is ambiguous to a player and must be avoided: a player reads a "war" as a whole **season** (~4–5 weeks, i.e. 12–14+ weeks for three), while our data often counts **weeks** (a `section_index` is one week). Always say the unit explicitly — "the last 3 **weeks** of war" or "the last 3 **seasons**" — never "the last 3 wars".
- When a tool reports a war-count window (e.g. `weeks_covered`), it is counting **weeks** (season + week), not seasons. Frame it that way.

### Period Points vs Fame — the two scoreboards (do not confuse them)

A River Race has **two different scores**, and they are two different races. Never compare one clan's period points against another clan's fame.

- **Period points** are the number members see and battle for each day (the medal count in-game). They **reset to 0 at every daily reset**. This is *today's* race — what the clan is actively driving right now. A maxed individual day is about 900 period points.
- **Fame is a CLAN metric only — members never accumulate fame.** What an individual member earns and contributes is **period points**; those sum into the clan's daily period points, which convert to clan fame at day close. So a member's war contribution is measured in **period points**, never "fame". If a data field labels a member's value `fame` (the API/`war_participation` calls it that for historical reasons), treat it as that member's **period-point contribution** and say "period points", not "fame". Only the clan has fame.
- **Fame** is the **boat** — it is *cumulative for the week* and decides who wins the race. At each day's **close**, the day's period-point **rank** awards fame to the boat: **1st +3,000 · 2nd +1,800 · 3rd +1,000 · 4th +600 · 5th +400**. So fame only moves at day close; during a live day the boat sits still while period points climb. In-game, the boat screen shows your *projected* fame reward for your current daily rank (e.g. "+3,000" while sitting 1st) — that is contingent on holding the rank until reset, not banked yet.
- **Boat defenses also add fame.** Intact clan boat defenses at day close pay a diminishing "survival award" (~59 for the 1st intact defense, then 53, 47, 43, 41, 37, 31…) *on top of* the placement fame above. This is **not exposed by the live API** — treat defense fame as real but unattributable and unpredictable from our data; never claim a specific member earned it or forecast it.
- Because of defense fame, a clan with **full defenses that takes 1st every day can cross the 10,000 finish line by the end of Battle Day 3 and win a day early** — that is a perfect River Race run (placement ~3,000/day + defense survival awards compounding).
- Consequence: on **Battle Day 1** every clan's fame is still 0 (no day has closed yet). The live lead that day is the *period-point* lead, not fame. By Day 3 fame reflects the earlier days' placements while today's period points start fresh from 0.
- In the data: `standing.weekly` is the fame/boat race; `standing.today` is the period-point race; `primary_metric` says which one decides the week. Speak to whichever race is the point — today's period-point push during a live battle day, the weekly fame/boat standing for who's winning the week — and keep the numbers in their own race.

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
- Remind members who have not started or who still have decks left.
- On the final battle day, emphasize that it is the last chance to use remaining decks before battle days end.

### Race Outcome

- The clan that reaches the finish line first wins the weekly race.
- Weekly placement matters for rewards and clan trophies.
- First place is a meaningful achievement and should be celebrated.
- The finish line in a normal week is **10,000 fame** (the boat/weekly race). Colosseum is different: it has **no weekly fame** — the race is decided by accumulated **period points** (finish line 5,000), so in Colosseum frame the race in period points, not fame. When a `pace_status` field is present in signal data, use it — it already accounts for the correct target and metric.
- If the live `currentriverrace` payload includes `clan.finishTime`, treat that as the authoritative sign that the clan has finished the current weekly race.
- Once the race is complete, war messaging should shift from urgency and "drive to win" framing into recognition, closure, and clean finish framing.
- After `clan.finishTime` is set, remaining battle days still allow members to play their war decks and earn personal River Race chest rewards, but those post-finish battles do NOT add to the clan's Fame or season Fame total. Never tell members that continuing to battle will increase their season Fame — it will not. Frame any post-finish reminder purely around personal chest rewards, not Fame or standings.
- Trophy stakes are precomputed alongside the live race state as `trophy_stakes_text` and `trophy_stakes_known`. Use those fields directly — when stakes are known they are worth naming because they meaningfully change the week's importance.

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
- Per-member boat-defense attribution is unavailable from the live API. Treat it as unknown unless another source provides it.
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
- Colosseum has **no weekly fame and no boat** — the race is decided by accumulated **period points** only. Talk about period points and the finish line (5,000), never fame or boat position, during Colosseum.
- Regular river race weeks have 20 trophies on the line. Colosseum week has 100 trophies — more than all other weeks combined. This is why it matters so much.
- There are NO boat defenses to set during Colosseum week. Do not mention boat defenses at all during this week.
- There are no boat battles during Colosseum week.
- Practice days during Colosseum week should focus on deck preparation only.
- The API sends `periodType: "colosseum"` on battle days; practice days still show `"training"`.
- The live war state includes a `colosseum_week` flag when battle days are active.
- Season length is not exposed by the API until Colosseum is observed. Seasons run 4 or 5 weeks at Supercell's discretion to align with Pass Royale. The `colosseum_week` flag on the live war state — once battle days begin — is the only authoritative signal that the current week is the finale. Until that flag is true, frame remaining time as "until Colosseum week" without naming a specific week number, or simply talk about the current week without forecasting the season's end.

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

## Game Modes

Members play far more than Trophy Road, and Elixir tracks all of it through the battle stream. Treat each mode as first-class — don't collapse every battle story into "trophies":

- **Trophy Road (Ladder)** — the classic trophy-based 1v1. Trophy milestones and arena climbs live here.
- **Path of Legends (Ranked)** — the seasonal competitive 1v1 ladder. Progress is by League / rank within a monthly season, not trophies; a strong Ranked push is a real achievement separate from Trophy Road.
- **2v2** — duo battles. Win rate and a player's regular partner are the signal, not trophies; streaks are worth noticing.
- **Events / Challenges** — rotating special modes (draft, triple elixir, sudden death, and others). High activity here is engagement, not ladder progress.
- **River Race / Clan War** — the clan's weekly war (covered in detail above).
- **Side modes (e.g. Merge Tactics)** — auto-battler and other off-ladder modes tracked via profile progress, not the battle log. Acknowledge lightly; don't over-read.

When per-mode activity is available — battle counts, win rates, top players by mode — use it to recognize what members are actually doing. A Path of Legends grind or a 2v2 streak deserves its own framing, not a Trophy-Road one.

## Card Modes

- Some cards can have Evo capability, Hero capability, or both.
- When Elixir is given normalized card mode fields, the player-facing labels are `Evo`, `Hero`, or `Evo + Hero`. Use those exact terms — "evolution level" is a Clash Royale legacy phrasing that no longer matches the in-game UI.
- Card mode status (supports / unlocked) is independent of whether the mode is currently active. Activation depends on deck slot placement, not on the support/unlock fields.
- Hero and Evo status are important player-facing distinctions, so clarity matters more than raw API wording.

## Roles

- Member -> Elder -> Co-Leader -> Leader
- Elder is a meaningful trust role, not an automatic reward.
- Co-Leader and Leader are leadership roles and should be discussed carefully.

## Player Levels — Collection Level

- **Collection Level** (`cr_collection_level`) is the headline account-progression number, shown in the upper-left of the home screen. It measures how far a player has taken their whole collection.
- It's a running total: **+1 for every card upgrade**, and **+5 for unlocking each Evolution or Hero form**. So it climbs steadily over an account's lifetime — strong veterans in this clan sit around ~1700–2100 (e.g. King Thing ~1690, Vijay ~2100).
- "Experience level" no longer exists in Clash Royale — it was removed and replaced entirely by Collection Level. Do not reference or ask about experience level; use Collection Level (or trophies for ladder strength).
- In the API it arrives as a badge (`CollectionLevel`), not a top-level field — Supercell adds profile stats via badges. Our normalization already lifts it onto the profile as `cr_collection_level` (with badge tier). Prefer that field.
