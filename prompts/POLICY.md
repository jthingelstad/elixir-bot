# POAP KINGS Policies

How Elder is earned, held, and lost, and when a member is considered for removal. Written for human leaders to read; the same rules are enforced deterministically in `engine/management.py`.

**Code owns these decisions, not the model.** The weekly review runs the scoring, the band, and the hysteresis every Monday and writes promotion/demotion cards to #actions. Read the result through the management projection — never re-derive who should be Elder, and never state a number that did not come from the engine.

## Elder is participation, and every input is in the player's control

Nothing about account power counts: not trophies, not card levels, not arena, not the Ranked league reached. Only what a player does — war decks played, ranked battles played, cards donated. League and prestige were removed on purpose (2026-07-12); they were a backdoor for collection strength.

Time served does not earn Elder either. There is a minimum tenure to be considered, and tenure breaks close calls, but sitting in the clan is not a contribution.

## Who is considered

Two hard filters, both evaluated live:

- **Tenure** — at least 28 days in the clan. This gates promotion *in*; a sitting Elder is never demoted over tenure.
- **The competitive floor** — at least 1 finalized war day with a deck actually played in the last 14 days, **or** at least 5 ranked battles in the last 14 days. War and ranked count equally here.

An Elder who fails *both* halves of that floor has abandoned the duty and is on the fast demotion path.

## How standing is scored

Every active member and Elder is ranked against the others (leaders and co-leaders are excluded from the ranking entirely) on three participation metrics, each turned into a percentile:

- **War rate** — per-day war credit, averaged over 28 days. Finishing a war day is worth more than the deck count suggests: 4 of 4 decks scores 1.00, 3 of 4 scores 0.56, 2 of 4 scores 0.38. Four decks is worth about 2.7x two decks, not 2x.
- **Ranked battles** — how many were played in the last 28 days. Participation, not the league reached.
- **Donations** — the trailing 4-week average, not a single week's snapshot.

Doing none of a thing scores zero for it; the players who did it are ranked against each other. The blend:

```
competitive = war% + 0.40 x ranked% x (1 - war%)
score       = 0.65 x competitive + 0.35 x donation%
```

War is the primary path because it is direct clan contribution. Ranked only represents the clan, so it fills part of the gap war leaves rather than substituting for it — and doing both is rewarded. Donations are the lighter half: lead by example, not the main route.

## How many Elders

Elders should be **20-30% of the whole active roster, leadership included**. That yields a floor, a ceiling, and a target at the midpoint.

- **Ceiling — hard.** Growth stops there, and only past it is anyone demoted for the count alone.
- **Target — the aim.** The middle of the band, not the bottom of it.
- **Floor — a drift limit, not a quota.** Nothing force-promotes to reach it. A clan without enough worthy members simply carries fewer Elders.

Growth is gated on worthiness as well as slots: a member must score at or above the clan median. Promoting a below-average member to hit a number is exactly what the target must not be allowed to cause.

## Promotion

Sustained, never a snapshot. A member must be in the promotable set on **three separate weekly reviews** before a card is raised. One miss is tolerated; two in a row resets the clock. So a card is roughly three to four weeks out from the first qualifying week, and leaders still make the final call.

Two different ways in:

- **An open seat** — the corps is below target. Nobody loses anything, so no deadband applies: strongest eligible first, stopping at the ceiling.
- **A swap** — the seats are full. The challenger must out-score the boundary Elder by a clear margin to take the seat. Inside that margin the contest is a close call, and **tenure decides it** — the longer-tenured player wins. A challenger still has to be ahead on score; tenure never manufactures a promotion, it only stops the deadband from shielding a shorter-tenured incumbent.

## Demotion

Demotion is a negative action and requires sustained evidence, never a small rank movement. Two reasons, two cadences:

- **Abandoned** — failed the war floor and the ranked floor. Two weeks.
- **Outranked** — fell outside the *ceiling* (not the target) and a higher-ranked member took the seat. Three weeks, matching the challenger's promotion cadence so the swap lands together and the Elder count never dips mid-swap.

Any week a member is off the gate resets the clock and withdraws an open card. An Elder sitting between the target and the ceiling is inside the tolerance the band exists to permit, and is not displaced for it.

Declining a card is the only "not now." The engine re-nominates on sustained evidence after 14 days, not on a leader-set clock.

## Removal

Removal is about inactivity and absence, measured from battles played and never from logins.

- A flat **5 battle-free days** is at-risk; **8 days** proposes a removal card. Trophies buy no extra rope.
- The only leeway is **contribution grace**: a member who currently clears the Elder competitive floor earns up to 4 extra confirm days, but that grace shrinks as the roster fills and reaches zero at 50/50. An idle seat only costs the clan something when there is no slot to spare.
- New members get no shield — everyone runs on the same clock from their own join date.
- A member who has told leaders they will be away is on **hold**, and the clock pauses until they return. Silence is not a hold.
- A declined kick card re-nominates after 7 days.

## Talking about any of this

- Promotions, demotions, and removals are discussed only in private leadership channels.
- The clan is told *why*. The weekly Elder Standing publishes to #announcements each Tuesday and names who is holding Elder, who is rising toward it, and who is slipping, each with their own participation evidence.
- Say it in terms a player can act on. "Outranked" means someone participated more than you this week. Never quote internal scores, percentiles, rank positions, or the Elder slot count to members.
